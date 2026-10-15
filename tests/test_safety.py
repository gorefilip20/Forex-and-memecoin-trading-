import asyncio
import json
from datetime import datetime, timezone

from core.risk import check_daily_drawdown
from core.readiness import readiness_report
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


def test_default_configuration_is_not_live(monkeypatch):
    for key in (
        "FOREX_LIVE", "MEMECOIN_LIVE", "AUTO_TRADE", "LIVE_TRADING_UNLOCK",
        "LIVE_RISK_ACK", "OANDA_API_KEY", "OANDA_ACCOUNT_ID",
    ):
        monkeypatch.delenv(key, raising=False)
    report = readiness_report()
    assert report["live_intent_detected"] is False
    assert report["ready"] is True  # locked paper mode is safe to run
    assert any(c["name"] == "paper_default" and c["ok"] for c in report["checks"])


def test_live_configuration_requires_explicit_ack_and_approval(monkeypatch):
    monkeypatch.setenv("FOREX_LIVE", "true")
    monkeypatch.setenv("AUTO_TRADE", "true")
    monkeypatch.setenv("LIVE_TRADING_UNLOCK", "true")
    monkeypatch.setenv("OANDA_PRACTICE", "false")
    monkeypatch.setenv("CONTROL_API_SECRET", "A" * 40)
    monkeypatch.setenv("MT5_SECRET", "B" * 40)
    monkeypatch.setenv("OANDA_API_KEY", "key")
    monkeypatch.setenv("OANDA_ACCOUNT_ID", "account")
    monkeypatch.setenv("APPROVE_FIRST", "false")
    monkeypatch.setenv("DAILY_MAX_DRAWDOWN_PCT", "3")
    monkeypatch.setenv("LIVE_RISK_ACK", "false")
    report = readiness_report()
    failed = {c["name"] for c in report["checks"] if not c["ok"]}
    assert {"explicit_risk_ack", "approval_gate", "daily_loss_limit"} <= failed
