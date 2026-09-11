"""AI decision engine for both forex and memecoin trading."""

import json
import logging

from openai import AsyncOpenAI

from config.settings import (
    AI_MODEL,
    MEMECOIN_MIN_TP_PCT, MEMECOIN_MAX_TP_PCT,
    MEMECOIN_MIN_SL_PCT, MEMECOIN_MAX_SL_PCT,
    FOREX_MIN_SL_PIPS, FOREX_MAX_SL_PIPS,
    FOREX_MIN_TP_PIPS, FOREX_MAX_TP_PIPS,
)

logger = logging.getLogger("trading_bot")


async def ai_decide_memecoins(tokens: list[dict], open_mints: set) -> list[dict]:
    """AI selects which memecoins to BUY or SKIP based on market data."""
    if not tokens:
        return []

    compact = [
        {k: t[k] for k in (
            "mint", "symbol", "price_usd", "liquidity_usd", "volume_24h",
            "market_cap", "buys_1h", "sells_1h", "buy_sell_ratio", "age_hours",
            "price_change_5m", "price_change_1h", "price_change_24h", "score",
        ) if k in t}
        for t in tokens
    ]

    already = ", ".join(sorted(open_mints)) or "none"
    system = (
        "You are a disciplined Solana memecoin trading analyst. "
        "Given live market data, decide BUY or SKIP for each token. "
        f"Mints already open (do NOT re-buy): {already}. "
        "Only BUY when momentum clearly justifies it: "
        "high buy/sell ratio (>1.5), strong volume, adequate liquidity, "
        "positive short-term price change, fresh but not brand-new (2-48h ideal). "
        "Watch for dump patterns: falling 1h price change with high sell count = SKIP. "
        f"Set take_profit_pct between {MEMECOIN_MIN_TP_PCT} and {MEMECOIN_MAX_TP_PCT}, "
        f"stop_loss_pct between {MEMECOIN_MIN_SL_PCT} and {MEMECOIN_MAX_SL_PCT}. "
        "Set confidence as an INTEGER 0-100. BUY requires confidence >= 55. "
        'Return ONLY a JSON object: {"decisions":[{"mint":"<exact>","symbol":"<symbol>",'
        '"action":"BUY|SKIP","confidence":0,"reasoning":"<short>",'
        '"take_profit_pct":0,"stop_loss_pct":0}]}. '
        "Copy the mint string EXACTLY from input."
    )
    user = "Candidates:\n" + json.dumps(compact, indent=1)

    return await _call_ai(system, user)


async def ai_decide_forex(signals: list[dict], open_pairs: set) -> list[dict]:
    """AI reviews forex signals and confirms/rejects/adjusts them."""
    if not signals:
        return []

    compact = []
    for s in signals:
        compact.append({
            "pair": s["pair"],
            "direction": s["direction"],
            "entry_price": s["entry_price"],
            "stop_loss": s["stop_loss"],
            "take_profit": s["take_profit"],
            "sl_pips": s["sl_pips"],
            "tp_pips": s["tp_pips"],
            "risk_reward": s["risk_reward"],
            "confidence": s["confidence"],
            "timeframe": s["timeframe"],
            "reasoning": s["reasoning"],
            "indicators": {
                k: v for k, v in s.get("indicators", {}).items()
                if v is not None
            },
        })

    already = ", ".join(sorted(open_pairs)) or "none"
    system = (
        "You are an experienced forex trading analyst. "
        "Review these technical signals and decide EXECUTE or SKIP for each. "
        f"Pairs already open (do NOT double up): {already}. "
        "Only EXECUTE when the technical picture is compelling: "
        "multiple confirming indicators, trend alignment, adequate risk:reward (>= 1.5:1). "
        "Be skeptical of signals in ranging/choppy markets with ADX < 20. "
        "Favor signals with multi-timeframe confirmation. "
        "For EVERY decision, set confidence as an INTEGER 0-100. "
        "EXECUTE requires confidence >= 60. "
        'Return ONLY a JSON object: {"decisions":[{"pair":"<pair>","direction":"BUY|SELL",'
        '"action":"EXECUTE|SKIP","confidence":0,"reasoning":"<short>",'
        '"adjusted_sl_pips":null,"adjusted_tp_pips":null}]}. '
        "Set adjusted_sl_pips / adjusted_tp_pips only if you want to override the technical levels."
    )
    user = "Signals for review:\n" + json.dumps(compact, indent=1)

    return await _call_ai(system, user)


async def _call_ai(system: str, user: str) -> list[dict]:
    client = AsyncOpenAI()
    try:
        resp = await client.chat.completions.create(
            model=AI_MODEL,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            response_format={"type": "json_object"},
            temperature=0.2,
            max_tokens=2000,
        )
        raw = (resp.choices[0].message.content or "{}").strip()
        data = json.loads(raw)
        return data.get("decisions", [])
    except json.JSONDecodeError as e:
        logger.warning(f"AI returned invalid JSON: {e}")
        return []
    except Exception as e:
        logger.warning(f"AI call failed: {e}")
        return []
