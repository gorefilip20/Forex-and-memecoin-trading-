"""Deployment safety checks for paper, demo, and live configuration.

This module never connects to a broker. It only validates configuration shape and
returns safe-to-display diagnostics.
"""

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str


def _present(name: str) -> bool:
    return bool(os.environ.get(name, "").strip())


def _strong_secret(value: str) -> bool:
    return len(value) >= 32 and len(set(value)) >= 12


def readiness_checks() -> list[Check]:
    live = os.environ.get("FOREX_LIVE", "false").lower() == "true"
    auto = os.environ.get("AUTO_TRADE", "false").lower() == "true"
    unlock = os.environ.get("LIVE_TRADING_UNLOCK", "false").lower() == "true"
    acknowledge = os.environ.get("LIVE_RISK_ACK", "false").lower() == "true"
    practice = os.environ.get("OANDA_PRACTICE", "true").lower() == "true"
    checks = [
        Check("control_secret", _strong_secret(os.environ.get("CONTROL_API_SECRET", "")),
              "CONTROL_API_SECRET must be at least 32 characters with varied characters"),
        Check("mt5_secret", _strong_secret(os.environ.get("MT5_SECRET", "")),
              "MT5_SECRET must be at least 32 characters with varied characters"),
        Check("redis", _present("REDIS_URL"), "REDIS_URL is configured"),
        Check("paper_default", not live or not auto or not unlock,
              "live execution is not enabled by default"),
    ]
    if live or auto or unlock:
        checks.extend([
            Check("explicit_risk_ack", acknowledge,
                  "set LIVE_RISK_ACK=true only after independent demo review"),
            Check("broker_credentials", _present("OANDA_API_KEY") and _present("OANDA_ACCOUNT_ID"),
                  "OANDA_API_KEY and OANDA_ACCOUNT_ID are required for OANDA live mode"),
        ])
    if live and practice:
        checks.append(Check("practice_mode", False,
                            "OANDA_PRACTICE must be false for live OANDA mode"))
    if live and os.environ.get("APPROVE_FIRST", "false").lower() != "true":
        checks.append(Check("approval_gate", False,
                            "APPROVE_FIRST=true is required for the first live deployment"))
    if live:
        try:
            daily_limit = float(os.environ.get("DAILY_MAX_DRAWDOWN_PCT", "0"))
        except ValueError:
            daily_limit = 0
        checks.append(Check("daily_loss_limit", 0 < daily_limit <= 1.0,
                            "DAILY_MAX_DRAWDOWN_PCT must be between 0 and 1 for live mode"))
    return checks


def readiness_report() -> dict:
    checks = readiness_checks()
    live_intent = any(os.environ.get(k, "").lower() == "true" for k in (
        "FOREX_LIVE", "MEMECOIN_LIVE", "AUTO_TRADE", "LIVE_TRADING_UNLOCK"))
    return {
        "ready": (not live_intent) or all(c.ok for c in checks),
        "live_intent_detected": live_intent,
        "checks": [{"name": c.name, "ok": c.ok, "detail": c.detail} for c in checks],
    }
