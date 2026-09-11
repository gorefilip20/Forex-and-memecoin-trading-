import os
import logging
import httpx

from config.settings import TELEGRAM_API

logger = logging.getLogger("trading_bot")


async def tg_get(http: httpx.AsyncClient, method: str, params: dict) -> dict:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        return {}
    r = await http.get(f"{TELEGRAM_API}/bot{token}/{method}", params=params, timeout=20)
    r.raise_for_status()
    return r.json()


async def tg_post(http: httpx.AsyncClient, method: str, payload: dict) -> dict:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        return {}
    r = await http.post(f"{TELEGRAM_API}/bot{token}/{method}", json=payload, timeout=20)
    r.raise_for_status()
    return r.json()


async def send_telegram(http: httpx.AsyncClient, text: str) -> bool:
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not chat_id:
        return False
    try:
        await tg_post(http, "sendMessage", {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": True,
        })
        return True
    except Exception as e:
        logger.warning(f"Telegram send failed: {e}")
        return False


async def send_approval_message(http: httpx.AsyncClient, aid: str, payload: dict, market: str = "memecoin") -> bool:
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not chat_id:
        return False

    kb = {"inline_keyboard": [[
        {"text": "Approve", "callback_data": f"approve:{market}:{aid}"},
        {"text": "Skip", "callback_data": f"skip:{market}:{aid}"},
    ]]}

    if market == "memecoin":
        text = (
            f"<b>NEW MEMECOIN TRADE — {payload['symbol']}</b>\n"
            f"DEX: {payload.get('dex', '?')}\n"
            f"Price: ${payload['entry_price_usd']:.8g}\n"
            f"Cost: ${payload['cost_usd']:.2f}\n"
            f"Take-profit: +{payload['take_profit_pct']:.0f}%\n"
            f"Stop-loss: -{payload['stop_loss_pct']:.0f}%\n"
            f"Confidence: {payload['confidence']:.0f}/100\n"
            f"Liquidity: ${payload['liquidity_usd']:,.0f}\n"
            f"Safety: Mint renounced / Freeze renounced\n\n"
            f"<i>{payload.get('reasoning', '')}</i>\n\n"
            f"Tap <b>Approve</b> to buy, or <b>Skip</b> to pass. Expires in ~30 min."
        )
    else:
        text = (
            f"<b>NEW FOREX SIGNAL — {payload['pair']}</b>\n"
            f"Direction: {payload['direction']}\n"
            f"Entry: {payload['entry_price']:.5f}\n"
            f"Stop Loss: {payload['stop_loss']:.5f} ({payload['sl_pips']:.1f} pips)\n"
            f"Take Profit: {payload['take_profit']:.5f} ({payload['tp_pips']:.1f} pips)\n"
            f"Risk:Reward = 1:{payload['risk_reward']:.1f}\n"
            f"Confidence: {payload['confidence']:.0f}/100\n"
            f"Timeframe: {payload.get('timeframe', '1h')}\n\n"
            f"<i>{payload.get('reasoning', '')}</i>\n\n"
            f"Tap <b>Approve</b> to execute, or <b>Skip</b> to pass."
        )

    try:
        await tg_post(http, "sendMessage", {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "reply_markup": kb,
            "disable_web_page_preview": True,
        })
        return True
    except Exception as e:
        logger.warning(f"Approval message send failed: {e}")
        return False


async def send_forex_signal(http: httpx.AsyncClient, signal: dict) -> bool:
    direction_emoji = "UP" if signal["direction"] == "BUY" else "DOWN"
    text = (
        f"FOREX SIGNAL — {signal['pair']}\n\n"
        f"Direction: {direction_emoji} {signal['direction']}\n"
        f"Entry: {signal['entry_price']:.5f}\n"
        f"Stop Loss: {signal['stop_loss']:.5f} ({signal['sl_pips']:.1f} pips)\n"
        f"Take Profit: {signal['take_profit']:.5f} ({signal['tp_pips']:.1f} pips)\n"
        f"Risk:Reward = 1:{signal['risk_reward']:.1f}\n"
        f"Confidence: {signal['confidence']:.0f}%\n"
        f"Timeframe: {signal.get('timeframe', '1h')}\n\n"
        f"Indicators:\n"
    )

    indicators = signal.get("indicators", {})
    if indicators.get("rsi"):
        text += f"  RSI: {indicators['rsi']:.1f}\n"
    if indicators.get("macd_histogram"):
        text += f"  MACD Hist: {indicators['macd_histogram']:.6f}\n"
    if indicators.get("trend"):
        text += f"  Trend: {indicators['trend']}\n"
    if indicators.get("support"):
        text += f"  Support: {indicators['support']:.5f}\n"
    if indicators.get("resistance"):
        text += f"  Resistance: {indicators['resistance']:.5f}\n"

    text += f"\n{signal.get('reasoning', '')}"

    return await send_telegram(http, text)
