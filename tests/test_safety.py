import asyncio
import json
from datetime import datetime, timezone

from core.risk import check_daily_drawdown
from core.state import log_forex_trade


class FakeRedis:
    def __init__(self):
        self.values = {}
        self.lists = {}
        self.hashes = {}

    async def get(self, key):
        return self.values.get(key)

    async def set(self, key, value):
        self.values[key] = str(value)

    async def lpush(self, key, value):
        self.lists.setdefault(key, []).insert(0, value)

    async def lrange(self, key, start, end):
        values = self.lists.get(key, [])
        return values[start:] if end == -1 else values[start:end + 1]

    async def ltrim(self, key, start, end):
        self.lists[key] = self.lists.get(key, [])[start:end + 1]

    async def hset(self, key, field, value):
        self.hashes.setdefault(key, {})[field] = value


def test_trade_log_adds_timestamp_and_event_id():
    async def run():
        r = FakeRedis()
        await log_forex_trade(r, "bot", {"type": "ENTRY", "pair": "EUR/USD"})
        event = json.loads(r.lists["bot:trade_log"][0])
        assert event["market"] == "forex"
        assert event["event_id"]
        datetime.fromisoformat(event["timestamp"])
    asyncio.run(run())


def test_daily_drawdown_trips_from_timestamped_loss_and_persists_baseline():
    async def run():
        r = FakeRedis()
        await r.set("bot:fx:balance_usd", "1000")
        await r.set("bot:mc:balance_usd", "0")
        today = datetime.now(timezone.utc).isoformat()
        await r.lpush("bot:trade_log", json.dumps({
            "type": "SL_HIT", "timestamp": today, "pnl_usd": -40,
        }))
        result = await check_daily_drawdown(r, "bot", 3.0)
        assert result["breached"] is True
        assert result["drawdown_pct"] == 4.0
        assert await r.get("bot:risk:daily_baseline:" + today[:10]) == "1000.0"
    asyncio.run(run())


def test_daily_drawdown_ignores_old_loss():
    async def run():
        r = FakeRedis()
        await r.set("bot:fx:balance_usd", "1000")
        await r.set("bot:mc:balance_usd", "0")
        await r.lpush("bot:trade_log", json.dumps({
            "type": "SL_HIT", "timestamp": "2020-01-01T00:00:00+00:00", "pnl_usd": -500,
        }))
        result = await check_daily_drawdown(r, "bot", 3.0)
        assert result["daily_pnl"] == 0
        assert result["breached"] is False
    asyncio.run(run())
