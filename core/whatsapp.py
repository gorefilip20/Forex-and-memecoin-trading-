"""WhatsApp Cloud API notifications via Meta Business Platform.

Setup:
1. Create a Meta Business account at business.facebook.com
2. Create a WhatsApp Business app at developers.facebook.com
3. Get your Phone Number ID and permanent access token
4. Add your recipient phone number (with country code, no +)
5. Set WHATSAPP_PHONE_NUMBER_ID, WHATSAPP_ACCESS_TOKEN, WHATSAPP_RECIPIENT in .env
"""

import logging
import os

import httpx

logger = logging.getLogger("trading_bot")

WHATSAPP_API = "https://graph.facebook.com/v21.0"


def whatsapp_configured() -> bool:
    return all([
        os.environ.get("WHATSAPP_PHONE_NUMBER_ID", "").strip(),
        os.environ.get("WHATSAPP_ACCESS_TOKEN", "").strip(),
        os.environ.get("WHATSAPP_RECIPIENT", "").strip(),
    ])


async def send_whatsapp(http: httpx.AsyncClient, text: str) -> bool:
    phone_id = os.environ.get("WHATSAPP_PHONE_NUMBER_ID", "").strip()
    token = os.environ.get("WHATSAPP_ACCESS_TOKEN", "").strip()
    recipient = os.environ.get("WHATSAPP_RECIPIENT", "").strip()

    if not all([phone_id, token, recipient]):
        return False

    try:
        r = await http.post(
            f"{WHATSAPP_API}/{phone_id}/messages",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "messaging_product": "whatsapp",
                "to": recipient,
                "type": "text",
                "text": {"body": text},
            },
            timeout=15,
        )
        r.raise_for_status()
        return True
    except Exception as e:
        logger.warning(f"WhatsApp send failed: {e}")
        return False


async def send_whatsapp_signal(http: httpx.AsyncClient, signal: dict) -> bool:
    """Send a formatted forex signal via WhatsApp."""
    direction_arrow = "UP" if signal["direction"] == "BUY" else "DOWN"
    text = (
        f"*FOREX SIGNAL — {signal['pair']}*\n\n"
        f"Direction: {direction_arrow} {signal['direction']}\n"
        f"Entry: {signal['entry_price']:.5f}\n"
        f"Stop Loss: {signal['stop_loss']:.5f} ({signal['sl_pips']:.1f} pips)\n"
        f"Take Profit: {signal['take_profit']:.5f} ({signal['tp_pips']:.1f} pips)\n"
        f"Risk:Reward = 1:{signal['risk_reward']:.1f}\n"
        f"Confidence: {signal['confidence']:.0f}%\n"
        f"Timeframe: {signal.get('timeframe', '1h')}\n\n"
        f"{signal.get('reasoning', '')}"
    )
    return await send_whatsapp(http, text)
