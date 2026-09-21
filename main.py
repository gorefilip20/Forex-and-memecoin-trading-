"""
Forex & Memecoin Trading Bot
─────────────────────────────
Dual-market autonomous trading platform:
  - Forex: Technical analysis, AI-confirmed signals, paper/live execution via OANDA + MT5
  - Memecoin: DexScreener discovery, enhanced rug detection, Jupiter execution on Solana
  - Shared: Telegram alerts + approval flow, Redis state, unified dashboard
  - Auto-scheduling: background loops run both markets on configurable intervals
"""

import asyncio
import hmac
import json
import logging
import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import httpx
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

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
from core.notify import notify_all, notify_signal, notify_trade
from core.risk import check_daily_drawdown, check_correlation, is_good_session_for_pair, calculate_analytics
from core.news_filter import check_news_filter
from core.equity_tracker import record_trade_result, is_trading_paused, reset_equity_tracker
from memecoin.discovery import discover_candidates
from memecoin.safety import check_token_safety, enhanced_rug_check
from memecoin.executor import MemecoinExecutor
from forex.signals import ForexSignalGenerator
from forex.executor import ForexExecutor
from ai import ai_decide_memecoins, ai_decide_forex

try:
    from codewords_client import AsyncCodewordsClient, logger as cw_logger, redis_client, run_service
    from structlog.contextvars import get_contextvars
    logger = cw_logger
    HAS_CODEWORDS = True
except ImportError:
    import redis.asyncio as aioredis
    logger = logging.getLogger("trading_bot")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
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


# ── Background scheduler state ────────────────────────────────────
_scheduler_tasks: list[asyncio.Task] = []
_bot_started_at: str = ""


def _require_control_secret(token: str | None) -> None:
    """Require an explicit control token for externally-triggered writes."""
    expected = (os.environ.get("CONTROL_API_SECRET") or "").strip()
    if not expected:
        raise HTTPException(status_code=503, detail="CONTROL_API_SECRET is not configured")
    if not token or not hmac.compare_digest(token, expected):
        raise HTTPException(status_code=401, detail="unauthorized")


def _enforce_execution_mode(paper_trading: bool) -> None:
    """Prevent request bodies from turning on live trading accidentally."""
    if not paper_trading and os.environ.get("LIVE_TRADING_UNLOCK", "").lower() != "true":
        raise HTTPException(
            status_code=403,
            detail="live trading is locked; set LIVE_TRADING_UNLOCK=true only after independent review",
        )


async def _notify_error(msg: str):
    """Send error notification to all configured channels."""
    try:
        async with httpx.AsyncClient(timeout=10) as http:
            await notify_all(http, f"[BOT ERROR] {msg}")
    except Exception:
        pass


async def _notify_cycle(msg: str):
    """Send cycle status to all configured channels."""
    try:
        async with httpx.AsyncClient(timeout=10) as http:
            await notify_all(http, msg)
    except Exception:
        pass


async def _auto_memecoin_loop():
    """Run memecoin cycles automatically every N minutes."""
    interval = int(os.environ.get("MEMECOIN_INTERVAL_MINUTES", "15"))
    paper = not (
        os.environ.get("MEMECOIN_LIVE", "").lower() == "true"
        and os.environ.get("LIVE_TRADING_UNLOCK", "").lower() == "true"
    )
    await asyncio.sleep(30)
    await _notify_cycle(f"[AUTO] Memecoin loop active: every {interval}m, {'PAPER' if paper else 'LIVE'}")
    while True:
        try:
            req = MemecoinBotRequest(
                paper_trading=paper,
                approve_first=os.environ.get("APPROVE_FIRST", "").lower() == "true",
                schedule_interval_minutes=interval,
                register_schedule=False,
                min_liquidity_usd=30000,
                min_volume_24h=50000,
                max_age_hours=120,
            )
            result = await run_memecoin_cycle(req)
            logger.info(f"Memecoin auto-cycle done: {result.message}")
            if result.candidates_found == 0:
                await _notify_cycle(f"[SCAN] Memecoin: 0 candidates found this cycle. Next scan in {interval}m.")
        except Exception as e:
            logger.error(f"Memecoin auto-cycle error: {e}")
            await _notify_error(f"Memecoin cycle crashed: {e}")
        await asyncio.sleep(interval * 60)


async def _auto_forex_loop():
    """Run forex cycles automatically every N minutes."""
    interval = int(os.environ.get("FOREX_INTERVAL_MINUTES", "60"))
    paper = not (
        os.environ.get("FOREX_LIVE", "").lower() == "true"
        and os.environ.get("LIVE_TRADING_UNLOCK", "").lower() == "true"
    )
    await asyncio.sleep(60)
    await _notify_cycle(f"[AUTO] Forex loop active: every {interval}m, {'PAPER' if paper else 'LIVE'}")
    while True:
        try:
            fx_pairs = ["EUR/USD", "GBP/USD", "XAU/USD", "BTC/USD", "USD/JPY"]
            req = ForexBotRequest(
                paper_trading=paper,
                approve_first=os.environ.get("APPROVE_FIRST", "").lower() == "true",
                pairs=fx_pairs,
                schedule_interval_minutes=interval,
                register_schedule=False,
                multi_timeframe=False,
            )
            result = await run_forex_cycle(req)
            logger.info(f"Forex auto-cycle done: {result.message}")
            await _notify_cycle(
                f"[SCAN] Forex: {result.pairs_analyzed} pairs, "
                f"{result.signals_generated} signals, "
                f"{result.trades_executed} trades. "
                f"Next scan in {interval}m."
            )
        except Exception as e:
            logger.error(f"Forex auto-cycle error: {e}")
            await _notify_error(f"Forex cycle crashed: {e}")
        await asyncio.sleep(interval * 60)


async def _daily_signal_loop():
    """Send one daily paper-only forex signal scan to Telegram."""
    interval = max(60, int(os.environ.get("DAILY_SIGNAL_INTERVAL_MINUTES", "1440")))
    pairs = [
        p.strip()
        for p in os.environ.get(
            "SIGNAL_PAIRS", "EUR/USD,GBP/USD,USD/JPY,AUD/USD"
        ).split(",")
        if p.strip()
    ]
    await asyncio.sleep(30)
    while True:
        try:
            req = ForexBotRequest(
                paper_trading=True,
                approve_first=False,
                pairs=pairs,
                timeframe=os.environ.get("SIGNAL_TIMEFRAME", "1h"),
                max_positions=1,
                register_schedule=False,
                multi_timeframe=True,
                signal_only=True,
            )
            result = await run_forex_cycle(req)
            if result.signals_generated == 0:
                async with httpx.AsyncClient(timeout=10) as http:
                    await notify_all(
                        http,
                        "DAILY MARKET SCAN\n\n"
                        "No qualified forex setup met the bot's filters today. "
                        "No trade is recommended.",
                    )
            logger.info(
                "Daily signal scan complete: %s signals from %s pairs",
                result.signals_generated,
                result.pairs_analyzed,
            )
        except Exception as e:
            logger.error(f"Daily signal scan error: {e}")
            await _notify_error(f"Daily signal scan failed: {e}")
        await asyncio.sleep(interval * 60)


async def _startup_notification():
    """Send a notification to all configured channels when the bot starts."""
    global _bot_started_at
    _bot_started_at = now_iso()
    async with httpx.AsyncClient(timeout=15) as http:
        mc_live = (
            os.environ.get("MEMECOIN_LIVE", "").lower() == "true"
            and os.environ.get("LIVE_TRADING_UNLOCK", "").lower() == "true"
        )
        fx_live = (
            os.environ.get("FOREX_LIVE", "").lower() == "true"
            and os.environ.get("LIVE_TRADING_UNLOCK", "").lower() == "true"
        )
        from core.discord import discord_configured
        from core.whatsapp import whatsapp_configured
        msg = (
            "Trading Bot Online\n\n"
            f"Memecoin: {'LIVE' if mc_live else 'PAPER'} mode\n"
            f"Forex: {'LIVE' if fx_live else 'PAPER'} mode\n"
            f"Daily signals: {'ON' if os.environ.get('DAILY_SIGNALS', '').lower() == 'true' else 'OFF'}\n"
            f"Notifications: Telegram"
            f"{' + Discord' if discord_configured() else ''}"
            f"{' + WhatsApp' if whatsapp_configured() else ''}\n"
            f"Started: {_bot_started_at}\n\n"
            "Signals are informational only; verify price before acting."
        )
        await notify_all(http, msg)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start background trading loops on server boot."""
    auto_trade = os.environ.get("AUTO_TRADE", "false").lower() == "true"
    if auto_trade:
        _scheduler_tasks.append(asyncio.create_task(_auto_memecoin_loop()))
        _scheduler_tasks.append(asyncio.create_task(_auto_forex_loop()))
    if os.environ.get("DAILY_SIGNALS", "false").lower() == "true":
        _scheduler_tasks.append(asyncio.create_task(_daily_signal_loop()))
    asyncio.create_task(_startup_notification())
    logger.info("Trading bot started, background loops active" if auto_trade else "Trading bot started, manual mode")
    yield
    for t in _scheduler_tasks:
        t.cancel()


app = FastAPI(
    title="Forex & Memecoin Trading Bot",
    description=(
        "Dual-market autonomous trading platform. "
        "Forex: AI-powered signal generation with technical analysis (RSI, MACD, EMA, BB, ATR, Stochastic, ADX). "
        "Memecoin: DexScreener discovery with enhanced rug-pull protection and Jupiter execution on Solana. "
        "MT5 bridge: webhook endpoint for MetaTrader 5 Expert Advisors to fetch and execute signals."
    ),
    version="3.1.0",
    lifespan=lifespan,
)


# ══════════════════════════════════════════════════════════════════
#  MEMECOIN ENDPOINTS
# ══════════════════════════════════════════════════════════════════

@app.post("/memecoin", response_model=MemecoinCycleResponse)
async def memecoin_endpoint(
    req: MemecoinBotRequest,
    x_control_token: str | None = Header(default=None),
):
    """Authenticated HTTP entry point for a memecoin cycle."""
    _require_control_secret(x_control_token)
    return await run_memecoin_cycle(req)


async def run_memecoin_cycle(req: MemecoinBotRequest):
    """Run a memecoin discovery + trading cycle internally."""
    _enforce_execution_mode(req.paper_trading)
    logger.info(f"Memecoin cycle start; paper={req.paper_trading}")
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

            max_dd_pct = float(os.environ.get("DAILY_MAX_DRAWDOWN_PCT", "3.0"))
            dd_check = await check_daily_drawdown(redis, ns, max_dd_pct)
            if dd_check["breached"]:
                msg = (
                    f"Memecoin cycle skipped: daily drawdown limit hit "
                    f"({dd_check['drawdown_pct']:.1f}% / {max_dd_pct}% max)."
                )
                logger.warning(msg)
                await notify_all(http, f"[RISK] {msg}")
                return MemecoinCycleResponse(
                    cycle_time=cycle_time, paper_trading=req.paper_trading, approve_first=req.approve_first,
                    candidates_found=0, approvals_processed=0, alerts_sent=1,
                    decisions=[], entries=[], exits=[], pending_approvals=0,
                    balance_usd=round(balance or 0, 2), open_positions=len(positions),
                    rugs_blocked=0, message=msg,
                )

            pause_state = await is_trading_paused(redis, ns)
            mc_paused = pause_state["paused"]
            if mc_paused:
                logger.warning(f"Equity tracker: {pause_state['reason']}")

            approvals_processed = await _process_memecoin_approvals(http, redis, ns, req, executor)
            balance, positions = await load_memecoin_state(redis, ns)

            exits = await executor.check_exits(positions, req.paper_trading, req.slippage_bps)
            for ex in exits:
                await notify_trade(http, ex, "memecoin")
                pnl = ex.get("pnl_usd", 0)
                if ex.get("type") != "LADDER_EXIT":
                    await record_trade_result(redis, ns, pnl)
                alerts_sent += 1
            balance, positions = await load_memecoin_state(redis, ns)

            candidates = await discover_candidates(
                http, req.min_liquidity_usd, req.min_volume_24h, req.max_age_hours,
            )
            logger.info(f"Memecoin candidates found; count={len(candidates)}")

            open_mints = set(positions.keys())
            pending = await load_memecoin_pending(redis, ns)

            if candidates and (len(positions) + len(pending)) < req.max_positions and not mc_paused:
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
                    logger.warning(
                        f"Token blocked; mint={mint}; symbol={cand['symbol']}; "
                        f"reasons={safety['reasons']}"
                    )
                    rugs_blocked += 1
                    await notify_all(http, f"[BLOCKED] {cand['symbol']}: {'; '.join(safety['reasons'])}")
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
                        entry_event = {
                            "type": "ENTRY", "symbol": payload["symbol"],
                            "direction": "BUY",
                            "entry_price_usd": payload["entry_price_usd"],
                        }
                        await notify_trade(http, entry_event, "memecoin")
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
async def forex_endpoint(
    req: ForexBotRequest,
    x_control_token: str | None = Header(default=None),
):
    """Authenticated HTTP entry point for a forex cycle."""
    _require_control_secret(x_control_token)
    return await run_forex_cycle(req)


async def run_forex_cycle(req: ForexBotRequest):
    """Run a forex analysis + signal + trading cycle internally."""
    _enforce_execution_mode(req.paper_trading)
    logger.info(f"Forex cycle start; paper={req.paper_trading}; pairs={req.pairs}")
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

            max_dd_pct = float(os.environ.get("DAILY_MAX_DRAWDOWN_PCT", "3.0"))
            dd_check = await check_daily_drawdown(redis, ns, max_dd_pct)
            if dd_check["breached"]:
                msg = (
                    f"Forex cycle skipped: daily drawdown limit hit "
                    f"({dd_check['drawdown_pct']:.1f}% / {max_dd_pct}% max). "
                    f"P&L today: ${dd_check['daily_pnl']:.2f} across {dd_check['trades_today']} trades."
                )
                logger.warning(msg)
                await notify_all(http, f"[RISK] {msg}")
                return ForexCycleResponse(
                    cycle_time=cycle_time, paper_trading=req.paper_trading, approve_first=req.approve_first,
                    pairs_analyzed=0, signals_generated=0, trades_executed=0, trades_closed=0,
                    alerts_sent=1, signals=[], open_positions=len(positions),
                    balance_usd=round(balance or 0, 2), equity_usd=round(equity, 2),
                    message=msg,
                )

            pause_state = await is_trading_paused(redis, ns)
            if pause_state["paused"] and not req.signal_only:
                logger.warning(f"Equity tracker: {pause_state['reason']}")
                await notify_all(http, f"[PAUSED] {pause_state['reason']} Switching to signal-only mode.")
                req = ForexBotRequest(**{**req.model_dump(), "signal_only": True})

            approvals = await _process_forex_approvals(http, redis, ns, req, fx_executor)
            balance, equity, positions = await load_forex_state(redis, ns)

            closed = await fx_executor.check_exits(req.paper_trading)
            trades_closed = len(closed)
            for c in closed:
                await notify_trade(http, c, "forex")
                await record_trade_result(redis, ns, c.get("pnl_usd", 0))
                alerts_sent += 1

            balance, equity, positions = await load_forex_state(redis, ns)

            signal_gen = ForexSignalGenerator(http)
            raw_signals = await signal_gen.scan_pairs(req.pairs, req.timeframe, req.multi_timeframe)
            logger.info(f"Forex signals generated; count={len(raw_signals)}")

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

                news = await check_news_filter(http, pair)
                if not news["safe"]:
                    logger.info(f"News filter blocked {pair}: {news['reason']}")
                    continue

                corr = check_correlation(pair, signal["direction"], {k: v for k, v in positions.items()})
                if not corr["ok"]:
                    logger.info(f"Correlation filter: {corr['reason']}")
                    continue

                session_info = is_good_session_for_pair(pair)
                if not session_info["optimal"]:
                    conf = conf * 0.85
                    signal["session_note"] = session_info["reason"]

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

                await notify_signal(http, signal)
                alerts_sent += 1

                if req.signal_only:
                    continue

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
                        entry_event = {
                            "type": "ENTRY", "pair": pair,
                            "direction": signal["direction"],
                            "entry_price": signal["entry_price"],
                        }
                        await notify_trade(http, entry_event, "forex")
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
    pairs: list[str] = ["EUR/USD", "GBP/USD", "USD/JPY", "XAU/USD", "BTC/USD"],
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


@app.get("/analytics")
async def analytics():
    """Detailed trading performance analytics: win rate, Sharpe, drawdown, by-pair breakdown."""
    async with redis_client() as (redis, ns):
        stats = await calculate_analytics(redis, ns)
        dd = await check_daily_drawdown(redis, ns)
        stats["daily_drawdown"] = dd
    return stats


@app.post("/reset", response_model=ResetResponse)
async def reset(x_control_token: str | None = Header(default=None)):
    """Reset all state (paper balances, positions, logs)."""
    _require_control_secret(x_control_token)
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
        await reset_equity_tracker(redis, ns)

    return ResetResponse(reset=True, memecoin_balance_usd=1000.0, forex_balance_usd=10000.0)


# ══════════════════════════════════════════════════════════════════
#  TELEGRAM COMMAND INTERFACE
# ══════════════════════════════════════════════════════════════════

async def _handle_telegram_command(http, redis, ns, msg: dict, req) -> None:
    """Process Telegram text commands from the user."""
    text = (msg.get("text") or "").strip().lower()

    if text in ("status", "/status", "balance", "/balance"):
        mc_bal, mc_pos = await load_memecoin_state(redis, ns)
        fx_bal, _, fx_pos = await load_forex_state(redis, ns)
        pause = await is_trading_paused(redis, ns)
        mode = "PAPER" if req.paper_trading else "LIVE"
        lines = [
            f"Bot Status ({mode})",
            f"Memecoin: ${mc_bal or 0:.2f} | {len(mc_pos)} positions",
            f"Forex: ${fx_bal or 0:.2f} | {len(fx_pos)} positions",
        ]
        if pause["paused"]:
            lines.append(f"PAUSED: {pause['reason']}")
        await send_telegram(http, "\n".join(lines))

    elif text in ("/positions", "positions", "/pos"):
        mc_bal, mc_pos = await load_memecoin_state(redis, ns)
        fx_bal, _, fx_pos = await load_forex_state(redis, ns)
        lines = ["Open Positions\n"]
        if fx_pos:
            lines.append("FOREX:")
            for pid, p in fx_pos.items():
                trail = " [TRAILING]" if p.get("trail_active") else ""
                lines.append(f"  {p['pair']} {p['direction']} @ {p['entry_price']:.5f}{trail}")
        else:
            lines.append("FOREX: none")
        if mc_pos:
            lines.append("\nMEMECOIN:")
            for mint, p in mc_pos.items():
                remaining = p.get("remaining_pct", 100)
                trail = " [TRAILING]" if p.get("trail_active") else ""
                rem_str = f" ({remaining:.0f}% remaining)" if remaining < 100 else ""
                lines.append(f"  {p['symbol']} @ ${p['entry_price_usd']:.8g}{trail}{rem_str}")
        else:
            lines.append("\nMEMECOIN: none")
        await send_telegram(http, "\n".join(lines))

    elif text in ("/analytics", "analytics", "/stats"):
        stats = await calculate_analytics(redis, ns)
        dd = await check_daily_drawdown(redis, ns)
        lines = [
            "Trading Analytics\n",
            f"Total trades: {stats['total_trades']}",
            f"Win rate: {stats.get('win_rate', 0):.1f}%",
            f"Profit factor: {stats.get('profit_factor', 0):.2f}",
            f"Sharpe ratio: {stats.get('sharpe_estimate', 0):.2f}",
            f"Total P&L: ${stats.get('total_pnl', 0):.2f}",
            f"Max drawdown: ${stats.get('max_drawdown', 0):.2f}",
            f"Avg win: ${stats.get('avg_win', 0):.2f}",
            f"Avg loss: ${stats.get('avg_loss', 0):.2f}",
            f"\nToday: ${dd['daily_pnl']:.2f} ({dd['trades_today']} trades)",
        ]
        await send_telegram(http, "\n".join(lines))

    elif text in ("/pause", "pause"):
        from core.equity_tracker import record_trade_result
        state = {"consecutive_losses": 99, "consecutive_wins": 0, "paused": True, "recovery_wins": 0}
        await redis.set(f"{ns}:equity_tracker", json.dumps(state))
        await send_telegram(http, "Bot PAUSED. Will only send signals, no new trades. Send /resume to restart.")

    elif text in ("/resume", "resume"):
        await reset_equity_tracker(redis, ns)
        await send_telegram(http, "Bot RESUMED. Trading is active again.")

    elif text in ("/help", "help"):
        await send_telegram(http, (
            "Bot Commands\n\n"
            "/status - Balances and mode\n"
            "/positions - All open positions\n"
            "/analytics - Win rate, P&L, Sharpe\n"
            "/pause - Stop new trades (signals only)\n"
            "/resume - Resume trading\n"
            "/help - This message"
        ))


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
                await _handle_telegram_command(http, redis, ns, msg, req)
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
#  HEALTH CHECK
# ══════════════════════════════════════════════════════════════════

@app.get("/health")
async def health():
    """Health check endpoint for Railway / load balancers."""
    redis_ok = False
    try:
        async with redis_client() as (redis, ns):
            await redis.ping()
            redis_ok = True
    except Exception:
        pass
    return {
        "status": "healthy",
        "redis": redis_ok,
        "started_at": _bot_started_at or None,
        "version": app.version,
    }


# ══════════════════════════════════════════════════════════════════
#  MT5 BRIDGE  –  webhook endpoints for MetaTrader 5 Expert Advisors
# ══════════════════════════════════════════════════════════════════

class MT5SignalRequest(BaseModel):
    secret: str = Field(..., description="Shared secret for authentication")
    action: str = Field("get_signals", description="get_signals | confirm_trade | get_status")
    pair: str | None = None
    trade_id: str | None = None
    fill_price: float | None = None


@app.post("/mt5/webhook")
async def mt5_webhook(req: MT5SignalRequest):
    """Endpoint for MT5 EA to fetch signals and report fills."""
    expected = os.environ.get("MT5_SECRET", "")
    if not expected or not hmac.compare_digest(req.secret, expected):
        return {"error": "unauthorized"}

    if req.action == "get_signals":
        async with redis_client() as (redis, ns):
            _, _, positions = await load_forex_state(redis, ns)
            pending = await load_forex_pending(redis, ns)
            open_pairs = {p["pair"] for p in positions.values()}
            available = []
            for aid, sig in pending.items():
                if sig["pair"] not in open_pairs:
                    available.append({
                        "id": aid,
                        "pair": sig["pair"],
                        "direction": sig["direction"],
                        "entry_price": sig["entry_price"],
                        "sl": sig["stop_loss"],
                        "tp": sig["take_profit"],
                        "confidence": sig.get("confidence", 0),
                        "lot_size": float(os.environ.get("FOREX_LOT_SIZE", "0.01")),
                    })
            return {"signals": available, "open_positions": len(positions)}

    elif req.action == "confirm_trade":
        if os.environ.get("LIVE_TRADING_UNLOCK", "").lower() != "true":
            return {"error": "live trading is locked"}
        if not req.trade_id:
            return {"error": "trade_id required"}
        async with httpx.AsyncClient(timeout=15) as http:
            async with redis_client() as (redis, ns):
                raw = await redis.hget(f"{ns}:fx:pending", req.trade_id)
                if not raw:
                    return {"error": "signal not found or already used"}
                signal = json.loads(_dec(raw))
                executor = ForexExecutor(http, redis, ns)
                lot = float(os.environ.get("FOREX_LOT_SIZE", "0.01"))
                position = await executor.open_position(signal, lot, paper_trading=False, leverage=100)
                await redis.hdel(f"{ns}:fx:pending", req.trade_id)
                if req.fill_price:
                    signal["fill_price_mt5"] = req.fill_price
                await send_telegram(http, f"[MT5] Opened {signal['pair']} {signal['direction']} @ {req.fill_price or signal['entry_price']:.5f}")
                return {"status": "confirmed", "pair": signal["pair"]}

    elif req.action == "get_status":
        async with redis_client() as (redis, ns):
            _, _, positions = await load_forex_state(redis, ns)
            pos_list = []
            for pid, p in positions.items():
                pos_list.append({
                    "id": pid,
                    "pair": p["pair"],
                    "direction": p["direction"],
                    "entry_price": p["entry_price"],
                    "sl": p["sl"],
                    "tp": p["tp"],
                    "unrealized_pnl": p.get("unrealized_pnl", 0),
                })
            return {"positions": pos_list}

    return {"error": f"unknown action: {req.action}"}


# ══════════════════════════════════════════════════════════════════
#  BROWSER TRIGGERS  –  GET endpoints you can open in a browser
# ══════════════════════════════════════════════════════════════════

@app.get("/trigger/memecoin")
async def trigger_memecoin(x_control_token: str | None = Header(default=None)):
    """Run a memecoin discovery + trading cycle (browser-friendly)."""
    _require_control_secret(x_control_token)
    paper = not (
        os.environ.get("MEMECOIN_LIVE", "").lower() == "true"
        and os.environ.get("LIVE_TRADING_UNLOCK", "").lower() == "true"
    )
    req = MemecoinBotRequest(
        paper_trading=paper,
        approve_first=os.environ.get("APPROVE_FIRST", "").lower() == "true",
    )
    result = await run_memecoin_cycle(req)
    return {"triggered": "memecoin", "paper": paper, "message": result.message}


@app.get("/trigger/forex")
async def trigger_forex(x_control_token: str | None = Header(default=None)):
    """Run a forex analysis + trading cycle (browser-friendly)."""
    _require_control_secret(x_control_token)
    paper = not (
        os.environ.get("FOREX_LIVE", "").lower() == "true"
        and os.environ.get("LIVE_TRADING_UNLOCK", "").lower() == "true"
    )
    req = ForexBotRequest(
        paper_trading=paper,
        approve_first=os.environ.get("APPROVE_FIRST", "").lower() == "true",
    )
    result = await run_forex_cycle(req)
    return {"triggered": "forex", "paper": paper, "message": result.message}


@app.get("/trigger/daily-signal")
async def trigger_daily_signal(x_control_token: str | None = Header(default=None)):
    """Run one authenticated, signal-only scan and send results to Telegram."""
    _require_control_secret(x_control_token)
    req = ForexBotRequest(
        paper_trading=True,
        approve_first=False,
        pairs=[
            p.strip()
            for p in os.environ.get(
                "SIGNAL_PAIRS", "EUR/USD,GBP/USD,USD/JPY,AUD/USD"
            ).split(",")
            if p.strip()
        ],
        timeframe=os.environ.get("SIGNAL_TIMEFRAME", "1h"),
        max_positions=1,
        register_schedule=False,
        multi_timeframe=True,
        signal_only=True,
    )
    result = await run_forex_cycle(req)
    return {
        "triggered": "daily-signal",
        "signal_only": True,
        "signals_generated": result.signals_generated,
        "message": result.message,
    }


@app.get("/trigger/analyze/{pair}")
async def trigger_analyze_pair(pair: str, x_control_token: str | None = Header(default=None)):
    """Analyze a single pair and send signal to Telegram (browser-friendly)."""
    _require_control_secret(x_control_token)
    async with httpx.AsyncClient(timeout=25) as http:
        generator = ForexSignalGenerator(http)
        signal = await generator.analyze_pair(pair, "1h", multi_timeframe=True)
        if signal and signal.get("confidence", 0) > 0:
            sent = await notify_signal(http, signal)
            return {"pair": pair, "signal": signal, "notifications_sent": sent}
        return {"pair": pair, "signal": signal, "notifications_sent": 0}


# ══════════════════════════════════════════════════════════════════
#  LEGACY ENDPOINT (backward compatible with original bot)
# ══════════════════════════════════════════════════════════════════

@app.post("/")
async def legacy_run(req: MemecoinBotRequest, x_control_token: str | None = Header(default=None)):
    """Backward-compatible endpoint that runs the memecoin cycle."""
    _require_control_secret(x_control_token)
    return await run_memecoin_cycle(req)


if __name__ == "__main__":
    if HAS_CODEWORDS:
        run_service(app)
    else:
        import uvicorn
        port = int(os.environ.get("PORT", "8000"))
        uvicorn.run(app, host="0.0.0.0", port=port)
