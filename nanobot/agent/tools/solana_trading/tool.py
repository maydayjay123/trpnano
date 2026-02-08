"""SolanaTraderTool — autonomous Solana meme coin trading with learning."""

from __future__ import annotations

import base64
import time
from pathlib import Path
from typing import Any

from loguru import logger

from nanobot.agent.tools.base import Tool
from nanobot.agent.tools.solana_trading.memory import StrategyMemory
from nanobot.agent.tools.solana_trading.positions import PositionManager
from nanobot.agent.tools.solana_trading.safety import _compute_trend_score, check_token_safety
from nanobot.agent.tools.solana_trading.service import (
    LAMPORTS_PER_SOL,
    SOL_MINT,
    TradingService,
    TokenInfo,
)
from nanobot.config.schema import SolanaTradingConfig


class SolanaTraderTool(Tool):
    """Autonomous Solana meme coin trading with learning memory."""

    def __init__(self, config: SolanaTradingConfig, data_dir: Path):
        self._config = config
        self._service = TradingService(
            helius_api_key=config.helius_api_key,
            rpc_url=config.rpc_url or None,
            jupiter_base=config.jupiter_base_url,
            dexscreener_base=config.dexscreener_base_url,
            dry_run=config.dry_run,
        )
        trading_dir = data_dir / "solana_trading"
        self._positions = PositionManager(trading_dir / "positions.json")
        self._memory = StrategyMemory(trading_dir / "strategy.json")
        self._wallet_pubkey = ""
        self._channel = ""
        self._chat_id = ""

        if config.wallet_private_key:
            try:
                from solders.keypair import Keypair
                kp = Keypair.from_base58_string(config.wallet_private_key)
                self._wallet_pubkey = str(kp.pubkey())
            except Exception as e:
                logger.warning(f"Could not derive wallet pubkey: {e}")

    def set_context(self, channel: str, chat_id: str) -> None:
        self._channel = channel
        self._chat_id = chat_id

    @property
    def name(self) -> str:
        return "solana_trader"

    @property
    def description(self) -> str:
        return (
            "Autonomous Solana meme coin trading. Actions: "
            "scan_trending, get_quote, swap, portfolio, positions, "
            "set_limits, check_exits, stats, memory, learn."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [
                        "scan_trending", "get_quote", "swap",
                        "portfolio", "positions", "set_limits",
                        "check_exits", "stats", "memory", "learn",
                    ],
                    "description": "Action to perform",
                },
                "input": {
                    "type": "string",
                    "description": "Parameters as key=value pairs. E.g. token_address=ABC123,amount_sol=0.1,side=buy,note=avoid low liq tokens",
                },
            },
            "required": ["action"],
        }

    @staticmethod
    def _parse_input(raw: str) -> dict[str, Any]:
        """Parse 'key=value,key2=value2' into a dict."""
        params: dict[str, Any] = {}
        if not raw:
            return params
        for pair in raw.split(","):
            if "=" not in pair:
                continue
            k, v = pair.split("=", 1)
            k = k.strip()
            v = v.strip()
            # Auto-convert numeric values
            try:
                if "." in v:
                    params[k] = float(v)
                else:
                    params[k] = int(v)
            except ValueError:
                params[k] = v
        return params

    async def execute(self, action: str, input: str = "", **kwargs: Any) -> str:
        # Merge parsed input with any direct kwargs
        parsed = self._parse_input(input)
        kwargs.update(parsed)
        if not self._config.enabled:
            return "Error: Solana trading not enabled. Set tools.solanaTrading.enabled=true in config."
        if not self._wallet_pubkey:
            return "Error: No wallet configured. Set tools.solanaTrading.walletPrivateKey in config."

        try:
            match action:
                case "scan_trending":
                    return await self._scan_trending()
                case "get_quote":
                    return await self._get_quote(**kwargs)
                case "swap":
                    return await self._swap(**kwargs)
                case "portfolio":
                    return await self._portfolio()
                case "positions":
                    return await self._positions_report()
                case "set_limits":
                    return await self._set_limits(**kwargs)
                case "check_exits":
                    return await self._check_exits()
                case "stats":
                    return self._positions.format_stats()
                case "memory":
                    return self._memory.get_context_for_agent()
                case "learn":
                    return self._learn(**kwargs)
                case _:
                    return f"Unknown action: {action}"
        except Exception as e:
            logger.error(f"SolanaTrader error ({action}): {e}")
            return f"Error in {action}: {e}"

    # ------------------------------------------------------------------ #
    #  learn — user teaches the bot
    # ------------------------------------------------------------------ #

    def _learn(self, note: str = "", **_: Any) -> str:
        if not note:
            return "Error: provide a 'note' with the lesson to learn"
        self._memory.add_user_note(note)
        return f"Learned and saved: {note}"

    # ------------------------------------------------------------------ #
    #  scan_trending — now with memory context + auto-buy
    # ------------------------------------------------------------------ #

    async def _scan_trending(self) -> str:
        tokens = await self._service.scan_trending_tokens()
        if not tokens:
            return "No trending Solana tokens found right now."

        risk = self._config.risk
        sol_balance = await self._service.get_sol_balance(self._wallet_pubkey)
        suggested_size = self._positions.suggest_trade_size(risk.max_position_sol, sol_balance)
        dry_tag = " [DRY RUN]" if self._config.dry_run else ""
        autonomous = self._config.autonomous

        # Include memory context for agent decision-making
        memory_ctx = self._memory.get_context_for_agent()

        # Score and filter tokens
        scored: list[tuple[TokenInfo, int, float]] = []
        for t in tokens:
            # Check avoid list
            avoid = self._memory.should_avoid(t.address)
            if avoid:
                continue
            score = _compute_trend_score(t)
            total = t.buy_count_5m + t.sell_count_5m
            buy_pct = (t.buy_count_5m / total * 100) if total > 0 else 0
            scored.append((t, score, buy_pct))

        scored.sort(key=lambda x: x[1], reverse=True)

        lines = [f"Trending Solana Meme Coins{dry_tag}:\n"]

        # Auto-buy candidates (autonomous mode)
        auto_bought: list[str] = []

        for t, score, buy_pct in scored[:5]:
            flags: list[str] = []
            if t.liquidity_usd < risk.min_liquidity_usd:
                flags.append("LOW_LIQ")
            if t.volume_24h < risk.min_volume_24h_usd:
                flags.append("LOW_VOL")
            flag_str = f" [{', '.join(flags)}]" if flags else ""

            # Mark auto-buy candidates
            is_candidate = (
                score >= self._config.min_trend_score
                and not flags
                and buy_pct >= 60
            )
            marker = " >>> AUTO-BUY CANDIDATE" if is_candidate and autonomous else ""

            lines.append(
                f"  {t.symbol} | Score: {score}/100{flag_str}{marker}\n"
                f"    Addr: {t.address}\n"
                f"    Price: ${t.price_usd:.8f} | 5m: {t.price_change_5m:+.1f}% | 1h: {t.price_change_1h:+.1f}%\n"
                f"    Vol24h: ${t.volume_24h:,.0f} | Liq: ${t.liquidity_usd:,.0f}\n"
                f"    5m: {t.buy_count_5m} buys / {t.sell_count_5m} sells ({buy_pct:.0f}% buy)"
            )

            # Autonomous auto-buy
            if is_candidate and autonomous and len(auto_bought) < 3:
                buy_result = await self._execute_buy(t.address, suggested_size, risk.max_slippage_bps)
                if "BLOCKED" not in buy_result and "Error" not in buy_result:
                    auto_bought.append(f"  {t.symbol}: {buy_result.split(chr(10))[0]}")
                    # Record in memory
                    self._memory.add_trade_review(
                        token_address=t.address, symbol=t.symbol, side="buy",
                        pnl_sol=0, pnl_pct=0, trend_score=score, buy_ratio=buy_pct,
                        liquidity_usd=t.liquidity_usd, hold_time_min=0, reason="auto_buy",
                    )

        lines.append(f"\nSOL balance: {sol_balance:.4f} SOL")
        lines.append(f"Suggested trade size: {suggested_size} SOL")
        lines.append(f"Mode: {'AUTONOMOUS' if autonomous else 'MANUAL'}{dry_tag}")

        if auto_bought:
            lines.append(f"\nAUTO-BOUGHT ({len(auto_bought)}):")
            lines.extend(auto_bought)

        return "\n".join(lines)

    # ------------------------------------------------------------------ #
    #  get_quote
    # ------------------------------------------------------------------ #

    async def _get_quote(
        self, token_address: str = "", amount_sol: float = 0,
        side: str = "buy", slippage_bps: int = 0, **_: Any,
    ) -> str:
        if not token_address:
            return "Error: token_address required"
        if amount_sol <= 0:
            return "Error: amount_sol must be positive"

        slippage = slippage_bps or self._config.risk.max_slippage_bps

        if side == "buy":
            lamports = int(amount_sol * LAMPORTS_PER_SOL)
            quote = await self._service.get_swap_quote(SOL_MINT, token_address, lamports, slippage)
        else:
            quote = await self._service.get_swap_quote(token_address, SOL_MINT, int(amount_sol), slippage)

        return (
            f"Jupiter Quote ({side.upper()}):\n"
            f"  Input: {quote.in_amount} ({quote.input_mint[:12]}...)\n"
            f"  Output: {quote.out_amount} ({quote.output_mint[:12]}...)\n"
            f"  Price Impact: {quote.price_impact_pct:.4f}%\n"
            f"  Slippage: {quote.slippage_bps} bps\n"
            f"  Routes: {len(quote.route_plan)}"
        )

    # ------------------------------------------------------------------ #
    #  swap
    # ------------------------------------------------------------------ #

    async def _swap(
        self, token_address: str = "", amount_sol: float = 0,
        side: str = "buy", slippage_bps: int = 0, **_: Any,
    ) -> str:
        if not token_address:
            return "Error: token_address required"
        if amount_sol <= 0:
            return "Error: amount_sol must be positive"
        if side not in ("buy", "sell"):
            return "Error: side must be 'buy' or 'sell'"

        slippage = min(slippage_bps or self._config.risk.max_slippage_bps, self._config.risk.max_slippage_bps)

        if side == "buy":
            return await self._execute_buy(token_address, amount_sol, slippage)
        else:
            return await self._execute_sell(token_address, amount_sol, slippage)

    async def _execute_buy(self, token_address: str, amount_sol: float, slippage: int) -> str:
        risk = self._config.risk

        # Check avoid list
        avoid = self._memory.should_avoid(token_address)
        if avoid:
            return f"Trade BLOCKED by memory: {avoid}"

        tokens = await self._service.get_token_info([token_address])
        if not tokens:
            return f"Error: Token {token_address} not found on DexScreener"
        token = tokens[0]

        safety = await check_token_safety(token, risk, self._positions, self._service, amount_sol)
        if not safety.safe:
            return f"Trade BLOCKED:\n{safety}\nTrend score: {safety.score}/100"

        lamports = int(amount_sol * LAMPORTS_PER_SOL)
        quote = await self._service.get_swap_quote(SOL_MINT, token_address, lamports, slippage)

        if abs(quote.price_impact_pct) > 5.0:
            return f"Trade BLOCKED: Price impact {quote.price_impact_pct:.2f}% > 5% limit"

        result = await self._service.execute_swap(quote.raw, self._wallet_pubkey)

        if self._config.dry_run:
            self._positions.open_position(
                token_address=token_address, symbol=token.symbol,
                entry_price_usd=token.price_usd, amount_tokens=float(quote.out_amount),
                amount_sol_in=amount_sol, stop_loss_pct=risk.stop_loss_pct,
                take_profit_pct=risk.take_profit_pct,
            )
            return (
                f"DRY RUN BUY:\n"
                f"  Token: {token.symbol} ({token_address})\n"
                f"  Spent: {amount_sol} SOL | Received: ~{quote.out_amount} tokens\n"
                f"  Price: ${token.price_usd:.8f} | Score: {safety.score}/100\n"
                f"  SL: -{risk.stop_loss_pct}% | TP: +{risk.take_profit_pct}%"
            )

        return await self._sign_and_send_buy(result, token, quote, amount_sol, safety.score)

    async def _sign_and_send_buy(self, swap_result: dict, token: Any, quote: Any, amount_sol: float, score: int) -> str:
        risk = self._config.risk
        try:
            from solders.keypair import Keypair
            from solders.transaction import VersionedTransaction
        except ImportError:
            return "Error: 'solders' required for live trading. pip install solders"

        swap_tx = swap_result.get("swapTransaction", "")
        if not swap_tx:
            return f"Error: No transaction from Jupiter: {swap_result}"

        tx_bytes = base64.b64decode(swap_tx)
        tx = VersionedTransaction.from_bytes(tx_bytes)
        kp = Keypair.from_base58_string(self._config.wallet_private_key)
        tx.sign([kp])
        signed_b64 = base64.b64encode(bytes(tx)).decode()
        tx_sig = await self._service.send_signed_transaction(signed_b64)

        self._positions.open_position(
            token_address=token.address, symbol=token.symbol,
            entry_price_usd=token.price_usd, amount_tokens=float(quote.out_amount),
            amount_sol_in=amount_sol, stop_loss_pct=risk.stop_loss_pct,
            take_profit_pct=risk.take_profit_pct,
        )

        return (
            f"BUY EXECUTED:\n"
            f"  Token: {token.symbol} ({token.address})\n"
            f"  Spent: {amount_sol} SOL | Score: {score}/100\n"
            f"  TX: {tx_sig}\n"
            f"  SL: -{risk.stop_loss_pct}% | TP: +{risk.take_profit_pct}%"
        )

    async def _execute_sell(self, token_address: str, amount_tokens: float, slippage: int) -> str:
        positions = self._positions.get_positions_for_token(token_address)
        if not positions:
            return f"No open position for {token_address}"

        tokens = await self._service.get_token_info([token_address])
        token = tokens[0] if tokens else None
        current_price = token.price_usd if token else 0

        quote = await self._service.get_swap_quote(token_address, SOL_MINT, int(amount_tokens), slippage)
        sol_out = quote.out_amount / LAMPORTS_PER_SOL

        result = await self._service.execute_swap(quote.raw, self._wallet_pubkey)

        if not self._config.dry_run:
            try:
                from solders.keypair import Keypair
                from solders.transaction import VersionedTransaction
                swap_tx = result.get("swapTransaction", "")
                tx_bytes = base64.b64decode(swap_tx)
                tx = VersionedTransaction.from_bytes(tx_bytes)
                kp = Keypair.from_base58_string(self._config.wallet_private_key)
                tx.sign([kp])
                signed_b64 = base64.b64encode(bytes(tx)).decode()
                await self._service.send_signed_transaction(signed_b64)
            except ImportError:
                return "Error: 'solders' required for live trading."
            except Exception as e:
                return f"Error signing sell tx: {e}"

        pos = positions[0]
        pnl_sol = sol_out - pos.amount_sol_in
        pnl_pct = (current_price - pos.entry_price_usd) / pos.entry_price_usd * 100 if pos.entry_price_usd else 0
        hold_min = (time.time() - pos.entry_time) / 60
        self._positions.close_position(token_address, current_price, sol_out, "closed")

        # Learn from this trade
        total_txns = (token.buy_count_5m + token.sell_count_5m) if token else 1
        buy_ratio = (token.buy_count_5m / total_txns * 100) if token and total_txns > 0 else 50
        score = _compute_trend_score(token) if token else 50
        self._memory.add_trade_review(
            token_address=token_address, symbol=pos.symbol, side="sell",
            pnl_sol=pnl_sol, pnl_pct=pnl_pct, trend_score=score,
            buy_ratio=buy_ratio, liquidity_usd=token.liquidity_usd if token else 0,
            hold_time_min=hold_min, reason="manual_sell",
        )

        dry_tag = "DRY RUN " if self._config.dry_run else ""
        return (
            f"{dry_tag}SELL EXECUTED:\n"
            f"  Token: {pos.symbol} ({token_address})\n"
            f"  SOL out: {sol_out:.4f} | PnL: {pnl_sol:+.4f} SOL ({pnl_pct:+.1f}%)\n"
            f"  Held: {hold_min:.0f} min"
        )

    # ------------------------------------------------------------------ #
    #  portfolio
    # ------------------------------------------------------------------ #

    async def _portfolio(self) -> str:
        balance = await self._service.get_sol_balance(self._wallet_pubkey)
        portfolio = await self._service.get_portfolio(self._wallet_pubkey)

        items = portfolio.get("items", [])
        fungible = [
            i for i in items
            if i.get("interface") == "FungibleToken"
            and i.get("token_info", {}).get("balance", 0) > 0
        ]

        lines = [
            f"Wallet: {self._wallet_pubkey[:8]}...{self._wallet_pubkey[-4:]}",
            f"SOL: {balance:.4f}",
            f"Tokens ({len(fungible)}):",
        ]

        for item in fungible[:25]:
            info = item.get("token_info", {})
            meta = item.get("content", {}).get("metadata", {})
            symbol = meta.get("symbol", "???")
            bal = info.get("balance", 0)
            decimals = info.get("decimals", 0)
            human = bal / (10 ** decimals) if decimals else bal
            price = info.get("price_info", {}).get("price_per_token", 0)
            value = human * price if price else 0
            lines.append(f"  {symbol}: {human:,.2f} (~${value:,.2f})")

        return "\n".join(lines)

    # ------------------------------------------------------------------ #
    #  positions
    # ------------------------------------------------------------------ #

    async def _positions_report(self) -> str:
        open_pos = self._positions.get_open_positions()
        if not open_pos:
            history = self._positions.get_trade_history(10)
            if history:
                lines = ["No open positions.\n\nRecent trades:"]
                for t in history:
                    pnl = t.get("pnl_sol")
                    pnl_str = f" ({pnl:+.4f} SOL)" if pnl is not None else ""
                    lines.append(f"  {t.get('action','?').upper()} {t.get('symbol','?')}{pnl_str}")
                lines.append("\n" + self._positions.format_stats())
                return "\n".join(lines)
            return "No positions and no trade history."

        addresses = [p.token_address for p in open_pos]
        try:
            tokens = await self._service.get_token_info(addresses)
            prices = {t.address: t.price_usd for t in tokens}
        except Exception:
            prices = {}

        report = self._positions.format_positions_report(prices)
        report += f"\n\nInvested: {self._positions.get_total_sol_invested():.3f} SOL"
        report += f" / Max: {self._config.risk.max_portfolio_sol} SOL"
        report += "\n" + self._positions.format_stats()
        return report

    # ------------------------------------------------------------------ #
    #  set_limits
    # ------------------------------------------------------------------ #

    async def _set_limits(
        self, token_address: str = "", stop_loss_pct: float = 0,
        take_profit_pct: float = 0, **_: Any,
    ) -> str:
        if not token_address:
            return "Error: token_address required"
        positions = self._positions.get_positions_for_token(token_address)
        if not positions:
            return f"No open position for {token_address}"

        pos = positions[0]
        if stop_loss_pct > 0:
            pos.stop_loss_price = pos.entry_price_usd * (1 - stop_loss_pct / 100)
        if take_profit_pct > 0:
            pos.take_profit_price = pos.entry_price_usd * (1 + take_profit_pct / 100)
        self._positions._save()

        return (
            f"Updated {pos.symbol}:\n"
            f"  SL: ${pos.stop_loss_price:.8f} | TP: ${pos.take_profit_price:.8f}"
        )

    # ------------------------------------------------------------------ #
    #  check_exits — now auto-closes in autonomous mode
    # ------------------------------------------------------------------ #

    async def _check_exits(self) -> str:
        open_pos = self._positions.get_open_positions()
        if not open_pos:
            return "No open positions to check."

        addresses = [p.token_address for p in open_pos]
        tokens = await self._service.get_token_info(addresses)
        prices = {t.address: t.price_usd for t in tokens}
        token_map = {t.address: t for t in tokens}

        triggered = self._positions.check_stop_loss_take_profit(prices)
        if not triggered:
            lines = ["All positions within limits.\n"]
            for p in open_pos:
                price = prices.get(p.token_address)
                if price:
                    pnl = (price - p.entry_price_usd) / p.entry_price_usd * 100 if p.entry_price_usd else 0
                    lines.append(f"  {p.symbol}: ${price:.8f} ({pnl:+.1f}%)")
            return "\n".join(lines)

        results: list[str] = []
        for pos, reason in triggered:
            current_price = prices.get(pos.token_address, 0)
            pnl_pct = (current_price - pos.entry_price_usd) / pos.entry_price_usd * 100 if pos.entry_price_usd else 0
            sol_out = pos.amount_sol_in * (1 + pnl_pct / 100)
            hold_min = (time.time() - pos.entry_time) / 60

            # Auto-close position (both dry_run and live)
            if self._config.dry_run:
                self._positions.close_position(pos.token_address, current_price, sol_out, reason)
            else:
                # In live mode, sell the tokens
                slippage = self._config.risk.max_slippage_bps
                try:
                    sell_result = await self._execute_sell(pos.token_address, pos.amount_tokens, slippage)
                    results.append(f"AUTO-SOLD: {pos.symbol} | {reason} | {sell_result.split(chr(10))[0]}")
                    continue
                except Exception as e:
                    results.append(f"AUTO-SELL FAILED: {pos.symbol} | {e}")
                    continue

            # Record in memory
            token = token_map.get(pos.token_address)
            total_txns = (token.buy_count_5m + token.sell_count_5m) if token else 1
            buy_ratio = (token.buy_count_5m / total_txns * 100) if token and total_txns > 0 else 50
            score = _compute_trend_score(token) if token else 50
            self._memory.add_trade_review(
                token_address=pos.token_address, symbol=pos.symbol, side="sell",
                pnl_sol=sol_out - pos.amount_sol_in, pnl_pct=pnl_pct, trend_score=score,
                buy_ratio=buy_ratio, liquidity_usd=token.liquidity_usd if token else 0,
                hold_time_min=hold_min, reason=reason,
            )

            dry_tag = "DRY RUN " if self._config.dry_run else ""
            results.append(
                f"{dry_tag}EXIT: {pos.symbol} | {reason} | {pnl_pct:+.1f}% | "
                f"~{sol_out:.4f} SOL | held {hold_min:.0f}m"
            )

        return "Exit Results:\n" + "\n".join(results)
