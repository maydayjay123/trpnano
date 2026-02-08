"""Pre-trade safety checks for Solana meme coin trading."""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from nanobot.agent.tools.solana_trading.service import TokenInfo, TradingService
from nanobot.agent.tools.solana_trading.positions import PositionManager
from nanobot.config.schema import SolanaRiskLimits


@dataclass
class SafetyResult:
    safe: bool
    reasons: list[str] = field(default_factory=list)
    score: int = 0  # 0-100 safety score

    def __str__(self) -> str:
        if self.safe:
            return f"PASS (score: {self.score}/100)"
        return "BLOCKED: " + "; ".join(self.reasons)


def _compute_trend_score(token: TokenInfo) -> int:
    """Score a token 0-100 on trend strength. Higher = stronger buy signal."""
    score = 50  # Baseline

    # Buy pressure (5m buys vs sells)
    total_txns = token.buy_count_5m + token.sell_count_5m
    if total_txns > 0:
        buy_ratio = token.buy_count_5m / total_txns
        if buy_ratio >= 0.7:
            score += 20
        elif buy_ratio >= 0.6:
            score += 10
        elif buy_ratio < 0.4:
            score -= 15

    # Price momentum
    if token.price_change_5m > 5:
        score += 10
    elif token.price_change_5m < -5:
        score -= 10

    if token.price_change_1h > 10:
        score += 10
    elif token.price_change_1h < -10:
        score -= 10

    # Liquidity depth
    if token.liquidity_usd > 200_000:
        score += 10
    elif token.liquidity_usd > 100_000:
        score += 5

    # Volume relative to liquidity (velocity)
    if token.liquidity_usd > 0:
        velocity = token.volume_24h / token.liquidity_usd
        if velocity > 5:
            score += 5  # High turnover
        elif velocity < 0.5:
            score -= 5  # Dead volume

    return max(0, min(100, score))


async def check_token_safety(
    token: TokenInfo,
    risk: SolanaRiskLimits,
    positions: PositionManager,
    service: TradingService,
    amount_sol: float,
) -> SafetyResult:
    """Run all safety checks before allowing a buy."""
    reasons: list[str] = []

    # 1. Liquidity
    if token.liquidity_usd < risk.min_liquidity_usd:
        reasons.append(f"Liquidity ${token.liquidity_usd:,.0f} < min ${risk.min_liquidity_usd:,.0f}")

    # 2. Volume
    if token.volume_24h < risk.min_volume_24h_usd:
        reasons.append(f"24h vol ${token.volume_24h:,.0f} < min ${risk.min_volume_24h_usd:,.0f}")

    # 3. Position size
    if amount_sol > risk.max_position_sol:
        reasons.append(f"Position {amount_sol} SOL > max {risk.max_position_sol} SOL")

    # 4. Portfolio exposure
    invested = positions.get_total_sol_invested()
    if invested + amount_sol > risk.max_portfolio_sol:
        reasons.append(f"Portfolio would be {invested + amount_sol:.2f} SOL > max {risk.max_portfolio_sol} SOL")

    # 5. Cooldown
    last_trade = positions.get_last_trade_time(token.address)
    if last_trade and (time.time() - last_trade) < risk.cooldown_seconds:
        remaining = int(risk.cooldown_seconds - (time.time() - last_trade))
        reasons.append(f"Cooldown: {remaining}s remaining for {token.symbol}")

    # 6. Duplicate position
    if positions.get_positions_for_token(token.address):
        reasons.append(f"Already have open position for {token.symbol}")

    # 7. Holder distribution (RPC call)
    try:
        holders = await service.get_token_holders(token.address)
        if holders["holder_count"] < risk.min_holder_count:
            reasons.append(f"Only {holders['holder_count']} holders < min {risk.min_holder_count}")
        if holders["top_holder_pct"] > risk.max_top_holder_pct:
            reasons.append(f"Top holder {holders['top_holder_pct']:.1f}% > max {risk.max_top_holder_pct}%")
    except Exception as e:
        reasons.append(f"Holder check failed: {e}")

    score = _compute_trend_score(token)

    return SafetyResult(safe=len(reasons) == 0, reasons=reasons, score=score)
