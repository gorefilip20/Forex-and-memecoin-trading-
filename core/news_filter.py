"""Economic calendar filter — pause trading around high-impact news events.

Fetches upcoming events from ForexFactory's public calendar feed.
Blocks forex entries within a configurable window (default 30 min)
before and after high-impact releases like NFP, CPI, FOMC, etc.
"""

import logging
import os
from datetime import datetime, timezone, timedelta

import httpx

logger = logging.getLogger("trading_bot")

CALENDAR_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"

_cache: dict = {"events": [], "fetched_at": None}
CACHE_TTL_MINUTES = 60


def _news_window_minutes() -> int:
    return int(os.environ.get("NEWS_WINDOW_MINUTES", "30"))


async def fetch_calendar(http: httpx.AsyncClient) -> list[dict]:
    """Fetch this week's economic calendar, cached for 1 hour."""
    now = datetime.now(timezone.utc)
    if (
        _cache["fetched_at"]
        and (now - _cache["fetched_at"]).total_seconds() < CACHE_TTL_MINUTES * 60
        and _cache["events"]
    ):
        return _cache["events"]

    try:
        r = await http.get(CALENDAR_URL, timeout=10)
        r.raise_for_status()
        raw = r.json()

        events = []
        for ev in raw:
            impact = (ev.get("impact") or "").lower()
            if impact not in ("high", "medium"):
                continue
            title = ev.get("title", "")
            country = ev.get("country", "")
            date_str = ev.get("date", "")
            if not date_str:
                continue
            try:
                dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
            except (ValueError, TypeError):
                continue
            events.append({
                "title": title,
                "country": country,
                "impact": impact,
                "datetime": dt,
                "forecast": ev.get("forecast", ""),
                "previous": ev.get("previous", ""),
            })

        _cache["events"] = events
        _cache["fetched_at"] = now
        return events

    except Exception as e:
        logger.warning(f"Economic calendar fetch failed: {e}")
        return _cache.get("events", [])


def _pair_currencies(pair: str) -> set[str]:
    """Extract the two currencies from a forex pair like EUR/USD."""
    parts = pair.upper().replace("/", " ").split()
    currencies = set()
    country_map = {
        "USD": "USD", "EUR": "EUR", "GBP": "GBP", "JPY": "JPY",
        "AUD": "AUD", "NZD": "NZD", "CAD": "CAD", "CHF": "CHF",
        "XAU": "USD", "XAG": "USD", "BTC": "USD", "ETH": "USD",
    }
    for p in parts:
        mapped = country_map.get(p, p)
        currencies.add(mapped)
    return currencies


async def check_news_filter(http: httpx.AsyncClient, pair: str) -> dict:
    """Check if a high-impact news event is near for this pair.

    Returns:
        {"safe": True/False, "events": [...], "reason": "..."}
    """
    events = await fetch_calendar(http)
    if not events:
        return {"safe": True, "events": [], "reason": "No calendar data available"}

    now = datetime.now(timezone.utc)
    window = timedelta(minutes=_news_window_minutes())
    pair_currencies = _pair_currencies(pair)

    blocking = []
    upcoming = []

    for ev in events:
        ev_time = ev["datetime"]
        diff = ev_time - now
        abs_diff = abs(diff.total_seconds())

        country_currency = {
            "USD": "USD", "EUR": "EUR", "GBP": "GBP", "JPY": "JPY",
            "AUD": "AUD", "NZD": "NZD", "CAD": "CAD", "CHF": "CHF",
            "CNY": "CNY",
        }.get(ev["country"], "")

        if country_currency not in pair_currencies:
            continue

        if abs_diff <= window.total_seconds():
            blocking.append({
                "title": ev["title"],
                "country": ev["country"],
                "impact": ev["impact"],
                "time": ev_time.isoformat(),
                "minutes_away": round(diff.total_seconds() / 60, 1),
            })
        elif 0 < diff.total_seconds() <= 7200:
            upcoming.append({
                "title": ev["title"],
                "country": ev["country"],
                "impact": ev["impact"],
                "time": ev_time.isoformat(),
                "minutes_away": round(diff.total_seconds() / 60, 1),
            })

    if blocking:
        high_impact = [e for e in blocking if e["impact"] == "high"]
        if high_impact:
            names = ", ".join(e["title"] for e in high_impact[:3])
            return {
                "safe": False,
                "events": blocking,
                "upcoming": upcoming,
                "reason": f"High-impact news within {_news_window_minutes()}min: {names}",
            }
        return {
            "safe": True,
            "events": blocking,
            "upcoming": upcoming,
            "reason": f"Medium-impact news nearby but not blocking: {blocking[0]['title']}",
            "caution": True,
        }

    return {
        "safe": True,
        "events": [],
        "upcoming": upcoming[:5],
        "reason": "No high-impact news near trading window",
    }
