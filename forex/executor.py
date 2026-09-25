"""Forex trade executor: paper trading engine + broker integration (OANDA / MT5 ready).

Includes Jev-powered smart exits (early close on momentum loss, trailing stops)
and dynamic position sizing based on AI confidence.
"""

import json
import logging
import os
import uuid
from typing import Optional

import httpx

from config.settings import FOREX_MAX_OPEN_POSITIONS
from config.pairs import get_pip_value
from core.state import (
    now_iso, _f,
    load_forex_state, log_forex_trade,
    load_forex_pending,
)

logger = logging.getLogger("trading_bot")

OANDA_API_BASE = "https://api-fxtrade.oanda.com"
OANDA_PRACTICE_BASE = "https://api-fxpractice.oanda.com"

TRAIL_ACTIVATE_RR = 1.0
TRAIL_STEP_PCT = 0.5
BREAKEVEN_BUFFER_PIPS = 2.0
JEV_EXIT_THRESHOLD = 0.65
JEV_TRAIL_THRESHOLD = 0.55


class ForexExecutor:

    def __init__(self, http: httpx.AsyncClient, redis, ns: str):
        self.http = http
        self.redis = redis
        self.ns = ns

    async def open_position(
        self,
        signal: dict,
        lot_size: float,
        paper: bool,
        leverage: int = 50,
    ) -> Optional[dict]:
        """Open a forex position based on a signal."""
        pair = signal["pair"]
        direction = signal["direction"]
        entry = signal["entry_price"]
        sl = signal["stop_loss"]
        tp = signal["take_profit"]
        pip_val = get_pip_value(pair)

        balance, equity, positions = await load_forex_state(self.redis, self.ns)
        if len(positions) >= FOREX_MAX_OPEN_POSITIONS:
            logger.info(f"Max forex positions reached ({len(positions)})")
            return None

        confidence = _f(signal.get("confidence", 50))
        adjusted_lot = _scale_lot_size(lot_size, confidence)

        position_id = uuid.uuid4().hex[:12]
        notional = adjusted_lot * 100_000
        margin_required = notional / leverage
        risk_usd = abs(entry - sl) / pip_val * adjusted_lot * 10

        if paper:
            if balance is None:
                return None
            if margin_required > balance * 0.5:
                logger.info(f"Insufficient margin for {pair}")
                return None

            position = {
                "id": position_id,
                "pair": pair,
                "direction": direction,
                "entry_price": entry,
                "current_price": entry,
                "stop_loss": sl,
                "take_profit": tp,
                "original_sl": sl,
                "lot_size": adjusted_lot,
                "leverage": leverage,
                "margin": round(margin_required, 2),
                "risk_usd": round(risk_usd, 2),
                "unrealized_pnl": 0.0,
                "highest_pnl_pips": 0.0,
                "paper": True,
                "opened_at": now_iso(),
                "confidence": confidence,
                "reasoning": signal.get("reasoning", ""),
                "trail_active": False,
            }

            await self.redis.hset(f"{self.ns}:fx:positions", position_id, json.dumps(position))
            used_margin = sum(json.loads(p.decode() if isinstance(p, bytes) else p).get("margin", 0)
                              for p in (await self.redis.hgetall(f"{self.ns}:fx:positions")).values())
            new_equity = balance - used_margin
            await self.redis.set(f"{self.ns}:fx:equity_usd", str(round(new_equity, 2)))

            await log_forex_trade(self.redis, self.ns, {
                "type": "ENTRY",
                "pair": pair,
                "direction": direction,
                "entry_price": entry,
                "lot_size": adjusted_lot,
                "risk_usd": round(risk_usd, 2),
                "confidence": confidence,
            })

            return position

        return await self._execute_broker_order(signal, adjusted_lot, leverage)

    async def check_exits(self, paper: bool) -> list[dict]:
        """Check all open positions for TP/SL hits, trailing stops, and Jev smart exits."""
        balance, equity, positions = await load_forex_state(self.redis, self.ns)
        closed = []

        for pos_id, pos in list(positions.items()):
            pair = pos["pair"]
            from forex.data import fetch_current_price
            current = await fetch_current_price(self.http, pair)
            if current is None:
                continue

            direction = pos["direction"]
            entry = pos["entry_price"]
            sl = pos["stop_loss"]
            tp = pos["take_profit"]
            pip_val = get_pip_value(pair)

            if direction == "BUY":
                pnl_pips = (current - entry) / pip_val
            else:
                pnl_pips = (entry - current) / pip_val

            highest = max(pos.get("highest_pnl_pips", 0), pnl_pips)

            reason = None
            exit_price = None

            if direction == "BUY":
                if current >= tp:
                    reason, exit_price = "TP_HIT", tp
                elif current <= sl:
                    reason, exit_price = "SL_HIT", sl
            else:
                if current <= tp:
                    reason, exit_price = "TP_HIT", tp
                elif current >= sl:
                    reason, exit_price = "SL_HIT", sl

            if reason is None:
                original_sl = pos.get("original_sl", sl)
                sl_distance_pips = abs(entry - original_sl) / pip_val
                rr_achieved = pnl_pips / sl_distance_pips if sl_distance_pips > 0 else 0

                new_sl = sl
                trail_active = pos.get("trail_active", False)

                if rr_achieved >= TRAIL_ACTIVATE_RR and pnl_pips > 0:
                    if not trail_active:
                        if direction == "BUY":
                            new_sl = entry + BREAKEVEN_BUFFER_PIPS * pip_val
                        else:
                            new_sl = entry - BREAKEVEN_BUFFER_PIPS * pip_val
                        trail_active = True
                        logger.info(f"Trailing stop activated for {pair}: moved SL to breakeven+{BREAKEVEN_BUFFER_PIPS} pips")

                    trail_distance = highest * TRAIL_STEP_PCT * pip_val
                    if direction == "BUY":
                        trail_sl = current - trail_distance
                        new_sl = max(new_sl, trail_sl)
                    else:
                        trail_sl = current + trail_distance
                        new_sl = min(new_sl, trail_sl)

                if trail_active and new_sl != sl:
                    pos["stop_loss"] = new_sl
                    pos["trail_active"] = True

                try:
                    from ai import ai_check_exit
                    jev_result = await ai_check_exit(pos, current, "forex")

                    if jev_result["should_exit"] >= JEV_EXIT_THRESHOLD:
                        reason = "JEV_EXIT"
                        exit_price = current
                        logger.info(f"Jev recommends exit for {pair}: prob={jev_result['should_exit']:.0%}")

                    elif jev_result["should_trail"] >= JEV_TRAIL_THRESHOLD and pnl_pips > 0:
                        if direction == "BUY":
                            tighter_sl = current - abs(current - entry) * 0.3
                            if tighter_sl > pos["stop_loss"]:
                                pos["stop_loss"] = tighter_sl
                                pos["trail_active"] = True
                                logger.info(f"Jev tightened SL for {pair} to {tighter_sl:.5f}")
                        else:
                            tighter_sl = current + abs(entry - current) * 0.3
                            if tighter_sl < pos["stop_loss"]:
                                pos["stop_loss"] = tighter_sl
                                pos["trail_active"] = True
                                logger.info(f"Jev tightened SL for {pair} to {tighter_sl:.5f}")
                except Exception as e:
                    logger.debug(f"Jev exit check skipped for {pair}: {e}")

            if reason is None:
                pnl = pnl_pips * pos["lot_size"] * 10
                pos["current_price"] = current
                pos["unrealized_pnl"] = round(pnl, 2)
                pos["highest_pnl_pips"] = highest
                await self.redis.hset(f"{self.ns}:fx:positions", pos_id, json.dumps(pos))
                continue

            if not exit_price:
                exit_price = current

            if paper:
                if direction == "BUY":
                    pips = (exit_price - entry) / pip_val
                else:
                    pips = (entry - exit_price) / pip_val
                pnl = pips * pos["lot_size"] * 10

                balance = _f(await self.redis.get(f"{self.ns}:fx:balance_usd"))
                new_balance = balance + pnl + pos.get("margin", 0)
                await self.redis.set(f"{self.ns}:fx:balance_usd", str(round(new_balance, 2)))
            else:
                pnl = await self._close_broker_position(pos)

            await self.redis.hdel(f"{self.ns}:fx:positions", pos_id)

            event = {
                "type": reason,
                "pair": pair,
                "direction": direction,
                "entry_price": entry,
                "exit_price": exit_price,
                "pnl_usd": round(pnl, 2),
                "lot_size": pos["lot_size"],
            }
            await log_forex_trade(self.redis, self.ns, event)
            closed.append(event)

        balance, equity, positions = await load_forex_state(self.redis, self.ns)
        total_unrealized = sum(p.get("unrealized_pnl", 0) for p in positions.values())
        if balance is not None:
            total_margin = sum(p.get("margin", 0) for p in positions.values())
            new_equity = balance - total_margin + total_unrealized
            await self.redis.set(f"{self.ns}:fx:equity_usd", str(round(new_equity, 2)))

        return closed

    async def _execute_broker_order(self, signal: dict, lot_size: float, leverage: int) -> Optional[dict]:
        """Execute via OANDA API (when configured)."""
        api_key = os.environ.get("OANDA_API_KEY", "")
        account_id = os.environ.get("OANDA_ACCOUNT_ID", "")

        if not api_key or not account_id:
            logger.warning("OANDA credentials not configured, cannot execute live forex trade")
            return None

        practice = os.environ.get("OANDA_PRACTICE", "true").lower() == "true"
        base = OANDA_PRACTICE_BASE if practice else OANDA_API_BASE

        pair_oanda = signal["pair"].replace("/", "_")
        units = int(lot_size * 100_000)
        if signal["direction"] == "SELL":
            units = -units

        order_data = {
            "order": {
                "type": "MARKET",
                "instrument": pair_oanda,
                "units": str(units),
                "stopLossOnFill": {"price": f"{signal['stop_loss']:.5f}"},
                "takeProfitOnFill": {"price": f"{signal['take_profit']:.5f}"},
                "timeInForce": "FOK",
            }
        }

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

        try:
            r = await self.http.post(
                f"{base}/v3/accounts/{account_id}/orders",
                json=order_data,
                headers=headers,
                timeout=15,
            )
            r.raise_for_status()
            data = r.json()

            fill = data.get("orderFillTransaction", {})
            trade_id = fill.get("tradeOpened", {}).get("tradeID", "")

            position = {
                "id": trade_id,
                "pair": signal["pair"],
                "direction": signal["direction"],
                "entry_price": float(fill.get("price", signal["entry_price"])),
                "stop_loss": signal["stop_loss"],
                "take_profit": signal["take_profit"],
                "original_sl": signal["stop_loss"],
                "lot_size": lot_size,
                "leverage": leverage,
                "paper": False,
                "broker": "oanda",
                "broker_trade_id": trade_id,
                "opened_at": now_iso(),
                "highest_pnl_pips": 0.0,
                "trail_active": False,
            }

            await self.redis.hset(f"{self.ns}:fx:positions", trade_id, json.dumps(position))
            return position

        except Exception as e:
            logger.warning(f"OANDA order failed: {e}")
            return None

    async def _close_broker_position(self, position: dict) -> float:
        """Close a position via OANDA."""
        api_key = os.environ.get("OANDA_API_KEY", "")
        account_id = os.environ.get("OANDA_ACCOUNT_ID", "")
        trade_id = position.get("broker_trade_id", "")

        if not all([api_key, account_id, trade_id]):
            return 0.0

        practice = os.environ.get("OANDA_PRACTICE", "true").lower() == "true"
        base = OANDA_PRACTICE_BASE if practice else OANDA_API_BASE

        headers = {"Authorization": f"Bearer {api_key}"}

        try:
            r = await self.http.put(
                f"{base}/v3/accounts/{account_id}/trades/{trade_id}/close",
                headers=headers,
                timeout=15,
            )
            r.raise_for_status()
            data = r.json()
            pnl = float(data.get("orderFillTransaction", {}).get("pl", 0))
            return pnl
        except Exception as e:
            logger.warning(f"OANDA close failed: {e}")
            return 0.0


def _scale_lot_size(base_lot: float, confidence: float) -> float:
    """Scale position size based on AI confidence. Higher confidence = larger position."""
    if confidence >= 85:
        return round(base_lot * 1.5, 3)
    if confidence >= 75:
        return round(base_lot * 1.25, 3)
    if confidence >= 65:
        return base_lot
    return max(0.001, round(base_lot * 0.75, 3))
