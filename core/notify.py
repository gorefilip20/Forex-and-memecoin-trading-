"""Unified notification dispatcher — sends to all configured channels."""

import httpx

from core.telegram import send_telegram, send_forex_signal as tg_forex_signal
from core.discord import discord_configured, send_discord, send_discord_signal, send_discord_trade
from core.whatsapp import whatsapp_configured, send_whatsapp, send_whatsapp_signal


async def notify_all(http: httpx.AsyncClient, text: str) -> int:
    """Send a text message to every configured channel. Returns count of successful sends."""
    sent = 0
    if await send_telegram(http, text):
        sent += 1
    if discord_configured() and await send_discord(http, text):
        sent += 1
    if whatsapp_configured() and await send_whatsapp(http, text):
        sent += 1
    return sent


async def notify_signal(http: httpx.AsyncClient, signal: dict) -> int:
    """Send a formatted forex signal to every configured channel."""
    sent = 0
    if await tg_forex_signal(http, signal):
        sent += 1
    if discord_configured() and await send_discord_signal(http, signal):
        sent += 1
    if whatsapp_configured() and await send_whatsapp_signal(http, signal):
        sent += 1
    return sent


async def notify_trade(http: httpx.AsyncClient, event: dict, market: str) -> int:
    """Send a trade entry/exit notification to every configured channel."""
    pnl = event.get("pnl_usd")
    symbol = event.get("pair", event.get("symbol", "?"))
    direction = event.get("direction", "")
    event_type = event.get("type", "TRADE")

    if event_type == "ENTRY":
        price = event.get("entry_price", event.get("entry_price_usd", 0))
        text = f"[{market.upper()} ENTRY] {symbol} {direction} @ {price:.5g}"
    elif event_type == "LADDER_EXIT":
        sell_pct = event.get("sell_pct", 0)
        remaining = event.get("remaining_pct", 0)
        ladder_lvl = event.get("ladder_level", 0)
        text = (
            f"[{market.upper()} PARTIAL EXIT] {symbol}: sold {sell_pct:.0f}% at +{ladder_lvl:.0f}% "
            f"(${pnl:+.2f}), {remaining:.0f}% still riding"
        )
    else:
        text = f"[{market.upper()} {event_type}] {symbol} {direction}: ${pnl:+.2f}"

    sent = 0
    if await send_telegram(http, text):
        sent += 1
    if discord_configured() and await send_discord_trade(http, event, market):
        sent += 1
    if whatsapp_configured() and await send_whatsapp(http, text):
        sent += 1
    return sent
