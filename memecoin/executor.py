"""Memecoin trade executor: Jupiter swaps + paper trading."""

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
            if cost > balance:
                return None

            entry = {
                **payload,
                "tokens": cost / price,
                "paper": True,
                "raw_tokens_out": None,
                "opened_at": now_iso(),
            }
            await self.redis.set(f"{self.ns}:mc:balance_usd", str(balance - cost))
            await self.redis.hset(f"{self.ns}:mc:positions", mint, json.dumps(entry))
            await log_memecoin_trade(self.redis, self.ns, {
                "type": "ENTRY", "mint": mint, "symbol": symbol,
                "cost_usd": cost, "entry_price_usd": price,
            })
            return entry

        try:
            res = await self._live_buy(mint, cost, slippage_bps)
        except Exception as e:
            logger.warning(f"Live buy failed for {mint}: {e}")
            return None

        actual_raw = await self._get_token_balance(mint)
        entry = {
            **payload,
            "tokens": 0.0,
            "paper": False,
            "raw_tokens_out": actual_raw or res["raw_tokens_out"],
            "tx": res["url"],
            "opened_at": now_iso(),
        }
        await self.redis.hset(f"{self.ns}:mc:positions", mint, json.dumps(entry))
        await log_memecoin_trade(self.redis, self.ns, {
            "type": "ENTRY", "mint": mint, "symbol": symbol,
            "cost_usd": cost, "entry_price_usd": price, "tx": res["url"],
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
            reason = None
            sell_price = None

            if price >= tp:
                reason, sell_price = "TAKE_PROFIT", tp
            elif price <= sl:
                reason, sell_price = "STOP_LOSS", sl

            if reason is None:
                continue

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
            }
            await log_memecoin_trade(self.redis, self.ns, event)
            exits.append(event)

        return exits

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
