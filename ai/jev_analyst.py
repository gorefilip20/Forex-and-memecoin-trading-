"""TypeSafe Jev decision engine for forex and memecoin trading.

Drop-in alternative to the OpenAI analyst. Uses Jev's typed judgments
(Choice, Score, Noul) instead of prompting a text model and parsing JSON.
Each decision returns structured probabilities the bot can act on directly.

Set TYPESAFE_API_KEY to enable; falls back to the OpenAI analyst otherwise.
"""

import logging
import os

from typesafe_sdk import AsyncTypeSafeClient, Choice, Noul, Score
from config.settings import (
    MEMECOIN_MIN_TP_PCT, MEMECOIN_MAX_TP_PCT,
    MEMECOIN_MIN_SL_PCT, MEMECOIN_MAX_SL_PCT,
)

logger = logging.getLogger("trading_bot")

_client: AsyncTypeSafeClient | None = None


def _get_client() -> AsyncTypeSafeClient:
    global _client
    if _client is None:
        api_key = os.environ.get("TYPESAFE_API_KEY", "").strip()
        if not api_key:
            raise RuntimeError("TYPESAFE_API_KEY not set")
        _client = AsyncTypeSafeClient(api_key=api_key)
    return _client


def jev_available() -> bool:
    return bool(os.environ.get("TYPESAFE_API_KEY", "").strip())


async def jev_decide_memecoins(tokens: list[dict], open_mints: set) -> list[dict]:
    """Jev selects which memecoins to BUY or SKIP."""
    if not tokens:
        return []

    client = _get_client()
    decisions = []

    for token in tokens:
        mint = token.get("mint", "")
        symbol = token.get("symbol", "?")

        if mint in open_mints:
            decisions.append({
                "mint": mint, "symbol": symbol,
                "action": "SKIP", "confidence": 0,
                "reasoning": "Already holding this token",
                "take_profit_pct": 0, "stop_loss_pct": 0,
            })
            continue

        state = {
            "mint": mint,
            "symbol": symbol,
            "price_usd": token.get("price_usd"),
            "liquidity_usd": token.get("liquidity_usd"),
            "volume_24h": token.get("volume_24h"),
            "market_cap": token.get("market_cap"),
            "buys_1h": token.get("buys_1h"),
            "sells_1h": token.get("sells_1h"),
            "buy_sell_ratio": token.get("buy_sell_ratio"),
            "age_hours": token.get("age_hours"),
            "price_change_5m": token.get("price_change_5m"),
            "price_change_1h": token.get("price_change_1h"),
            "price_change_24h": token.get("price_change_24h"),
            "score": token.get("score"),
        }

        try:
            result = await client.system_one(
                state=state,
                questions={
                    "action": Choice(
                        instructions=(
                            "Should we buy this Solana memecoin right now? "
                            "BUY only when momentum clearly justifies it: high buy/sell ratio (>1.5), "
                            "strong volume, adequate liquidity (>$20k), positive short-term price change, "
                            "token age 2-48h ideal. Watch for dump patterns: falling 1h price change "
                            "with high sell count means SKIP."
                        ),
                        criteria={
                            "BUY": "Strong momentum, good liquidity, buy pressure exceeds sells, fresh token with uptrend.",
                            "SKIP": "Weak signals, dump pattern, too new/old, low liquidity, or poor buy/sell ratio.",
                        },
                    ),
                    "setup_quality": Score(
                        instructions="Rate the overall quality of this memecoin trading setup.",
                        criteria=[
                            "Terrible. Multiple red flags: low liquidity, dump pattern, terrible ratio.",
                            "Weak. One or two positives but overall unconvincing.",
                            "Moderate. Decent signals but nothing exceptional.",
                            "Strong. Good momentum, solid liquidity, strong buy pressure.",
                            "Exceptional. Everything aligns: volume, momentum, ratio, timing.",
                        ],
                    ),
                    "momentum_real": Noul(
                        instructions=(
                            "Is the price momentum genuine (driven by real buying interest) "
                            "rather than artificial (wash trading, single whale, or coordinated pump)?"
                        ),
                    ),
                },
            )

            action = result.answers["action"].choice
            confidence = int(result.answers["action"].confidence * 100)
            quality = result.answers["setup_quality"].score / 4
            momentum_prob = result.answers["momentum_real"].noul

            if quality >= 0.75 and momentum_prob >= 0.7:
                tp_pct = 80.0 + quality * 120.0
                sl_pct = 8.0 + (1 - quality) * 7.0
            elif quality >= 0.5:
                tp_pct = 40.0 + quality * 60.0
                sl_pct = 12.0 + (1 - quality) * 10.0
            else:
                tp_pct = 20.0 + quality * 30.0
                sl_pct = 15.0 + (1 - quality) * 15.0
            tp_pct = max(MEMECOIN_MIN_TP_PCT, min(MEMECOIN_MAX_TP_PCT, tp_pct))
            sl_pct = max(MEMECOIN_MIN_SL_PCT, min(MEMECOIN_MAX_SL_PCT, sl_pct))

            reasoning = (
                f"Quality: {quality:.0%}, momentum real: {momentum_prob:.0%}, "
                f"action confidence: {confidence}%"
            )

            decisions.append({
                "mint": mint,
                "symbol": symbol,
                "action": action,
                "confidence": confidence,
                "reasoning": reasoning,
                "take_profit_pct": round(tp_pct, 1),
                "stop_loss_pct": round(sl_pct, 1),
            })

        except Exception as e:
            logger.warning(f"Jev memecoin decision failed for {symbol}: {e}")
            decisions.append({
                "mint": mint, "symbol": symbol,
                "action": "SKIP", "confidence": 0,
                "reasoning": f"Jev error: {e}",
                "take_profit_pct": 0, "stop_loss_pct": 0,
            })

    return decisions


async def jev_decide_forex(signals: list[dict], open_pairs: set) -> list[dict]:
    """Jev reviews forex signals and confirms/rejects them."""
    if not signals:
        return []

    client = _get_client()
    decisions = []

    for signal in signals:
        pair = signal["pair"]

        if pair in open_pairs:
            decisions.append({
                "pair": pair, "direction": signal["direction"],
                "action": "SKIP", "confidence": 0,
                "reasoning": "Already have an open position on this pair",
                "adjusted_sl_pips": None, "adjusted_tp_pips": None,
            })
            continue

        mtf = signal.get("mtf_confirmation") or {}
        mtf_context = {}
        if mtf:
            mtf_context["higher_tf"] = mtf.get("timeframe")
            mtf_context["higher_trend"] = mtf.get("trend")
            mtf_context["higher_trend_strength"] = mtf.get("trend_strength")
            mtf_context["higher_momentum"] = mtf.get("momentum")
            mtf_context["higher_rsi"] = mtf.get("rsi")
            mtf_context["higher_macd_histogram"] = mtf.get("macd_histogram")
            if mtf.get("top_timeframe"):
                mtf_context["top_tf"] = mtf.get("top_timeframe")
                mtf_context["top_trend"] = mtf.get("top_trend")
                mtf_context["top_trend_strength"] = mtf.get("top_trend_strength")
                mtf_context["top_rsi"] = mtf.get("top_rsi")
                mtf_context["top_momentum"] = mtf.get("top_momentum")
            mtf_context = {k: v for k, v in mtf_context.items() if v is not None}

        state = {
            "pair": pair,
            "direction": signal["direction"],
            "entry_price": signal["entry_price"],
            "stop_loss": signal["stop_loss"],
            "take_profit": signal["take_profit"],
            "sl_pips": signal["sl_pips"],
            "tp_pips": signal["tp_pips"],
            "risk_reward": signal["risk_reward"],
            "confidence": signal["confidence"],
            "timeframe": signal["timeframe"],
            "reasoning": signal["reasoning"],
            "indicators": {
                k: v for k, v in signal.get("indicators", {}).items()
                if v is not None
            },
            "multi_timeframe": mtf_context,
        }

        try:
            result = await client.system_one(
                state=state,
                questions={
                    "action": Choice(
                        instructions=(
                            "Should we execute this forex trade signal? "
                            "EXECUTE only when the technical picture is compelling: "
                            "multiple confirming indicators, trend alignment, risk:reward >= 1.5:1. "
                            "Be skeptical of signals in ranging/choppy markets (ADX < 20). "
                            "Multi-timeframe alignment is critical: check multi_timeframe data — "
                            "when higher_trend and top_trend both align with the trade direction, "
                            "that's a strong confirmation. When they conflict, be very cautious. "
                            "RSI divergence across timeframes (entry TF vs higher TF) often signals reversals."
                        ),
                        criteria={
                            "EXECUTE": "Strong technical setup. Multi-timeframe trend alignment. Good risk/reward.",
                            "SKIP": "Conflicting timeframes, weak signals, poor risk/reward, or choppy conditions.",
                        },
                    ),
                    "signal_strength": Score(
                        instructions="Rate the strength of this forex trading signal based on all available indicators.",
                        criteria=[
                            "Very weak. Conflicting indicators, no clear trend, poor risk/reward.",
                            "Weak. Some alignment but key indicators diverge.",
                            "Moderate. Reasonable alignment, acceptable risk/reward.",
                            "Strong. Most indicators confirm, good risk/reward, clear trend.",
                            "Exceptional. All indicators align, excellent risk/reward, strong trend with momentum.",
                        ],
                    ),
                    "trend_confirmed": Noul(
                        instructions=(
                            "Is the underlying trend genuinely confirmed by multiple timeframes? "
                            "Check multi_timeframe: if higher_trend, top_trend, and the entry timeframe trend "
                            "all agree, confidence should be high. If any conflict (e.g. entry is bullish but "
                            "top_trend is bearish), this is likely counter-trend and risky. "
                            "Also compare RSI levels across timeframes — overbought on higher TF with bullish "
                            "entry signal is a warning."
                        ),
                    ),
                },
            )

            action = result.answers["action"].choice
            confidence = int(result.answers["action"].confidence * 100)
            strength = result.answers["signal_strength"].score / 4
            trend_ok = result.answers["trend_confirmed"].noul

            reasoning = (
                f"Strength: {strength:.0%}, trend confirmed: {trend_ok:.0%}, "
                f"action confidence: {confidence}%"
            )

            decisions.append({
                "pair": pair,
                "direction": signal["direction"],
                "action": action,
                "confidence": confidence,
                "reasoning": reasoning,
                "adjusted_sl_pips": None,
                "adjusted_tp_pips": None,
            })

        except Exception as e:
            logger.warning(f"Jev forex decision failed for {pair}: {e}")
            decisions.append({
                "pair": pair, "direction": signal["direction"],
                "action": "SKIP", "confidence": 0,
                "reasoning": f"Jev error: {e}",
                "adjusted_sl_pips": None, "adjusted_tp_pips": None,
            })

    return decisions


async def jev_check_exit(position: dict, current_price: float, market: str) -> dict:
    """Jev evaluates whether an open position should be closed early."""
    client = _get_client()

    entry = position.get("entry_price", position.get("entry_price_usd", 0))
    direction = position.get("direction", "BUY")
    symbol = position.get("pair", position.get("symbol", "?"))

    if direction in ("BUY", "long"):
        pnl_pct = ((current_price - entry) / entry) * 100 if entry else 0
    else:
        pnl_pct = ((entry - current_price) / entry) * 100 if entry else 0

    sl = position.get("stop_loss", position.get("stop_loss_usd", 0))
    tp = position.get("take_profit", position.get("take_profit_usd", 0))

    sl_distance_pct = abs(entry - sl) / entry * 100 if entry else 0
    tp_distance_pct = abs(tp - entry) / entry * 100 if entry else 0
    progress_to_tp = (pnl_pct / tp_distance_pct * 100) if tp_distance_pct > 0 else 0

    state = {
        "symbol": symbol,
        "market": market,
        "direction": direction,
        "entry_price": entry,
        "current_price": current_price,
        "pnl_pct": round(pnl_pct, 2),
        "stop_loss": sl,
        "take_profit": tp,
        "sl_distance_pct": round(sl_distance_pct, 2),
        "tp_distance_pct": round(tp_distance_pct, 2),
        "progress_to_tp_pct": round(progress_to_tp, 1),
        "trail_active": position.get("trail_active", False),
        "highest_pnl": position.get("highest_pnl_pips", position.get("highest_price", 0)),
    }

    try:
        result = await client.system_one(
            state=state,
            questions={
                "should_exit": Noul(
                    instructions=(
                        "Should this position be closed now, before hitting the take-profit or stop-loss? "
                        "Exit early when: momentum has clearly reversed, price is pulling back from "
                        "a peak toward the stop-loss, the trade has been open too long without progress, "
                        "or risk no longer justifies holding. Keep holding when: price is trending toward "
                        "the take-profit, the position is in decent profit and still moving."
                    ),
                ),
                "should_trail": Noul(
                    instructions=(
                        "Should the stop-loss be tightened to lock in gains? "
                        "Trail the stop when: the position is well in profit (>50% of the way to TP), "
                        "price has made a strong move and is at risk of reversal. "
                        "Don't trail too aggressively if the trend is strong and steady."
                    ),
                ),
            },
        )

        return {
            "should_exit": result.answers["should_exit"].noul,
            "should_trail": result.answers["should_trail"].noul,
        }

    except Exception as e:
        logger.warning(f"Jev exit check failed for {symbol}: {e}")
        return {"should_exit": 0.0, "should_trail": 0.0}
