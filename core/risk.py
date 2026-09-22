"""Risk management: daily drawdown limits, pair correlation filter, session-aware trading."""

import json
import logging
from datetime import datetime, timezone

from core.state import _f, _dec

logger = logging.getLogger("trading_bot")

CORRELATED_GROUPS = [
    {"EUR/USD", "GBP/USD", "EUR/GBP"},
    {"AUD/USD", "NZD/USD"},
    {"USD/CHF", "USD/CAD"},
    {"EUR/JPY", "GBP/JPY", "AUD/JPY"},
]

INVERSE_PAIRS = {
    ("EUR/USD", "USD/CHF"),
    ("GBP/USD", "USD/CHF"),
}

FOREX_SESSIONS = {
    "sydney":     (22, 7),
    "tokyo":      (0, 9),
    "london":     (8, 17),
    "new_york":   (13, 22),
    "london_ny":  (13, 17),
}

PAIR_BEST_SESSIONS = {
    "EUR/USD": ["london", "london_ny", "new_york"],
    "GBP/USD": ["london", "london_ny", "new_york"],
    "USD/JPY": ["tokyo", "london", "london_ny"],
    "EUR/JPY": ["tokyo", "london"],
    "GBP/JPY": ["london", "london_ny"],
    "AUD/USD": ["sydney", "tokyo", "london"],
    "NZD/USD": ["sydney", "tokyo"],
    "USD/CAD": ["new_york", "london_ny"],
    "USD/CHF": ["london", "london_ny"],
    "EUR/GBP": ["london"],
    "XAU/USD": ["london", "london_ny", "new_york"],
    "XAG/USD": ["london", "london_ny", "new_york"],
    "BTC/USD": ["london_ny", "new_york"],
    "ETH/USD": ["london_ny", "new_york"],
}


async def check_daily_drawdown(redis, ns: str, max_drawdown_pct: float = 3.0) -> dict:
    """Check if daily P&L has breached the drawdown limit."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    raw = await redis.lrange(f"{ns}:trade_log", 0, 499)

    daily_pnl = 0.0
    trade_count = 0
    for entry in raw:
        trade = json.loads(_dec(entry))
        if trade.get("type") not in ("TAKE_PROFIT", "STOP_LOSS", "TP_HIT", "SL_HIT", "JEV_EXIT", "TRAIL_STOP", "MANUAL_CLOSE", "LADDER_EXIT"):
            continue
        ts = trade.get("timestamp", trade.get("closed_at", ""))
        if today not in ts and not ts.startswith(today):
            continue
        daily_pnl += _f(trade.get("pnl_usd", 0))
        trade_count += 1

    fx_bal = _f(await redis.get(f"{ns}:fx:balance_usd"), 10000)
    mc_bal = _f(await redis.get(f"{ns}:mc:balance_usd"), 1000)
    total_balance = fx_bal + mc_bal
    drawdown_pct = abs(daily_pnl) / total_balance * 100 if total_balance > 0 and daily_pnl < 0 else 0

    breached = drawdown_pct >= max_drawdown_pct
    if breached:
        logger.warning(f"Daily drawdown limit breached: {drawdown_pct:.1f}% (limit: {max_drawdown_pct}%)")

    return {
        "daily_pnl": round(daily_pnl, 2),
        "drawdown_pct": round(drawdown_pct, 2),
        "max_drawdown_pct": max_drawdown_pct,
        "breached": breached,
        "trades_today": trade_count,
    }


def check_correlation(pair: str, direction: str, open_positions: dict) -> dict:
    """Check if opening this pair would create dangerous correlation with existing positions."""
    open_pairs = {}
    for pos in open_positions.values():
        p = pos.get("pair", "")
        d = pos.get("direction", "")
        if p:
            open_pairs[p] = d

    if not open_pairs:
        return {"ok": True, "reason": ""}

    for group in CORRELATED_GROUPS:
        if pair not in group:
            continue
        for open_pair, open_dir in open_pairs.items():
            if open_pair in group and open_dir == direction:
                return {
                    "ok": False,
                    "reason": f"{pair} {direction} blocked: correlated with open {open_pair} {open_dir}",
                    "correlated_with": open_pair,
                }

    for p1, p2 in INVERSE_PAIRS:
        if pair == p1:
            other = p2
        elif pair == p2:
            other = p1
        else:
            continue
        if other in open_pairs:
            open_dir = open_pairs[other]
            if (direction == "BUY" and open_dir == "SELL") or (direction == "SELL" and open_dir == "BUY"):
                return {
                    "ok": False,
                    "reason": f"{pair} {direction} blocked: inverse correlation with {other} {open_dir} (same bet)",
                    "correlated_with": other,
                }

    return {"ok": True, "reason": ""}


def get_active_sessions() -> list[str]:
    """Get currently active forex trading sessions based on UTC hour."""
    hour = datetime.now(timezone.utc).hour
    active = []
    for session, (start, end) in FOREX_SESSIONS.items():
        if start <= end:
            if start <= hour < end:
                active.append(session)
        else:
            if hour >= start or hour < end:
                active.append(session)
    return active


def is_good_session_for_pair(pair: str) -> dict:
    """Check if the current session is optimal for trading this pair."""
    active = get_active_sessions()
    best = PAIR_BEST_SESSIONS.get(pair, [])

    if not best:
        return {"optimal": True, "active_sessions": active, "reason": "No session preference for this pair"}

    overlap = [s for s in active if s in best]
    if overlap:
        return {
            "optimal": True,
            "active_sessions": active,
            "matched_sessions": overlap,
            "reason": f"Good timing: {', '.join(overlap)} session active",
        }

    return {
        "optimal": False,
        "active_sessions": active,
        "best_sessions": best,
        "reason": f"Suboptimal: {pair} trades best during {', '.join(best)}, currently in {', '.join(active) or 'off-hours'}",
    }


async def calculate_analytics(redis, ns: str) -> dict:
    """Calculate detailed trading performance analytics."""
    raw = await redis.lrange(f"{ns}:trade_log", 0, 999)
    trades = [json.loads(_dec(x)) for x in raw]
    closed = [
        t for t in trades
        if t.get("type") in ("TAKE_PROFIT", "STOP_LOSS", "TP_HIT", "SL_HIT", "JEV_EXIT", "TRAIL_STOP", "MANUAL_CLOSE", "LADDER_EXIT")
    ]

    if not closed:
        return {
            "total_trades": 0, "win_rate": 0, "profit_factor": 0,
            "expectancy": 0, "max_drawdown": 0, "sharpe_estimate": 0,
            "avg_win": 0, "avg_loss": 0, "largest_win": 0, "largest_loss": 0,
            "by_pair": {}, "by_type": {},
        }

    pnls = [_f(t.get("pnl_usd", 0)) for t in closed]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]

    total_wins = sum(wins)
    total_losses = abs(sum(losses))
    profit_factor = total_wins / total_losses if total_losses > 0 else float("inf") if total_wins > 0 else 0

    avg_win = total_wins / len(wins) if wins else 0
    avg_loss = total_losses / len(losses) if losses else 0
    win_rate = len(wins) / len(closed) * 100

    expectancy = (win_rate / 100 * avg_win) - ((1 - win_rate / 100) * avg_loss)

    running_pnl = 0.0
    peak = 0.0
    max_dd = 0.0
    for p in pnls:
        running_pnl += p
        if running_pnl > peak:
            peak = running_pnl
        dd = peak - running_pnl
        if dd > max_dd:
            max_dd = dd

    import math
    mean_pnl = sum(pnls) / len(pnls)
    variance = sum((p - mean_pnl) ** 2 for p in pnls) / len(pnls)
    std_pnl = math.sqrt(variance) if variance > 0 else 0
    sharpe = (mean_pnl / std_pnl) * math.sqrt(252) if std_pnl > 0 else 0

    by_pair = {}
    for t in closed:
        pair = t.get("pair", t.get("symbol", "unknown"))
        if pair not in by_pair:
            by_pair[pair] = {"trades": 0, "wins": 0, "pnl": 0.0}
        by_pair[pair]["trades"] += 1
        pnl = _f(t.get("pnl_usd", 0))
        by_pair[pair]["pnl"] += pnl
        if pnl > 0:
            by_pair[pair]["wins"] += 1

    for pair, stats in by_pair.items():
        stats["pnl"] = round(stats["pnl"], 2)
        stats["win_rate"] = round(stats["wins"] / stats["trades"] * 100, 1) if stats["trades"] > 0 else 0

    by_type = {}
    for t in closed:
        exit_type = t.get("type", "unknown")
        if exit_type not in by_type:
            by_type[exit_type] = {"count": 0, "pnl": 0.0}
        by_type[exit_type]["count"] += 1
        by_type[exit_type]["pnl"] += _f(t.get("pnl_usd", 0))
    for stats in by_type.values():
        stats["pnl"] = round(stats["pnl"], 2)

    return {
        "total_trades": len(closed),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(win_rate, 1),
        "profit_factor": round(profit_factor, 2),
        "expectancy": round(expectancy, 2),
        "sharpe_estimate": round(sharpe, 2),
        "total_pnl": round(sum(pnls), 2),
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "largest_win": round(max(pnls), 2) if pnls else 0,
        "largest_loss": round(min(pnls), 2) if pnls else 0,
        "max_drawdown": round(max_dd, 2),
        "by_pair": by_pair,
        "by_exit_type": by_type,
    }
