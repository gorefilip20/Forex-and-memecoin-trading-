"""
Forex & Memecoin Trading Bot
─────────────────────────────
Dual-market autonomous trading platform:
  - Forex: Technical analysis, AI-confirmed signals, paper/live execution via OANDA
  - Memecoin: DexScreener discovery, enhanced rug detection, Jupiter execution on Solana
  - Shared: Telegram alerts + approval flow, Redis state, unified dashboard
"""

import json
import logging
import os
import uuid
from datetime import datetime, timezone

import httpx
from fastapi import FastAPI

from config.settings import (
    MEMECOIN_MIN_CONFIDENCE, FOREX_MIN_CONFIDENCE,
    MEMECOIN_MIN_TP_PCT, MEMECOIN_MAX_TP_PCT,
    MEMECOIN_MIN_SL_PCT, MEMECOIN_MAX_SL_PCT,
)
from config.pairs import get_pip_value
from core.models import (
    MemecoinBotRequest, MemecoinCycleResponse,
    ForexBotRequest, ForexCycleResponse,
    StatusResponse, ResetResponse, DashboardResponse,
)
from core.state import (
    now_iso, _f, _dec,
    load_memecoin_state, load_memecoin_pending,
    load_forex_state, load_forex_pending,
    load_all_trades, calculate_stats,
    is_in_cooldown,
)
from core.telegram import send_telegram, send_approval_message, send_forex_signal, tg_get, tg_post
from memecoin.discovery import discover_candidates
from memecoin.safety import check_token_safety, enhanced_rug_check
from memecoin.executor import MemecoinExecutor
from forex.signals import ForexSignalGenerator
from forex.executor import ForexExecutor
from ai.analyst import ai_decide_memecoins, ai_decide_forex

try:
    from codewords_client import AsyncCodewordsClient, logger as cw_logger, redis_client, run_service
    from structlog.contextvars import get_contextvars
    logger = cw_logger
    HAS_CODEWORDS = True
except ImportError:
    import redis.asyncio as aioredis
    from contextlib import asynccontextmanager
    logger = logging.getLogger("trading_bot")
    logging.basicConfig(level=logging.INFO)
    HAS_CODEWORDS = False

    @asynccontextmanager
    async def redis_client():
        url = os.environ.get("REDIS_URL", "redis://localhost:6379")
        r = aioredis.from_url(url)
        ns = os.environ.get("REDIS_NAMESPACE", "trading_bot")
        try:
            yield r, ns
        finally:
            await r.aclose()

    def get_contextvars():
        return {}


app = FastAPI(
    title="Forex & Memecoin Trading Bot",
    description=(
        "Dual-market autonomous trading platform. "
        "Forex: AI-powered signal generation with technical analysis (RSI, MACD, EMA, BB, ATR, Stochastic, ADX). "
        "Memecoin: DexScreener discovery with enhanced rug-pull protection and Jupiter execution on Solana."
    ),
    version="3.0.0",
)


# ══════════════════════════════════════════════════════════════════
#  MEMECOIN ENDPOINTS
# ══════════════════════════════════════════════════════════════════

@app.post("/memecoin", response_model=MemecoinCycleResponse)
async def run_memecoin_cycle(req: MemecoinBotRequest):
    """Run a memecoin discovery + trading cycle."""
    logger.info("Memecoin cycle start", paper=req.paper_trading)
    cycle_time = now_iso()
    entries, exits, decisions = [], [], []
    approvals_processed = 0
    alerts_sent = 0
    rugs_blocked = 0

    async with httpx.AsyncClient(timeout=25) as http:
        async with redis_client() as (redis, ns):
            executor = MemecoinExecutor(http, redis, ns)
            balance, positions = await load_memecoin_state(redis, ns)

            if req.paper_trading and balance is None:
                balance = req.paper_starting_usd
                await redis.set(f"{ns}:mc:balance_usd", str(balance))

            approvals_processed = await _process_memecoin_approvals(http, redis, ns, req, executor)
            balance, positions = await load_memecoin_state(redis, ns)

            exits = await executor.check_exits(positions, req.paper_trading, req.slippage_bps)
            balance, positions = await load_memecoin_state(redis, ns)

            candidates = await discover_candidates(
                http, req.min_liquidity_usd, req.min_volume_24h, req.max_age_hours,
            )
            logger.info("Memecoin candidates found", count=len(candidates))

            open_mints = set(positions.keys())
            pending = await load_memecoin_pending(redis, ns)

            if candidates and (len(positions) + len(pending)) < req.max_positions:
                decisions = await ai_decide_memecoins(candidates, open_mints)

            for d in decisions:
                if (len(positions) + len(pending)) >= req.max_positions:
                    break

                action = str(d.get("action", "")).upper()
                if action != "BUY":
                    continue

                mint = str(d.get("mint", ""))
                cand = next((c for c in candidates if c["mint"] == mint), None)
                if cand is None or mint in open_mints:
                    continue
                if await is_in_cooldown(redis, ns, mint):
                    continue

                conf = _f(d.get("confidence"))
                if conf < MEMECOIN_MIN_CONFIDENCE:
                    continue

                tp_pct = min(max(_f(d.get("take_profit_pct")), MEMECOIN_MIN_TP_PCT), MEMECOIN_MAX_TP_PCT)
                sl_pct = min(max(_f(d.get("stop_loss_pct")), MEMECOIN_MIN_SL_PCT), MEMECOIN_MAX_SL_PCT)
                if tp_pct <= sl_pct or cand["price_usd"] <= 0 or cand["liquidity_usd"] < req.min_liquidity_usd:
                    continue

                balance, positions = await load_memecoin_state(redis, ns)
                open_mints = set(positions.keys())
                if mint in open_mints:
                    continue
                cost = min(req.per_trade_usd, balance) if (req.paper_trading and balance is not None) else req.per_trade_usd
                if cost < 0.05:
                    break

                if req.enhanced_rug_check:
                    safety = await enhanced_rug_check(http, mint)
                else:
                    safety = await check_token_safety(http, mint)

                if not safety["ok"]:
                    logger.warning("Token blocked", mint=mint, symbol=cand["symbol"], reasons=safety["reasons"])
                    rugs_blocked += 1
                    if await send_telegram(http, f"[BLOCKED] {cand['symbol']}: {'; '.join(safety['reasons'])}"):
                        alerts_sent += 1
                    decisions = [
                        {**dd, "action": "SKIP", "reasoning": "rug-check: " + "; ".join(safety["reasons"])}
                        if dd.get("mint") == mint else dd
                        for dd in decisions
                    ]
                    continue

                payload = {
                    "mint": mint, "symbol": cand["symbol"], "dex": cand.get("dex", "?"),
                    "entry_price_usd": cand["price_usd"], "cost_usd": cost,
                    "take_profit_usd": cand["price_usd"] * (1 + tp_pct / 100),
                    "stop_loss_usd": cand["price_usd"] * (1 - sl_pct / 100),
                    "take_profit_pct": tp_pct, "stop_loss_pct": sl_pct,
                    "confidence": conf, "reasoning": str(d.get("reasoning", ""))[:200],
                    "liquidity_usd": cand["liquidity_usd"],
                    "safety_score": safety.get("score", 0),
                }

                if req.approve_first:
                    aid = uuid.uuid4().hex[:10]
                    pending_payload = {**payload, "created_at": cycle_time}
                    await redis.hset(f"{ns}:mc:pending", aid, json.dumps(pending_payload))
                    sent = await send_approval_message(http, aid, payload, "memecoin")
                    if sent:
                        alerts_sent += 1
                    pending = await load_memecoin_pending(redis, ns)
                else:
                    entry = await executor.execute_entry(payload, req.paper_trading, req.slippage_bps)
                    if entry:
                        entries.append({
                            k: entry.get(k) for k in (
                                "mint", "symbol", "entry_price_usd", "cost_usd",
                                "take_profit_usd", "stop_loss_usd", "confidence", "reasoning",
                            )
                        })
                        label = "PAPER" if req.paper_trading else "LIVE"
                        if await send_telegram(http, f"[{label} ENTRY] {payload['symbol']} @ ${payload['entry_price_usd']:.8g} | cost ${payload['cost_usd']:.2f}"):
                            alerts_sent += 1
                    break

            balance, positions = await load_memecoin_state(redis, ns)
            pending = await load_memecoin_pending(redis, ns)

    msg = (
        f"Memecoin cycle: {len(candidates)} candidates, {len(decisions)} decisions, "
        f"{len(entries)} entries, {len(exits)} exits, {rugs_blocked} rugs blocked. "
    )
    if req.paper_trading:
        msg += f"Paper balance ${balance:.2f}."

    return MemecoinCycleResponse(
        cycle_time=cycle_time, paper_trading=req.paper_trading, approve_first=req.approve_first,
        candidates_found=len(candidates), approvals_processed=approvals_processed,
        alerts_sent=alerts_sent, decisions=decisions, entries=entries, exits=exits,
        pending_approvals=len(pending), balance_usd=round(balance or 0, 2),
        open_positions=len(positions), rugs_blocked=rugs_blocked, message=msg,
    )


# ══════════════════════════════════════════════════════════════════
#  FOREX ENDPOINTS
# ══════════════════════════════════════════════════════════════════

@app.post("/forex", response_model=ForexCycleResponse)
async def run_forex_cycle(req: ForexBotRequest):
    """Run a forex analysis + signal + trading cycle."""
    logger.info("Forex cycle start", paper=req.paper_trading, pairs=req.pairs)
    cycle_time = now_iso()
    signals_out = []
    trades_executed = 0
    trades_closed = 0
    alerts_sent = 0

    async with httpx.AsyncClient(timeout=25) as http:
        async with redis_client() as (redis, ns):
            fx_executor = ForexExecutor(http, redis, ns)
            balance, equity, positions = await load_forex_state(redis, ns)

            if req.paper_trading and balance is None:
                balance = req.paper_starting_usd
                await redis.set(f"{ns}:fx:balance_usd", str(balance))
                await redis.set(f"{ns}:fx:equity_usd", str(balance))
                equity = balance

            approvals = await _process_forex_approvals(http, redis, ns, req, fx_executor)
            balance, equity, positions = await load_forex_state(redis, ns)

            closed = await fx_executor.check_exits(req.paper_trading)
            trades_closed = len(closed)
            for c in closed:
                pnl_str = f"${c['pnl_usd']:+.2f}"
                if await send_telegram(http, f"[FOREX {c['type']}] {c['pair']} {c['direction']}: {pnl_str}"):
                    alerts_sent += 1

            balance, equity, positions = await load_forex_state(redis, ns)

            signal_gen = ForexSignalGenerator(http)
            raw_signals = await signal_gen.scan_pairs(req.pairs, req.timeframe, req.multi_timeframe)
            logger.info("Forex signals generated", count=len(raw_signals))

            open_pairs = {p.get("pair") for p in positions.values()}
            pending = await load_forex_pending(redis, ns)

            ai_decisions = []
            if raw_signals and (len(positions) + len(pending)) < req.max_positions:
                ai_decisions = await ai_decide_forex(raw_signals, open_pairs)

            for decision in ai_decisions:
                if (len(positions) + len(pending)) >= req.max_positions:
                    break

                action = str(decision.get("action", "")).upper()
                if action != "EXECUTE":
                    continue

                pair = decision.get("pair", "")
                if pair in open_pairs:
                    continue

                conf = _f(decision.get("confidence"))
                if conf < FOREX_MIN_CONFIDENCE:
                    continue

                signal = next((s for s in raw_signals if s["pair"] == pair), None)
                if not signal:
                    continue

                if decision.get("adjusted_sl_pips"):
                    pip_val = get_pip_value(pair)
                    new_sl_pips = _f(decision["adjusted_sl_pips"])
                    if signal["direction"] == "BUY":
                        signal["stop_loss"] = signal["entry_price"] - new_sl_pips * pip_val
                    else:
                        signal["stop_loss"] = signal["entry_price"] + new_sl_pips * pip_val
                    signal["sl_pips"] = new_sl_pips

                if decision.get("adjusted_tp_pips"):
                    pip_val = get_pip_value(pair)
                    new_tp_pips = _f(decision["adjusted_tp_pips"])
                    if signal["direction"] == "BUY":
                        signal["take_profit"] = signal["entry_price"] + new_tp_pips * pip_val
                    else:
                        signal["take_profit"] = signal["entry_price"] - new_tp_pips * pip_val
                    signal["tp_pips"] = new_tp_pips

                signal["confidence"] = conf
                signal["reasoning"] = str(decision.get("reasoning", signal.get("reasoning", "")))[:200]
                signals_out.append(signal)

                if await send_forex_signal(http, signal):
                    alerts_sent += 1

                if req.approve_first:
                    aid = uuid.uuid4().hex[:10]
                    pending_payload = {**signal, "created_at": cycle_time}
                    await redis.hset(f"{ns}:fx:pending", aid, json.dumps(pending_payload))
                    sent = await send_approval_message(http, aid, signal, "forex")
                    if sent:
                        alerts_sent += 1
                    pending = await load_forex_pending(redis, ns)
                else:
                    position = await fx_executor.open_position(
                        signal, req.lot_size, req.paper_trading, req.leverage,
                    )
                    if position:
                        trades_executed += 1
                        label = "PAPER" if req.paper_trading else "LIVE"
                        if await send_telegram(
                            http,
                            f"[{label} FOREX ENTRY] {pair} {signal['direction']} @ {signal['entry_price']:.5f} | "
                            f"SL: {signal['sl_pips']:.0f} pips, TP: {signal['tp_pips']:.0f} pips",
                        ):
                            alerts_sent += 1

            balance, equity, positions = await load_forex_state(redis, ns)

    msg = (
        f"Forex cycle: {len(req.pairs)} pairs analyzed, {len(signals_out)} signals, "
        f"{trades_executed} executed, {trades_closed} closed. "
    )
    if req.paper_trading:
        msg += f"Balance ${balance or 0:.2f}, Equity ${equity:.2f}."

    return ForexCycleResponse(
        cycle_time=cycle_time, paper_trading=req.paper_trading, approve_first=req.approve_first,
        pairs_analyzed=len(req.pairs), signals_generated=len(signals_out),
        trades_executed=trades_executed, trades_closed=trades_closed,
        alerts_sent=alerts_sent, signals=[s for s in signals_out],
        open_positions=len(positions), balance_usd=round(balance or 0, 2),
        equity_usd=round(equity, 2), message=msg,
    )


# ══════════════════════════════════════════════════════════════════
#  FOREX ANALYSIS ENDPOINT (signals only, no execution)
# ══════════════════════════════════════════════════════════════════

@app.post("/forex/analyze")
async def forex_analyze(
    pairs: list[str] = ["EUR/USD", "GBP/USD", "USD/JPY", "XAU/USD"],
    timeframe: str = "1h",
    multi_timeframe: bool = True,
):
    """Generate forex signals without executing trades. Pure analysis."""
    async with httpx.AsyncClient(timeout=25) as http:
        generator = ForexSignalGenerator(http)
        signals = await generator.scan_pairs(pairs, timeframe, multi_timeframe)
        return {
            "timestamp": now_iso(),
            "pairs_analyzed": len(pairs),
            "signals": signals,
            "timeframe": timeframe,
        }


@app.post("/forex/analyze/{pair}")
async def forex_analyze_pair(pair: str, timeframe: str = "1h"):
    """Deep analysis of a single forex pair with all indicators."""
    from forex.analysis import TechnicalAnalyzer
    from forex.data import fetch_candles

    async with httpx.AsyncClient(timeout=25) as http:
        candles = await fetch_candles(http, pair, timeframe, 200)
        if len(candles) < 50:
            return {"error": f"Insufficient data for {pair} ({len(candles)} candles)"}

        analyzer = TechnicalAnalyzer(candles)
        analysis = analyzer.full_analysis()

        generator = ForexSignalGenerator(http)
        signal = await generator.analyze_pair(pair, timeframe)

        return {
            "timestamp": now_iso(),
            "pair": pair,
            "timeframe": timeframe,
            "analysis": analysis,
            "signal": signal,
        }


# ══════════════════════════════════════════════════════════════════
#  STATUS / DASHBOARD / RESET
# ══════════════════════════════════════════════════════════════════

@app.get("/status", response_model=StatusResponse)
async def status():
    """Get full status of both memecoin and forex positions."""
    async with redis_client() as (redis, ns):
        mc_bal, mc_pos = await load_memecoin_state(redis, ns)
        mc_pending = await load_memecoin_pending(redis, ns)
        fx_bal, fx_eq, fx_pos = await load_forex_state(redis, ns)
        trades = await load_all_trades(redis, ns, 100)

    return StatusResponse(
        memecoin_balance_usd=round(mc_bal or 0, 2),
        memecoin_positions=list(mc_pos.values()),
        memecoin_pending=list(mc_pending.values()),
        forex_balance_usd=round(fx_bal or 0, 2),
        forex_positions=list(fx_pos.values()),
        forex_equity_usd=round(fx_eq or 0, 2),
        trade_log=trades,
    )


@app.get("/dashboard", response_model=DashboardResponse)
async def dashboard():
    """Combined trading dashboard with stats from both markets."""
    async with redis_client() as (redis, ns):
        mc_bal, mc_pos = await load_memecoin_state(redis, ns)
        fx_bal, fx_eq, fx_pos = await load_forex_state(redis, ns)
        stats = await calculate_stats(redis, ns)

    return DashboardResponse(
        memecoin={
            "balance_usd": round(mc_bal or 0, 2),
            "open_positions": len(mc_pos),
            "positions": list(mc_pos.values()),
        },
        forex={
            "balance_usd": round(fx_bal or 0, 2),
            "equity_usd": round(fx_eq or 0, 2),
            "open_positions": len(fx_pos),
            "positions": list(fx_pos.values()),
        },
        combined_pnl_usd=stats.get("total_pnl", 0),
        total_trades=stats.get("total_trades", 0),
        win_rate=stats.get("win_rate", 0),
    )


@app.post("/reset", response_model=ResetResponse)
async def reset():
    """Reset all state (paper balances, positions, logs)."""
    async with redis_client() as (redis, ns):
        for key_suffix in (
            "mc:balance_usd", "mc:positions", "mc:pending", "mc:cooldowns",
            "fx:balance_usd", "fx:equity_usd", "fx:positions", "fx:pending",
            "trade_log", "tg_offset",
        ):
            await redis.delete(f"{ns}:{key_suffix}")

        await redis.set(f"{ns}:mc:balance_usd", "1000")
        await redis.set(f"{ns}:fx:balance_usd", "10000")
        await redis.set(f"{ns}:fx:equity_usd", "10000")

    return ResetResponse(reset=True, memecoin_balance_usd=1000.0, forex_balance_usd=10000.0)


# ══════════════════════════════════════════════════════════════════
#  TELEGRAM APPROVAL PROCESSING
# ══════════════════════════════════════════════════════════════════

async def _process_memecoin_approvals(http, redis, ns, req, executor) -> int:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        return 0

    offset_raw = await redis.get(f"{ns}:tg_offset")
    params = {"timeout": 0}
    if offset_raw:
        params["offset"] = offset_raw

    try:
        data = await tg_get(http, "getUpdates", params)
    except Exception as e:
        logger.warning(f"getUpdates failed: {e}")
        return 0

    updates = data.get("result", []) or []
    processed = 0
    max_id = None

    for u in updates:
        uid = u.get("update_id")
        if uid is not None:
            max_id = uid
        cq = u.get("callback_query")
        if not cq:
            msg = u.get("message")
            if msg:
                text = (msg.get("text") or "").strip().lower()
                if text in ("status", "/status", "balance", "/balance"):
                    mc_bal, mc_pos = await load_memecoin_state(redis, ns)
                    fx_bal, _, fx_pos = await load_forex_state(redis, ns)
                    mode = "PAPER" if req.paper_trading else "LIVE"
                    lines = [
                        f"Bot status ({mode})",
                        f"Memecoin: ${mc_bal or 0:.2f} | {len(mc_pos)} positions",
                        f"Forex: ${fx_bal or 0:.2f} | {len(fx_pos)} positions",
                    ]
                    await send_telegram(http, "\n".join(lines))
            continue

        cbid = cq.get("id")
        cbdata = cq.get("data", "")
        try:
            await tg_post(http, "answerCallbackQuery", {"callback_query_id": cbid})
        except Exception:
            pass

        if cbdata.startswith("approve:memecoin:"):
            aid = cbdata.split(":", 2)[2]
            raw = await redis.hget(f"{ns}:mc:pending", aid)
            if raw:
                payload = json.loads(_dec(raw))
                if not _ttl_expired_check(payload.get("created_at", "")):
                    entry = await executor.execute_entry(payload, req.paper_trading, req.slippage_bps)
                    await redis.hdel(f"{ns}:mc:pending", aid)
                    if entry:
                        processed += 1
                        await send_telegram(http, f"[APPROVED] {payload['symbol']} @ ${payload['entry_price_usd']:.8g}")
                else:
                    await redis.hdel(f"{ns}:mc:pending", aid)
        elif cbdata.startswith("skip:memecoin:"):
            aid = cbdata.split(":", 2)[2]
            await redis.hdel(f"{ns}:mc:pending", aid)
            await send_telegram(http, "Skipped the memecoin proposal.")

    await load_memecoin_pending(redis, ns)
    if max_id is not None:
        await redis.set(f"{ns}:tg_offset", str(max_id + 1))
    return processed


async def _process_forex_approvals(http, redis, ns, req, executor) -> int:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        return 0

    offset_raw = await redis.get(f"{ns}:tg_offset")
    params = {"timeout": 0}
    if offset_raw:
        params["offset"] = offset_raw

    try:
        data = await tg_get(http, "getUpdates", params)
    except Exception:
        return 0

    updates = data.get("result", []) or []
    processed = 0
    max_id = None

    for u in updates:
        uid = u.get("update_id")
        if uid is not None:
            max_id = uid
        cq = u.get("callback_query")
        if not cq:
            continue

        cbid = cq.get("id")
        cbdata = cq.get("data", "")
        try:
            await tg_post(http, "answerCallbackQuery", {"callback_query_id": cbid})
        except Exception:
            pass

        if cbdata.startswith("approve:forex:"):
            aid = cbdata.split(":", 2)[2]
            raw = await redis.hget(f"{ns}:fx:pending", aid)
            if raw:
                signal = json.loads(_dec(raw))
                if not _ttl_expired_check(signal.get("created_at", "")):
                    position = await executor.open_position(
                        signal, req.lot_size, req.paper_trading, req.leverage,
                    )
                    await redis.hdel(f"{ns}:fx:pending", aid)
                    if position:
                        processed += 1
                        await send_telegram(http, f"[APPROVED] {signal['pair']} {signal['direction']} @ {signal['entry_price']:.5f}")
                else:
                    await redis.hdel(f"{ns}:fx:pending", aid)
        elif cbdata.startswith("skip:forex:"):
            aid = cbdata.split(":", 2)[2]
            await redis.hdel(f"{ns}:fx:pending", aid)
            await send_telegram(http, "Skipped the forex proposal.")

    await load_forex_pending(redis, ns)
    if max_id is not None:
        await redis.set(f"{ns}:tg_offset", str(max_id + 1))
    return processed


def _ttl_expired_check(created_at: str) -> bool:
    from config.settings import APPROVAL_TTL_SECONDS
    try:
        dt = datetime.fromisoformat(created_at)
        return (datetime.now(timezone.utc) - dt).total_seconds() > APPROVAL_TTL_SECONDS
    except Exception:
        return True


# ══════════════════════════════════════════════════════════════════
#  LEGACY ENDPOINT (backward compatible with original bot)
# ══════════════════════════════════════════════════════════════════

@app.post("/")
async def legacy_run(req: MemecoinBotRequest):
    """Backward-compatible endpoint that runs the memecoin cycle."""
    return await run_memecoin_cycle(req)


if __name__ == "__main__":
    if HAS_CODEWORDS:
        run_service(app)
    else:
        import uvicorn
        port = int(os.environ.get("PORT", "8000"))
        uvicorn.run(app, host="0.0.0.0", port=port)
