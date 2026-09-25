"""Memecoin trade executor: Jupiter swaps + paper trading.

Includes Jev-powered smart exits (early close on momentum loss, trailing stops)
and dynamic position sizing based on AI confidence.
"""

import base64
import hashlib
import hmac
import json
import logging
import os
import time
import asyncio
from typing import Optional

import base58
import httpx
from solders.keypair import Keypair  # type: ignore
from solders.transaction import VersionedTransaction  # type: ignore
from mnemonic import Mnemonic

from config.settings import (
    JUPITER_QUOTE_URL, JUPITER_SWAP_URL,
    SOL_MINT, LAMPORTS_PER_SOL,
    MEMECOIN_MAX_PRICE_IMPACT_PCT,
    get_solana_rpc_url,
)
from core.state import (
    now_iso, _f,
    load_memecoin_state, log_memecoin_trade,
    set_cooldown,
)
from memecoin.discovery import fetch_token_price_usd, fetch_sol_usd_price

logger = logging.getLogger("trading_bot")

JEV_EXIT_THRESHOLD = 0.65
JEV_TRAIL_THRESHOLD = 0.55
TRAIL_ACTIVATE_PCT = 15.0
TRAIL_STEP_PCT = 0.4
BREAKEVEN_BUFFER_PCT = 2.0

LADDER_ENABLED = True
LADDER_LEVELS = [
    {"pct_gain": 30.0, "sell_pct": 50.0},
    {"pct_gain": 80.0, "sell_pct": 25.0},
]


class MemecoinExecutor:

    def __init__(self, http: httpx.AsyncClient, redis, ns: str):
        self.http = http
        self.redis = redis
        self.ns = ns

    # ── Entry ──────────────────────────────────────────────────────

    async def execute_entry(self, payload: dict, paper: bool, slippage_bps: int) -> Optional[dict]:
        mint = payload["mint"]
        cost = payload["cost_usd"]
        symbol = payload["symbol"]

        confidence = _f(payload.get("confidence", 50))
        adjusted_cost = _scale_cost(cost, confidence)

        fresh = await fetch_token_price_usd(self.http, mint)
        if fresh and fresh > 0:
            tp_pct = payload["take_profit_pct"]
            sl_pct = payload["stop_loss_pct"]
            payload = {
                **payload,
                "entry_price_usd": fresh,
                "take_profit_usd": fresh * (1 + tp_pct / 100),
                "stop_loss_usd": fresh * (1 - sl_pct / 100),
            }

        price = payload["entry_price_usd"]

        if paper:
            balance = _f(await self.redis.get(f"{self.ns}:mc:balance_usd"))
            if adjusted_cost > balance:
                return None

            entry = {
                **payload,
                "cost_usd": adjusted_cost,
                "original_cost_usd": adjusted_cost,
                "tokens": adjusted_cost / price,
                "paper": True,
                "raw_tokens_out": None,
                "opened_at": now_iso(),
                "original_sl_usd": payload["stop_loss_usd"],
                "highest_price": price,
                "trail_active": False,
                "remaining_pct": 100.0,
                "ladder_fills": [],
            }
            await self.redis.set(f"{self.ns}:mc:balance_usd", str(balance - adjusted_cost))
            await self.redis.hset(f"{self.ns}:mc:positions", mint, json.dumps(entry))
            await log_memecoin_trade(self.redis, self.ns, {
                "type": "ENTRY", "mint": mint, "symbol": symbol,
                "cost_usd": adjusted_cost, "entry_price_usd": price,
            })
            return entry

        try:
            res = await self._live_buy(mint, adjusted_cost, slippage_bps)
        except Exception as e:
            logger.warning(f"Live buy failed for {mint}: {e}")
            return None

        actual_raw = await self._get_token_balance(mint)
        entry = {
            **payload,
            "cost_usd": adjusted_cost,
            "original_cost_usd": adjusted_cost,
            "tokens": 0.0,
            "paper": False,
            "raw_tokens_out": actual_raw or res["raw_tokens_out"],
            "tx": res["url"],
            "opened_at": now_iso(),
            "original_sl_usd": payload["stop_loss_usd"],
            "highest_price": price,
            "trail_active": False,
            "remaining_pct": 100.0,
            "ladder_fills": [],
        }
        await self.redis.hset(f"{self.ns}:mc:positions", mint, json.dumps(entry))
        await log_memecoin_trade(self.redis, self.ns, {
            "type": "ENTRY", "mint": mint, "symbol": symbol,
            "cost_usd": adjusted_cost, "entry_price_usd": price, "tx": res["url"],
        })
        return entry

    # ── Exit checks ────────────────────────────────────────────────

    async def check_exits(self, positions: dict, paper: bool, slippage_bps: int) -> list[dict]:
        exits = []
        for mint, pos in list(positions.items()):
            price = await fetch_token_price_usd(self.http, mint)
            if price is None:
                continue

            tp = pos["take_profit_usd"]
            sl = pos["stop_loss_usd"]
            entry_price = pos["entry_price_usd"]
            reason = None
            sell_price = None
            sell_fraction = 1.0

            highest = max(pos.get("highest_price", entry_price), price)
            pnl_pct = ((price - entry_price) / entry_price) * 100 if entry_price > 0 else 0

            ladder_hit = self._check_ladder(pos, pnl_pct)
            if ladder_hit:
                partial = await self._execute_partial_exit(
                    pos, mint, price, ladder_hit, paper, slippage_bps,
                )
                if partial:
                    exits.append(partial)
                    pos["highest_price"] = highest
                    await self.redis.hset(f"{self.ns}:mc:positions", mint, json.dumps(pos))
                    continue

            if price >= tp:
                reason, sell_price = "TAKE_PROFIT", tp
            elif price <= sl:
                reason, sell_price = "STOP_LOSS", sl

            if reason is None:
                trail_active = pos.get("trail_active", False)
                new_sl = sl

                if pnl_pct >= TRAIL_ACTIVATE_PCT:
                    if not trail_active:
                        new_sl = entry_price * (1 + BREAKEVEN_BUFFER_PCT / 100)
                        trail_active = True
                        logger.info(f"Trailing stop activated for {pos.get('symbol', mint)}: moved SL to breakeven+{BREAKEVEN_BUFFER_PCT}%")

                    trail_sl = highest * (1 - TRAIL_STEP_PCT)
                    new_sl = max(new_sl, trail_sl)

                if trail_active and new_sl > sl:
                    pos["stop_loss_usd"] = new_sl
                    pos["trail_active"] = True

                    if price <= new_sl:
                        reason, sell_price = "TRAIL_STOP", price

                try:
                    from ai import ai_check_exit
                    jev_result = await ai_check_exit(pos, price, "memecoin")

                    if reason is None and jev_result["should_exit"] >= JEV_EXIT_THRESHOLD:
                        reason = "JEV_EXIT"
                        sell_price = price
                        logger.info(f"Jev recommends exit for {pos.get('symbol', mint)}: prob={jev_result['should_exit']:.0%}")

                    elif reason is None and jev_result["should_trail"] >= JEV_TRAIL_THRESHOLD and pnl_pct > 5:
                        tighter_sl = price * 0.92
                        if tighter_sl > pos["stop_loss_usd"]:
                            pos["stop_loss_usd"] = tighter_sl
                            pos["trail_active"] = True
                            logger.info(f"Jev tightened SL for {pos.get('symbol', mint)} to ${tighter_sl:.8g}")
                except Exception as e:
                    logger.debug(f"Jev exit check skipped for {pos.get('symbol', mint)}: {e}")

            if reason is None:
                pos["highest_price"] = highest
                await self.redis.hset(f"{self.ns}:mc:positions", mint, json.dumps(pos))
                continue

            if sell_price is None:
                sell_price = price

            if not paper:
                raw_amt = await self._get_token_balance(mint)
                if raw_amt <= 0:
                    logger.warning(f"No token balance for live exit: {mint}")
                    continue
                try:
                    await self._live_sell(mint, raw_amt, slippage_bps)
                except Exception as e:
                    logger.warning(f"Live exit failed for {mint}: {e}")
                    continue

            remaining_pct = pos.get("remaining_pct", 100.0)
            original_cost = pos.get("original_cost_usd", pos["cost_usd"])

            if paper:
                proceeds = pos["tokens"] * sell_price
                pnl = proceeds - pos["cost_usd"]
                bal = _f(await self.redis.get(f"{self.ns}:mc:balance_usd"))
                await self.redis.set(f"{self.ns}:mc:balance_usd", str(bal + proceeds))
            else:
                pnl = pos["cost_usd"] * (sell_price / pos["entry_price_usd"] - 1)

            await self.redis.hdel(f"{self.ns}:mc:positions", mint)
            await set_cooldown(self.redis, self.ns, mint)

            event = {
                "type": reason, "mint": mint, "symbol": pos["symbol"],
                "entry_price_usd": pos["entry_price_usd"],
                "exit_price_usd": sell_price, "pnl_usd": round(pnl, 4),
                "remaining_pct": 0,
            }
            await log_memecoin_trade(self.redis, self.ns, event)
            exits.append(event)

        return exits

    def _check_ladder(self, pos: dict, pnl_pct: float) -> dict | None:
        """Check if price has hit a profit-taking ladder level."""
        if not LADDER_ENABLED:
            return None
        ladder_fills = pos.get("ladder_fills", [])
        for level in LADDER_LEVELS:
            target_pct = level["pct_gain"]
            if pnl_pct >= target_pct and target_pct not in ladder_fills:
                return level
        return None

    async def _execute_partial_exit(
        self, pos: dict, mint: str, price: float,
        ladder_level: dict, paper: bool, slippage_bps: int,
    ) -> dict | None:
        """Sell a fraction of the position at a ladder level."""
        sell_pct = ladder_level["sell_pct"]
        remaining_pct = pos.get("remaining_pct", 100.0)

        actual_sell_pct = min(sell_pct, remaining_pct - 10)
        if actual_sell_pct <= 0:
            return None

        sell_fraction = actual_sell_pct / 100.0
        symbol = pos.get("symbol", mint)

        if not paper:
            raw_amt = await self._get_token_balance(mint)
            sell_raw = int(raw_amt * (actual_sell_pct / remaining_pct))
            if sell_raw <= 0:
                return None
            try:
                await self._live_sell(mint, sell_raw, slippage_bps)
            except Exception as e:
                logger.warning(f"Partial sell failed for {mint}: {e}")
                return None

        if paper:
            tokens_to_sell = pos["tokens"] * (actual_sell_pct / remaining_pct)
            proceeds = tokens_to_sell * price
            cost_portion = pos["cost_usd"] * (actual_sell_pct / remaining_pct)
            pnl = proceeds - cost_portion

            bal = _f(await self.redis.get(f"{self.ns}:mc:balance_usd"))
            await self.redis.set(f"{self.ns}:mc:balance_usd", str(bal + proceeds))

            pos["tokens"] -= tokens_to_sell
            pos["cost_usd"] -= cost_portion
        else:
            cost_portion = pos["cost_usd"] * (actual_sell_pct / remaining_pct)
            pnl = cost_portion * (price / pos["entry_price_usd"] - 1)
            pos["cost_usd"] -= cost_portion

        new_remaining = remaining_pct - actual_sell_pct
        pos["remaining_pct"] = new_remaining
        ladder_fills = pos.get("ladder_fills", [])
        ladder_fills.append(ladder_level["pct_gain"])
        pos["ladder_fills"] = ladder_fills

        await self.redis.hset(f"{self.ns}:mc:positions", mint, json.dumps(pos))

        pnl_pct = ((price - pos["entry_price_usd"]) / pos["entry_price_usd"]) * 100
        logger.info(
            f"Ladder exit {symbol}: sold {actual_sell_pct:.0f}% at +{pnl_pct:.1f}%, "
            f"{new_remaining:.0f}% remaining"
        )

        event = {
            "type": "LADDER_EXIT", "mint": mint, "symbol": symbol,
            "entry_price_usd": pos["entry_price_usd"],
            "exit_price_usd": price, "pnl_usd": round(pnl, 4),
            "sell_pct": actual_sell_pct,
            "remaining_pct": new_remaining,
            "ladder_level": ladder_level["pct_gain"],
        }
        await log_memecoin_trade(self.redis, self.ns, event)
        return event

    # ── Jupiter swap internals ─────────────────────────────────────

    async def _live_buy(self, mint: str, usd_amount: float, slippage_bps: int) -> dict:
        addr = os.environ.get("SOLANA_WALLET_ADDRESS")
        if not addr:
            raise RuntimeError("SOLANA_WALLET_ADDRESS not set")

        sol_usd = await fetch_sol_usd_price(self.http)
        if not sol_usd:
            raise RuntimeError("Could not resolve SOL price")

        amount_raw = int(usd_amount / sol_usd * LAMPORTS_PER_SOL)
        quote = await self._jupiter_quote(SOL_MINT, mint, amount_raw, slippage_bps)

        impact = _f(quote.get("priceImpactPct"))
        if impact > MEMECOIN_MAX_PRICE_IMPACT_PCT:
            raise ValueError(f"Price impact too high: {impact:.2f}%")

        raw_tx = await self._jupiter_swap(quote, addr)
        signed = self._sign_transaction(raw_tx)
        sig = await self._send_transaction(signed)

        return {
            "tx": sig,
            "url": f"https://solscan.io/tx/{sig}",
            "raw_tokens_out": int(quote.get("outAmount", 0)),
        }

    async def _live_sell(self, mint: str, raw_amount: int, slippage_bps: int) -> dict:
        addr = os.environ.get("SOLANA_WALLET_ADDRESS")
        if not addr:
            raise RuntimeError("SOLANA_WALLET_ADDRESS not set")

        quote = await self._jupiter_quote(mint, SOL_MINT, raw_amount, slippage_bps)
        raw_tx = await self._jupiter_swap(quote, addr)
        signed = self._sign_transaction(raw_tx)
        sig = await self._send_transaction(signed)

        return {
            "tx": sig,
            "url": f"https://solscan.io/tx/{sig}",
            "raw_sol_out": int(quote.get("outAmount", 0)),
        }

    async def _jupiter_quote(self, input_mint, output_mint, amount, slippage_bps) -> dict:
        r = await self.http.get(JUPITER_QUOTE_URL, params={
            "inputMint": input_mint,
            "outputMint": output_mint,
            "amount": str(amount),
            "slippageBps": slippage_bps,
        }, timeout=15)
        r.raise_for_status()
        q = r.json()
        if "outAmount" not in q:
            raise ValueError(f"Jupiter quote error: {q}")
        return q

    async def _jupiter_swap(self, quote: dict, wallet: str) -> str:
        payload = {
            "quoteResponse": quote,
            "userPublicKey": wallet,
            "dynamicComputeUnitLimit": True,
            "prioritizationFeeLamports": 10000,
        }
        r = await self.http.post(JUPITER_SWAP_URL, json=payload, timeout=20)
        r.raise_for_status()
        data = r.json()
        if "swapTransaction" not in data:
            raise ValueError(f"No swapTransaction: {data}")
        return data["swapTransaction"]

    def _sign_transaction(self, serialized_b64: str) -> str:
        kp = _load_keypair()
        tx = VersionedTransaction.from_bytes(base64.b64decode(serialized_b64))
        signed = VersionedTransaction(tx.message, [kp])
        return base64.b64encode(bytes(signed)).decode()

    async def _send_transaction(self, signed_b64: str) -> str:
        rpc = get_solana_rpc_url()
        payload = {
            "jsonrpc": "2.0", "id": 1, "method": "sendTransaction",
            "params": [signed_b64, {
                "encoding": "base64",
                "skipPreflight": False,
                "preflightCommitment": "confirmed",
                "maxRetries": 3,
            }],
        }
        r = await self.http.post(rpc, json=payload, timeout=30)
        r.raise_for_status()
        result = r.json()
        if "error" in result:
            raise ValueError(result["error"].get("message", "RPC error"))
        sig = result["result"]
        await self._confirm_transaction(sig)
        return sig

    async def _confirm_transaction(self, sig: str, timeout_s: float = 60.0) -> str:
        rpc = get_solana_rpc_url()
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            r = await self.http.post(rpc, json={
                "jsonrpc": "2.0", "id": 1, "method": "getSignatureStatuses",
                "params": [[sig], {"searchTransactionHistory": True}],
            }, timeout=15)
            r.raise_for_status()
            vals = r.json().get("result", {}).get("value", [])
            if vals and vals[0] is not None:
                status = vals[0]
                if status.get("err"):
                    raise ValueError(f"Transaction failed on-chain: {status['err']}")
                conf = status.get("confirmationStatus")
                if conf in ("confirmed", "finalized"):
                    return sig
            await asyncio.sleep(2)
        raise TimeoutError(f"Transaction {sig} not confirmed within {timeout_s}s")

    async def _get_token_balance(self, mint: str) -> int:
        wallet = os.environ.get("SOLANA_WALLET_ADDRESS", "")
        if not wallet:
            return 0
        rpc = get_solana_rpc_url()
        body = {
            "jsonrpc": "2.0", "id": 1, "method": "getTokenAccountsByOwner",
            "params": [wallet, {"mint": mint}, {"encoding": "jsonParsed"}],
        }
        try:
            r = await self.http.post(rpc, json=body, timeout=15)
            r.raise_for_status()
            accounts = r.json().get("result", {}).get("value", []) or []
        except Exception as e:
            logger.warning(f"Token balance fetch failed for {mint}: {e}")
            return 0
        total = 0
        for acct in accounts:
            info = acct.get("account", {}).get("data", {}).get("parsed", {}).get("info", {})
            amt = info.get("tokenAmount", {})
            try:
                total += int(amt.get("amount", 0))
            except (TypeError, ValueError):
                continue
        return total


def _load_keypair() -> Keypair:
    secret = (os.environ.get("SOLANA_PRIVATE_KEY") or "").strip()
    if not secret:
        raise RuntimeError("SOLANA_PRIVATE_KEY not set")
    if " " not in secret:
        return Keypair.from_bytes(base58.b58decode(secret))
    seed = Mnemonic("english").to_seed(secret, passphrase="")
    I = hmac.new(b"ed25519 seed", seed, hashlib.sha512).digest()
    IL, IR = I[:32], I[32:]
    for idx in (0x80000000 | 44, 0x80000000 | 501, 0x80000000 | 0, 0x80000000 | 0):
        I = hmac.new(IR, b"\x00" + IL + idx.to_bytes(4, "big"), hashlib.sha512).digest()
        IL, IR = I[:32], I[32:]
    return Keypair.from_seed(IL)


def _scale_cost(base_cost: float, confidence: float) -> float:
    """Scale trade size based on AI confidence. Higher confidence = bigger position."""
    if confidence >= 85:
        return round(base_cost * 1.5, 2)
    if confidence >= 75:
        return round(base_cost * 1.25, 2)
    if confidence >= 65:
        return base_cost
    return round(base_cost * 0.75, 2)
