"""Position tracking, P&L management, and smart compounding for Solana trading."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from loguru import logger


@dataclass
class Position:
    """An open or closed trading position."""
    token_address: str
    symbol: str
    entry_price_usd: float
    amount_tokens: float
    amount_sol_in: float
    entry_time: float
    stop_loss_price: float
    take_profit_price: float
    status: str = "open"  # open, closed, stopped_out, took_profit


class PositionManager:
    """Manages open/closed positions with JSON persistence and compounding stats."""

    def __init__(self, store_path: Path):
        self.store_path = store_path
        self._positions: list[Position] = []
        self._trade_log: list[dict[str, Any]] = []
        self._stats: dict[str, Any] = {"total_pnl_sol": 0.0, "wins": 0, "losses": 0}
        self._loaded = False

    def _load(self) -> None:
        if self._loaded:
            return
        if self.store_path.exists():
            try:
                data = json.loads(self.store_path.read_text())
                for p in data.get("positions", []):
                    self._positions.append(Position(**p))
                self._trade_log = data.get("trade_log", [])
                self._stats = data.get("stats", self._stats)
            except Exception as e:
                logger.warning(f"Failed to load positions: {e}")
        self._loaded = True

    def _save(self) -> None:
        self.store_path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "positions": [
                {
                    "token_address": p.token_address,
                    "symbol": p.symbol,
                    "entry_price_usd": p.entry_price_usd,
                    "amount_tokens": p.amount_tokens,
                    "amount_sol_in": p.amount_sol_in,
                    "entry_time": p.entry_time,
                    "stop_loss_price": p.stop_loss_price,
                    "take_profit_price": p.take_profit_price,
                    "status": p.status,
                }
                for p in self._positions
            ],
            "trade_log": self._trade_log[-500:],
            "stats": self._stats,
        }
        self.store_path.write_text(json.dumps(data, indent=2))

    # ------------------------------------------------------------------ #
    #  Position lifecycle
    # ------------------------------------------------------------------ #

    def open_position(
        self,
        token_address: str,
        symbol: str,
        entry_price_usd: float,
        amount_tokens: float,
        amount_sol_in: float,
        stop_loss_pct: float,
        take_profit_pct: float,
    ) -> Position:
        self._load()
        pos = Position(
            token_address=token_address,
            symbol=symbol,
            entry_price_usd=entry_price_usd,
            amount_tokens=amount_tokens,
            amount_sol_in=amount_sol_in,
            entry_time=time.time(),
            stop_loss_price=entry_price_usd * (1 - stop_loss_pct / 100),
            take_profit_price=entry_price_usd * (1 + take_profit_pct / 100),
        )
        self._positions.append(pos)
        self._trade_log.append({
            "action": "buy",
            "token": token_address,
            "symbol": symbol,
            "price": entry_price_usd,
            "sol_amount": amount_sol_in,
            "time": time.time(),
        })
        self._save()
        return pos

    def close_position(
        self, token_address: str, exit_price: float, sol_out: float, reason: str,
    ) -> Position | None:
        self._load()
        for pos in self._positions:
            if pos.token_address == token_address and pos.status == "open":
                pos.status = reason
                pnl_sol = sol_out - pos.amount_sol_in
                pnl_pct = (exit_price - pos.entry_price_usd) / pos.entry_price_usd * 100 if pos.entry_price_usd else 0

                self._stats["total_pnl_sol"] = round(self._stats.get("total_pnl_sol", 0) + pnl_sol, 6)
                if pnl_sol >= 0:
                    self._stats["wins"] = self._stats.get("wins", 0) + 1
                else:
                    self._stats["losses"] = self._stats.get("losses", 0) + 1

                self._trade_log.append({
                    "action": "sell",
                    "token": token_address,
                    "symbol": pos.symbol,
                    "entry_price": pos.entry_price_usd,
                    "exit_price": exit_price,
                    "sol_in": pos.amount_sol_in,
                    "sol_out": sol_out,
                    "pnl_sol": round(pnl_sol, 6),
                    "pnl_pct": round(pnl_pct, 2),
                    "reason": reason,
                    "time": time.time(),
                })
                self._save()
                return pos
        return None

    # ------------------------------------------------------------------ #
    #  Queries
    # ------------------------------------------------------------------ #

    def get_open_positions(self) -> list[Position]:
        self._load()
        return [p for p in self._positions if p.status == "open"]

    def get_total_sol_invested(self) -> float:
        return sum(p.amount_sol_in for p in self.get_open_positions())

    def get_positions_for_token(self, token_address: str) -> list[Position]:
        self._load()
        return [p for p in self._positions if p.token_address == token_address and p.status == "open"]

    def get_last_trade_time(self, token_address: str) -> float | None:
        self._load()
        for entry in reversed(self._trade_log):
            if entry.get("token") == token_address:
                return entry.get("time")
        return None

    def get_trade_history(self, limit: int = 20) -> list[dict[str, Any]]:
        self._load()
        return self._trade_log[-limit:]

    def get_stats(self) -> dict[str, Any]:
        self._load()
        total = self._stats.get("wins", 0) + self._stats.get("losses", 0)
        win_rate = (self._stats["wins"] / total * 100) if total > 0 else 0
        return {
            **self._stats,
            "total_trades": total,
            "win_rate_pct": round(win_rate, 1),
        }

    # ------------------------------------------------------------------ #
    #  Smart compounding — suggest next trade size based on performance
    # ------------------------------------------------------------------ #

    def suggest_trade_size(self, base_sol: float, sol_balance: float) -> float:
        """Scale trade size based on win rate. Good performance → bigger bets, capped at base * 2."""
        stats = self.get_stats()
        total = stats["total_trades"]
        if total < 5:
            return base_sol  # Not enough data, use base size

        win_rate = stats["win_rate_pct"]
        if win_rate >= 70:
            multiplier = 1.5
        elif win_rate >= 55:
            multiplier = 1.2
        elif win_rate < 40:
            multiplier = 0.5  # Scale down on losing streak
        else:
            multiplier = 1.0

        suggested = min(base_sol * multiplier, base_sol * 2, sol_balance * 0.1)
        return round(max(suggested, 0.01), 4)

    # ------------------------------------------------------------------ #
    #  Stop-loss / take-profit checking
    # ------------------------------------------------------------------ #

    def check_stop_loss_take_profit(self, current_prices: dict[str, float]) -> list[tuple[Position, str]]:
        """Check all open positions against current prices. Returns (position, reason) pairs."""
        triggered: list[tuple[Position, str]] = []
        for pos in self.get_open_positions():
            price = current_prices.get(pos.token_address)
            if price is None:
                continue
            if price <= pos.stop_loss_price:
                triggered.append((pos, "stopped_out"))
            elif price >= pos.take_profit_price:
                triggered.append((pos, "took_profit"))
        return triggered

    # ------------------------------------------------------------------ #
    #  Formatting
    # ------------------------------------------------------------------ #

    def format_positions_report(self, current_prices: dict[str, float] | None = None) -> str:
        positions = self.get_open_positions()
        if not positions:
            return "No open positions."

        lines = [f"Open Positions ({len(positions)}):"]
        for p in positions:
            pnl = ""
            if current_prices and p.token_address in current_prices:
                curr = current_prices[p.token_address]
                pnl_pct = (curr - p.entry_price_usd) / p.entry_price_usd * 100 if p.entry_price_usd else 0
                pnl = f" | Now: ${curr:.8f} ({pnl_pct:+.1f}%)"
            age_min = int((time.time() - p.entry_time) / 60)
            lines.append(
                f"  {p.symbol}: {p.amount_sol_in:.3f} SOL @ ${p.entry_price_usd:.8f}"
                f" | SL: ${p.stop_loss_price:.8f} | TP: ${p.take_profit_price:.8f}{pnl}"
                f" | {age_min}m ago"
            )
        return "\n".join(lines)

    def format_stats(self) -> str:
        s = self.get_stats()
        return (
            f"Trading Stats:\n"
            f"  Trades: {s['total_trades']} | Wins: {s['wins']} | Losses: {s['losses']}\n"
            f"  Win Rate: {s['win_rate_pct']}%\n"
            f"  Total PnL: {s['total_pnl_sol']:+.4f} SOL"
        )
