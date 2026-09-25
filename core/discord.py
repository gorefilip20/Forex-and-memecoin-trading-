"""Discord webhook notifications."""

import logging
import os

import httpx

logger = logging.getLogger("trading_bot")


def discord_configured() -> bool:
    return bool(os.environ.get("DISCORD_WEBHOOK_URL", "").strip())


async def send_discord(http: httpx.AsyncClient, text: str, username: str = "Trading Bot") -> bool:
    url = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    if not url:
        return False
    try:
        r = await http.post(url, json={
            "content": text,
            "username": username,
        }, timeout=10)
        r.raise_for_status()
        return True
    except Exception as e:
        logger.warning(f"Discord send failed: {e}")
        return False


async def send_discord_embed(
    http: httpx.AsyncClient,
    title: str,
    description: str,
    color: int = 0x00FF00,
    fields: list[dict] | None = None,
    username: str = "Trading Bot",
) -> bool:
    url = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    if not url:
        return False

    embed = {"title": title, "description": description, "color": color}
    if fields:
        embed["fields"] = fields

    try:
        r = await http.post(url, json={
            "username": username,
            "embeds": [embed],
        }, timeout=10)
        r.raise_for_status()
        return True
    except Exception as e:
        logger.warning(f"Discord embed send failed: {e}")
        return False


async def send_discord_signal(http: httpx.AsyncClient, signal: dict) -> bool:
    """Send a formatted forex signal as a Discord embed."""
    direction = signal["direction"]
    color = 0x00CC66 if direction == "BUY" else 0xCC3333

    fields = [
        {"name": "Direction", "value": direction, "inline": True},
        {"name": "Entry", "value": f"{signal['entry_price']:.5f}", "inline": True},
        {"name": "Confidence", "value": f"{signal['confidence']:.0f}%", "inline": True},
        {"name": "Stop Loss", "value": f"{signal['stop_loss']:.5f} ({signal['sl_pips']:.1f} pips)", "inline": True},
        {"name": "Take Profit", "value": f"{signal['take_profit']:.5f} ({signal['tp_pips']:.1f} pips)", "inline": True},
        {"name": "Risk:Reward", "value": f"1:{signal['risk_reward']:.1f}", "inline": True},
    ]

    indicators = signal.get("indicators", {})
    if indicators.get("rsi"):
        fields.append({"name": "RSI", "value": f"{indicators['rsi']:.1f}", "inline": True})
    if indicators.get("trend"):
        fields.append({"name": "Trend", "value": indicators["trend"], "inline": True})

    return await send_discord_embed(
        http,
        title=f"FOREX SIGNAL — {signal['pair']}",
        description=signal.get("reasoning", ""),
        color=color,
        fields=fields,
    )


async def send_discord_trade(http: httpx.AsyncClient, event: dict, market: str) -> bool:
    """Send a trade entry/exit notification as a Discord embed."""
    event_type = event.get("type", "TRADE")
    pnl = event.get("pnl_usd", 0)
    color = 0x00CC66 if pnl > 0 else 0xCC3333 if pnl < 0 else 0x999999

    symbol = event.get("pair", event.get("symbol", "?"))
    direction = event.get("direction", "")

    if event_type in ("ENTRY",):
        title = f"[{market.upper()} ENTRY] {symbol}"
        desc = f"Direction: {direction}\nEntry: {event.get('entry_price', event.get('entry_price_usd', 0)):.5g}"
        color = 0x3399FF
    else:
        title = f"[{market.upper()} {event_type}] {symbol}"
        desc = f"Direction: {direction}\nP&L: ${pnl:+.2f}"

    return await send_discord_embed(http, title=title, description=desc, color=color)
