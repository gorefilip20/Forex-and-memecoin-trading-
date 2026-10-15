"""Backtest acceptance checks; these do not claim future profitability."""
from dataclasses import dataclass

@dataclass(frozen=True)
class ValidationResult:
    accepted: bool
    reasons: tuple[str, ...]


def stress_test(pnls: list[float], extra_cost_per_trade: float = 0.0,
                max_drawdown_pct: float = 20.0, starting_equity: float = 10000.0) -> ValidationResult:
    if not pnls or starting_equity <= 0:
        return ValidationResult(False, ("insufficient trades or invalid equity",))
    adjusted = [float(p) - extra_cost_per_trade for p in pnls]
    equity = peak = starting_equity
    max_dd = 0.0
    for pnl in adjusted:
        equity += pnl
        peak = max(peak, equity)
        max_dd = max(max_dd, (peak - equity) / peak * 100)
    reasons = []
    if sum(adjusted) <= 0: reasons.append("net P&L is not positive after stress costs")
    if max_dd > max_drawdown_pct: reasons.append("stress drawdown exceeds limit")
    if len(adjusted) < 100: reasons.append("fewer than 100 trades in validation sample")
    return ValidationResult(not reasons, tuple(reasons))
