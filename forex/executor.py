"""Forex trade executor: paper trading engine + broker integration (OANDA / MT5 ready)."""

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

        position_id = uuid.uuid4().hex[:12]
        notional = lot_size * 100_000
        margin_required = notional / leverage
        risk_usd = abs(entry - sl) / pip_val * lot_size * 10

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
                "lot_size": lot_size,
                "leverage": leverage,
                "margin": round(margin_required, 2),
                "risk_usd": round(risk_usd, 2),
                "unrealized_pnl": 0.0,
                "paper": True,
                "opened_at": now_iso(),
                "confidence": signal.get("confidence", 0),
                "reasoning": signal.get("reasoning", ""),
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
                "lot_size": lot_size,
                "risk_usd": round(risk_usd, 2),
                "confidence": signal.get("confidence", 0),
            })

            return position

        return await self._execute_broker_order(signal, lot_size, leverage)

    async def check_exits(self, paper: bool) -> list[dict]:
        """Check all open positions for TP/SL hits."""
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
                pip_val = get_pip_value(pair)
                if direction == "BUY":
                    pips = (current - entry) / pip_val
                else:
                    pips = (entry - current) / pip_val
                pnl = pips * pos["lot_size"] * 10
                pos["current_price"] = current
                pos["unrealized_pnl"] = round(pnl, 2)
                await self.redis.hset(f"{self.ns}:fx:positions", pos_id, json.dumps(pos))
                continue

            if paper:
                pip_val = get_pip_value(pair)
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
                "lot_size": lot_size,
                "leverage": leverage,
                "paper": False,
                "broker": "oanda",
                "broker_trade_id": trade_id,
                "opened_at": now_iso(),
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
