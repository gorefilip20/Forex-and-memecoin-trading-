"""Deterministic risk controls. AI signals are advisory; this module is authoritative."""
from dataclasses import dataclass
from math import floor, isfinite

@dataclass(frozen=True)
class RiskDecision:
    allowed: bool
    reason: str
    volume: float = 0.0


def size_from_tick_risk(equity: float, risk_pct: float, stop_ticks: float,
                        tick_value_per_unit: float, min_volume: float,
                        max_volume: float, volume_step: float) -> RiskDecision:
    values = (equity, risk_pct, stop_ticks, tick_value_per_unit, min_volume, max_volume, volume_step)
    if not all(isfinite(float(v)) for v in values) or equity <= 0 or risk_pct <= 0:
        return RiskDecision(False, "invalid sizing inputs")
    if stop_ticks <= 0 or tick_value_per_unit <= 0 or volume_step <= 0:
        return RiskDecision(False, "stop distance and tick value must be positive")
    if min_volume <= 0 or max_volume < min_volume:
        return RiskDecision(False, "invalid venue volume limits")
    raw = equity * risk_pct / 100 / (stop_ticks * tick_value_per_unit)
    stepped = floor((raw / volume_step) + 1e-9) * volume_step
    if stepped < min_volume:
        return RiskDecision(False, "risk budget is below venue minimum volume")
    return RiskDecision(True, "ok", round(min(stepped, max_volume), 8))


def validate_signal(signal: dict, spread_ticks: float, max_spread_ticks: float,
                    stale_seconds: float, max_stale_seconds: float,
                    daily_loss_pct: float, max_daily_loss_pct: float,
                    open_risk_pct: float, max_total_risk_pct: float) -> RiskDecision:
    direction = str(signal.get("direction", "")).upper()
    entry, stop, target = (signal.get(k) for k in ("entry_price", "stop_loss", "take_profit"))
    if direction not in {"BUY", "SELL"}:
        return RiskDecision(False, "invalid direction")
    if not all(isfinite(float(v)) for v in (entry, stop, target)) or entry <= 0:
        return RiskDecision(False, "invalid prices")
    if direction == "BUY" and not (stop < entry < target):
        return RiskDecision(False, "BUY requires stop < entry < target")
    if direction == "SELL" and not (target < entry < stop):
        return RiskDecision(False, "SELL requires target < entry < stop")
    if spread_ticks > max_spread_ticks:
        return RiskDecision(False, "spread limit exceeded")
    if stale_seconds > max_stale_seconds:
        return RiskDecision(False, "market data is stale")
    if daily_loss_pct >= max_daily_loss_pct:
        return RiskDecision(False, "daily loss limit reached")
    if open_risk_pct >= max_total_risk_pct:
        return RiskDecision(False, "portfolio risk limit reached")
    return RiskDecision(True, "ok")
