"""Equity curve tracking with auto-pause on losing streaks.

Tracks consecutive wins/losses in Redis. When the bot hits a configurable
number of consecutive losses (default 5), it switches to signal-only mode
(still scans and notifies, but won't open new positions). Resumes after
a configurable number of paper wins in a row (default 2).
"""

import json
import logging
import os

from core.state import _f, _dec

logger = logging.getLogger("trading_bot")


def _max_consecutive_losses() -> int:
    return int(os.environ.get("MAX_CONSECUTIVE_LOSSES", "5"))


def _wins_to_resume() -> int:
    return int(os.environ.get("WINS_TO_RESUME", "2"))


async def record_trade_result(redis, ns: str, pnl: float) -> dict:
    """Record a trade result and update the streak tracker.

    Returns the current streak state.
    """
    key = f"{ns}:equity_tracker"
    raw = await redis.get(key)

    state = {"consecutive_losses": 0, "consecutive_wins": 0, "paused": False, "recovery_wins": 0}
    if raw:
        try:
            state = json.loads(_dec(raw))
        except (json.JSONDecodeError, TypeError):
            pass

    if pnl > 0:
        state["consecutive_wins"] = state.get("consecutive_wins", 0) + 1
        state["consecutive_losses"] = 0

        if state.get("paused"):
            state["recovery_wins"] = state.get("recovery_wins", 0) + 1
            if state["recovery_wins"] >= _wins_to_resume():
                state["paused"] = False
                state["recovery_wins"] = 0
                logger.info("Equity tracker: resuming after recovery wins")
    else:
        state["consecutive_losses"] = state.get("consecutive_losses", 0) + 1
        state["consecutive_wins"] = 0
        state["recovery_wins"] = 0

        if state["consecutive_losses"] >= _max_consecutive_losses() and not state.get("paused"):
            state["paused"] = True
            logger.warning(
                f"Equity tracker: PAUSED after {state['consecutive_losses']} consecutive losses"
            )

    await redis.set(key, json.dumps(state))
    return state


async def get_equity_state(redis, ns: str) -> dict:
    """Get current equity tracker state."""
    key = f"{ns}:equity_tracker"
    raw = await redis.get(key)
    if raw:
        try:
            return json.loads(_dec(raw))
        except (json.JSONDecodeError, TypeError):
            pass
    return {"consecutive_losses": 0, "consecutive_wins": 0, "paused": False, "recovery_wins": 0}


async def is_trading_paused(redis, ns: str) -> dict:
    """Check if trading is paused due to losing streak.

    Returns:
        {"paused": bool, "reason": str, "consecutive_losses": int, "recovery_wins": int}
    """
    state = await get_equity_state(redis, ns)
    paused = state.get("paused", False)

    if paused:
        recovery = state.get("recovery_wins", 0)
        needed = _wins_to_resume()
        return {
            "paused": True,
            "reason": (
                f"Paused after {state['consecutive_losses']} consecutive losses. "
                f"Recovery: {recovery}/{needed} wins needed to resume."
            ),
            "consecutive_losses": state.get("consecutive_losses", 0),
            "recovery_wins": recovery,
        }

    return {
        "paused": False,
        "reason": "",
        "consecutive_losses": state.get("consecutive_losses", 0),
        "consecutive_wins": state.get("consecutive_wins", 0),
    }


async def reset_equity_tracker(redis, ns: str) -> None:
    """Reset the equity tracker (used on full bot reset)."""
    await redis.delete(f"{ns}:equity_tracker")
