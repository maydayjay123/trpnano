"""Strategy memory — the bot learns from its trades."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from loguru import logger


class StrategyMemory:
    """
    Persistent memory that tracks what works and what doesn't.

    The agent reads this before every trade decision to avoid
    repeating mistakes and double down on winning patterns.

    Stored as JSON at ~/.nanobot/solana_trading/strategy.json
    """

    def __init__(self, store_path: Path):
        self.store_path = store_path
        self._data: dict[str, Any] = {
            "lessons": [],          # What the bot has learned
            "avoid_tokens": [],     # Tokens/deployers to avoid
            "prefer_patterns": [],  # Patterns that work
            "session_notes": [],    # User guidance notes
            "trade_reviews": [],    # Post-trade analysis
        }
        self._loaded = False

    def _load(self) -> None:
        if self._loaded:
            return
        if self.store_path.exists():
            try:
                self._data = json.loads(self.store_path.read_text())
            except Exception as e:
                logger.warning(f"Failed to load strategy memory: {e}")
        self._loaded = True

    def _save(self) -> None:
        self.store_path.parent.mkdir(parents=True, exist_ok=True)
        # Keep memory bounded
        for key in ("lessons", "trade_reviews", "session_notes"):
            if key in self._data and len(self._data[key]) > 200:
                self._data[key] = self._data[key][-200:]
        self.store_path.write_text(json.dumps(self._data, indent=2))

    # ------------------------------------------------------------------ #
    #  Lessons — general patterns the bot discovers
    # ------------------------------------------------------------------ #

    def add_lesson(self, lesson: str, source: str = "auto") -> None:
        """Add a learned lesson (from trade outcome or user guidance)."""
        self._load()
        self._data["lessons"].append({
            "lesson": lesson,
            "source": source,
            "time": time.time(),
        })
        self._save()

    def get_lessons(self, limit: int = 20) -> list[str]:
        self._load()
        return [l["lesson"] for l in self._data["lessons"][-limit:]]

    # ------------------------------------------------------------------ #
    #  Avoid list — tokens/deployers that rugged or dumped
    # ------------------------------------------------------------------ #

    def add_avoid(self, address: str, reason: str) -> None:
        self._load()
        # Don't duplicate
        existing = {a["address"] for a in self._data["avoid_tokens"]}
        if address not in existing:
            self._data["avoid_tokens"].append({
                "address": address,
                "reason": reason,
                "time": time.time(),
            })
            self._save()

    def should_avoid(self, address: str) -> str | None:
        """Returns reason to avoid, or None if ok."""
        self._load()
        for entry in self._data["avoid_tokens"]:
            if entry["address"] == address:
                return entry["reason"]
        return None

    # ------------------------------------------------------------------ #
    #  Winning patterns
    # ------------------------------------------------------------------ #

    def add_pattern(self, pattern: str) -> None:
        self._load()
        if pattern not in self._data["prefer_patterns"]:
            self._data["prefer_patterns"].append(pattern)
            self._save()

    def get_patterns(self) -> list[str]:
        self._load()
        return self._data.get("prefer_patterns", [])

    # ------------------------------------------------------------------ #
    #  User guidance — things the user teaches the bot
    # ------------------------------------------------------------------ #

    def add_user_note(self, note: str) -> None:
        """Store guidance from the user."""
        self._load()
        self._data["session_notes"].append({
            "note": note,
            "time": time.time(),
        })
        self._save()

    def get_user_notes(self, limit: int = 10) -> list[str]:
        self._load()
        return [n["note"] for n in self._data["session_notes"][-limit:]]

    # ------------------------------------------------------------------ #
    #  Trade reviews — post-trade analysis
    # ------------------------------------------------------------------ #

    def add_trade_review(
        self,
        token_address: str,
        symbol: str,
        side: str,
        pnl_sol: float,
        pnl_pct: float,
        trend_score: int,
        buy_ratio: float,
        liquidity_usd: float,
        hold_time_min: float,
        reason: str,
    ) -> None:
        """Record a trade outcome for pattern analysis."""
        self._load()
        self._data["trade_reviews"].append({
            "token": token_address,
            "symbol": symbol,
            "side": side,
            "pnl_sol": round(pnl_sol, 6),
            "pnl_pct": round(pnl_pct, 2),
            "trend_score": trend_score,
            "buy_ratio": round(buy_ratio, 1),
            "liquidity_usd": round(liquidity_usd),
            "hold_time_min": round(hold_time_min, 1),
            "reason": reason,
            "time": time.time(),
        })
        self._save()

        # Auto-learn from outcomes
        self._auto_learn(pnl_pct, trend_score, buy_ratio, liquidity_usd, symbol, token_address, reason)

    def _auto_learn(
        self, pnl_pct: float, score: int, buy_ratio: float,
        liq: float, symbol: str, addr: str, reason: str,
    ) -> None:
        """Automatically extract lessons from trade outcomes."""
        if pnl_pct <= -30:
            self.add_lesson(
                f"Big loss on {symbol}: score={score}, buy_ratio={buy_ratio:.0f}%, liq=${liq:,.0f}. "
                f"Reason: {reason}. Be more cautious with similar setups.",
                source="auto_loss",
            )
            self.add_avoid(addr, f"Lost {pnl_pct:.0f}% — {reason}")

        elif pnl_pct >= 50:
            self.add_lesson(
                f"Big win on {symbol}: score={score}, buy_ratio={buy_ratio:.0f}%, liq=${liq:,.0f}. "
                f"Look for similar setups.",
                source="auto_win",
            )
            pattern = f"score>={score},buy_ratio>={buy_ratio:.0f}%,liq>=${liq:,.0f}"
            self.add_pattern(pattern)

    # ------------------------------------------------------------------ #
    #  Context for agent — what to include in decisions
    # ------------------------------------------------------------------ #

    def get_context_for_agent(self) -> str:
        """Build a context string the agent reads before making decisions."""
        self._load()
        parts: list[str] = []

        lessons = self.get_lessons(15)
        if lessons:
            parts.append("LEARNED LESSONS:")
            for l in lessons:
                parts.append(f"  - {l}")

        patterns = self.get_patterns()
        if patterns:
            parts.append("\nWINNING PATTERNS:")
            for p in patterns:
                parts.append(f"  - {p}")

        notes = self.get_user_notes(10)
        if notes:
            parts.append("\nUSER GUIDANCE:")
            for n in notes:
                parts.append(f"  - {n}")

        avoids = self._data.get("avoid_tokens", [])[-20:]
        if avoids:
            parts.append(f"\nAVOID LIST ({len(avoids)} tokens):")
            for a in avoids[-10:]:
                parts.append(f"  - {a['address'][:16]}... : {a['reason']}")

        # Win/loss summary from recent reviews
        reviews = self._data.get("trade_reviews", [])[-50:]
        if reviews:
            wins = [r for r in reviews if r["pnl_pct"] > 0]
            losses = [r for r in reviews if r["pnl_pct"] <= 0]
            avg_win = sum(r["pnl_pct"] for r in wins) / len(wins) if wins else 0
            avg_loss = sum(r["pnl_pct"] for r in losses) / len(losses) if losses else 0
            avg_score_win = sum(r["trend_score"] for r in wins) / len(wins) if wins else 0
            avg_score_loss = sum(r["trend_score"] for r in losses) / len(losses) if losses else 0

            parts.append(f"\nRECENT PERFORMANCE ({len(reviews)} trades):")
            parts.append(f"  Wins: {len(wins)} (avg +{avg_win:.1f}%, avg score {avg_score_win:.0f})")
            parts.append(f"  Losses: {len(losses)} (avg {avg_loss:.1f}%, avg score {avg_score_loss:.0f})")
            if avg_score_win > avg_score_loss:
                parts.append(f"  Insight: Higher scores ({avg_score_win:.0f}+) correlate with wins")

        if not parts:
            return "No trading memory yet. Start trading to build experience."

        return "\n".join(parts)
