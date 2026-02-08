"""Solana meme coin trading service — API interaction layer."""

from __future__ import annotations

import base64
import json
import time
from dataclasses import dataclass, field
from typing import Any

import httpx
from loguru import logger

SOL_MINT = "So11111111111111111111111111111111111111112"
LAMPORTS_PER_SOL = 1_000_000_000


@dataclass
class TokenInfo:
    """DexScreener token data snapshot."""
    address: str
    symbol: str
    name: str
    price_usd: float
    volume_24h: float
    liquidity_usd: float
    price_change_5m: float
    price_change_1h: float
    price_change_24h: float
    buy_count_5m: int
    sell_count_5m: int
    pair_address: str
    fdv: float


@dataclass
class SwapQuote:
    """Jupiter swap quote."""
    input_mint: str
    output_mint: str
    in_amount: int
    out_amount: int
    price_impact_pct: float
    slippage_bps: int
    route_plan: list[dict[str, Any]] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)


class TradingService:
    """Handles all external API calls for Solana trading."""

    def __init__(
        self,
        helius_api_key: str,
        rpc_url: str | None = None,
        jupiter_base: str = "https://api.jup.ag",
        dexscreener_base: str = "https://api.dexscreener.com",
        dry_run: bool = True,
    ):
        self.helius_api_key = helius_api_key
        self.rpc_url = rpc_url or f"https://mainnet.helius-rpc.com/?api-key={helius_api_key}"
        self.jupiter_base = jupiter_base.rstrip("/")
        self.dexscreener_base = dexscreener_base.rstrip("/")
        self.dry_run = dry_run

    # ------------------------------------------------------------------ #
    #  DexScreener
    # ------------------------------------------------------------------ #

    async def get_token_info(self, token_addresses: list[str]) -> list[TokenInfo]:
        """Fetch token data from DexScreener for Solana token addresses."""
        addr_str = ",".join(token_addresses[:30])
        url = f"{self.dexscreener_base}/tokens/v1/solana/{addr_str}"

        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(url)
            r.raise_for_status()

        pairs = r.json()
        if isinstance(pairs, dict):
            pairs = pairs.get("pairs", pairs.get("pair", []))
        if not isinstance(pairs, list):
            pairs = []

        results: list[TokenInfo] = []
        seen: set[str] = set()

        for pair in pairs:
            base = pair.get("baseToken", {})
            addr = base.get("address", "")
            if not addr or addr in seen:
                continue
            seen.add(addr)

            txns = pair.get("txns", {})
            m5 = txns.get("m5", {})
            pc = pair.get("priceChange", {})

            results.append(TokenInfo(
                address=addr,
                symbol=base.get("symbol", "???"),
                name=base.get("name", ""),
                price_usd=float(pair.get("priceUsd") or 0),
                volume_24h=float(pair.get("volume", {}).get("h24") or 0),
                liquidity_usd=float(pair.get("liquidity", {}).get("usd") or 0),
                price_change_5m=float(pc.get("m5") or 0),
                price_change_1h=float(pc.get("h1") or 0),
                price_change_24h=float(pc.get("h24") or 0),
                buy_count_5m=int(m5.get("buys") or 0),
                sell_count_5m=int(m5.get("sells") or 0),
                pair_address=pair.get("pairAddress", ""),
                fdv=float(pair.get("fdv") or 0),
            ))
        return results

    async def scan_trending_tokens(self) -> list[TokenInfo]:
        """Fetch trending/boosted tokens from DexScreener, filtered to Solana."""
        url = f"{self.dexscreener_base}/token-boosts/latest/v1"

        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(url)
            r.raise_for_status()

        boosts = r.json()
        if not isinstance(boosts, list):
            boosts = []

        solana_addrs = [
            b["tokenAddress"]
            for b in boosts
            if b.get("chainId") == "solana" and b.get("tokenAddress")
        ]
        if not solana_addrs:
            return []

        return await self.get_token_info(solana_addrs[:30])

    # ------------------------------------------------------------------ #
    #  Jupiter
    # ------------------------------------------------------------------ #

    async def get_swap_quote(
        self,
        input_mint: str,
        output_mint: str,
        amount_lamports: int,
        slippage_bps: int = 300,
    ) -> SwapQuote:
        """Get a Jupiter swap quote."""
        params = {
            "inputMint": input_mint,
            "outputMint": output_mint,
            "amount": str(amount_lamports),
            "slippageBps": slippage_bps,
        }
        url = f"{self.jupiter_base}/swap/v1/quote"

        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(url, params=params)
            r.raise_for_status()

        data = r.json()
        return SwapQuote(
            input_mint=input_mint,
            output_mint=output_mint,
            in_amount=int(data.get("inAmount") or 0),
            out_amount=int(data.get("outAmount") or 0),
            price_impact_pct=float(data.get("priceImpactPct") or 0),
            slippage_bps=slippage_bps,
            route_plan=data.get("routePlan", []),
            raw=data,
        )

    async def execute_swap(
        self,
        quote_raw: dict[str, Any],
        wallet_pubkey: str,
    ) -> dict[str, Any]:
        """Build a swap transaction via Jupiter. Returns serialized tx for signing."""
        if self.dry_run:
            return {
                "status": "dry_run",
                "message": "Swap simulated (dry_run=True)",
                "quote": {
                    "inAmount": quote_raw.get("inAmount"),
                    "outAmount": quote_raw.get("outAmount"),
                },
            }

        payload = {
            "quoteResponse": quote_raw,
            "userPublicKey": wallet_pubkey,
            "wrapAndUnwrapSol": True,
            "prioritizationFeeLamports": "auto",
        }
        url = f"{self.jupiter_base}/swap/v1/swap"

        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(url, json=payload)
            r.raise_for_status()

        return r.json()

    # ------------------------------------------------------------------ #
    #  Helius / Solana RPC
    # ------------------------------------------------------------------ #

    async def _rpc_call(self, method: str, params: list | dict, timeout: float = 15.0) -> Any:
        """Generic Solana JSON-RPC call."""
        payload = {"jsonrpc": "2.0", "id": "nanobot", "method": method, "params": params}
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.post(self.rpc_url, json=payload)
            r.raise_for_status()
        data = r.json()
        if "error" in data:
            raise RuntimeError(f"RPC error: {data['error']}")
        return data.get("result")

    async def get_sol_balance(self, wallet_pubkey: str) -> float:
        """Get SOL balance."""
        result = await self._rpc_call("getBalance", [wallet_pubkey])
        lamports = result.get("value", 0) if isinstance(result, dict) else 0
        return lamports / LAMPORTS_PER_SOL

    async def get_portfolio(self, wallet_pubkey: str) -> dict[str, Any]:
        """Get token holdings using Helius DAS getAssetsByOwner."""
        result = await self._rpc_call(
            "getAssetsByOwner",
            {"ownerAddress": wallet_pubkey, "displayOptions": {"showFungible": True}},
            timeout=20.0,
        )
        return result or {}

    async def get_token_holders(self, mint_address: str) -> dict[str, Any]:
        """Get largest token holders to detect rug risk."""
        result = await self._rpc_call("getTokenLargestAccounts", [mint_address])
        accounts = result.get("value", []) if isinstance(result, dict) else []
        if not accounts:
            return {"holder_count": 0, "top_holder_pct": 100.0}

        amounts = [int(a.get("amount", 0)) for a in accounts]
        total = sum(amounts) or 1
        top_pct = max(amounts) / total * 100

        return {"holder_count": len(accounts), "top_holder_pct": round(top_pct, 2)}

    async def send_signed_transaction(self, signed_tx_base64: str) -> str:
        """Submit a signed transaction to the Solana network."""
        if self.dry_run:
            return "DRY_RUN_SIMULATED_TX_SIG"

        result = await self._rpc_call(
            "sendTransaction",
            [signed_tx_base64, {"encoding": "base64"}],
            timeout=30.0,
        )
        return result or ""
