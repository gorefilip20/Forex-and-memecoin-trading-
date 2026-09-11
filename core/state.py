import json
from datetime import datetime, timezone
from typing import Optional

from config.settings import APPROVAL_TTL_SECONDS, MEMECOIN_COOLDOWN_MINUTES


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _dec(v):
    return v.decode() if isinstance(v, bytes) else v


def _f(v, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _ttl_expired(created_at: str) -> bool:
    try:
        dt = datetime.fromisoformat(created_at)
        return (datetime.now(timezone.utc) - dt).total_seconds() > APPROVAL_TTL_SECONDS
    except Exception:
        return True


# ── Memecoin State ─────────────────────────────────────────────────

async def load_memecoin_state(redis, ns):
    bal = await redis.get(f"{ns}:mc:balance_usd")
    balance = _f(bal, 0.0) if bal is not None else None
    raw_pos = await redis.hgetall(f"{ns}:mc:positions")
    positions = {_dec(k): json.loads(_dec(v)) for k, v in raw_pos.items()}
    return balance, positions


async def log_memecoin_trade(redis, ns, entry: dict):
    entry["market"] = "memecoin"
    await redis.lpush(f"{ns}:trade_log", json.dumps(entry))
    await redis.ltrim(f"{ns}:trade_log", 0, 499)


async def load_memecoin_pending(redis, ns) -> dict:
    raw = await redis.hgetall(f"{ns}:mc:pending")
    out = {}
    for aid, v in raw.items():
        p = json.loads(_dec(v))
        if not _ttl_expired(p.get("created_at", "")):
            out[_dec(aid)] = p
        else:
            await redis.hdel(f"{ns}:mc:pending", aid)
    return out


async def set_cooldown(redis, ns, mint: str):
    await redis.hset(f"{ns}:mc:cooldowns", mint, now_iso())


async def is_in_cooldown(redis, ns, mint: str) -> bool:
    ts = await redis.hget(f"{ns}:mc:cooldowns", mint)
    if not ts:
        return False
    try:
        dt = datetime.fromisoformat(_dec(ts))
        return (datetime.now(timezone.utc) - dt).total_seconds() < MEMECOIN_COOLDOWN_MINUTES * 60
    except Exception:
        return False


# ── Forex State ────────────────────────────────────────────────────

async def load_forex_state(redis, ns):
    bal = await redis.get(f"{ns}:fx:balance_usd")
    balance = _f(bal, 0.0) if bal is not None else None
    equity = _f(await redis.get(f"{ns}:fx:equity_usd"), 0.0)
    raw_pos = await redis.hgetall(f"{ns}:fx:positions")
    positions = {_dec(k): json.loads(_dec(v)) for k, v in raw_pos.items()}
    return balance, equity, positions


async def log_forex_trade(redis, ns, entry: dict):
    entry["market"] = "forex"
    await redis.lpush(f"{ns}:trade_log", json.dumps(entry))
    await redis.ltrim(f"{ns}:trade_log", 0, 499)


async def load_forex_pending(redis, ns) -> dict:
    raw = await redis.hgetall(f"{ns}:fx:pending")
    out = {}
    for aid, v in raw.items():
        p = json.loads(_dec(v))
        if not _ttl_expired(p.get("created_at", "")):
            out[_dec(aid)] = p
        else:
            await redis.hdel(f"{ns}:fx:pending", aid)
    return out


# ── Combined ───────────────────────────────────────────────────────

async def load_all_trades(redis, ns, limit: int = 100) -> list[dict]:
    raw = await redis.lrange(f"{ns}:trade_log", 0, limit - 1)
    return [json.loads(_dec(x)) for x in raw]


async def calculate_stats(redis, ns) -> dict:
    trades = await load_all_trades(redis, ns, 500)
    closed = [t for t in trades if t.get("type") in ("TAKE_PROFIT", "STOP_LOSS", "MANUAL_CLOSE", "TP_HIT", "SL_HIT")]
    if not closed:
        return {"total_trades": 0, "wins": 0, "losses": 0, "win_rate": 0.0, "total_pnl": 0.0}
    wins = [t for t in closed if _f(t.get("pnl_usd", 0)) > 0]
    losses = [t for t in closed if _f(t.get("pnl_usd", 0)) <= 0]
    total_pnl = sum(_f(t.get("pnl_usd", 0)) for t in closed)
    return {
        "total_trades": len(closed),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / len(closed) * 100, 1) if closed else 0.0,
        "total_pnl": round(total_pnl, 2),
    }
