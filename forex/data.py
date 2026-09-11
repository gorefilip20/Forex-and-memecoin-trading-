"""Forex market data fetching from free APIs (TwelveData primary, Alpha Vantage fallback)."""

import os
import logging
from typing import Optional

import httpx

from config.settings import TWELVE_DATA_BASE, ALPHA_VANTAGE_BASE

logger = logging.getLogger("trading_bot")

TIMEFRAME_MAP_TWELVE = {
    "5m": "5min", "15m": "15min", "30m": "30min",
    "1h": "1h", "4h": "4h", "1d": "1day",
}

TIMEFRAME_MAP_AV = {
    "5m": ("TIME_SERIES_INTRADAY", "5min"),
    "15m": ("TIME_SERIES_INTRADAY", "15min"),
    "30m": ("TIME_SERIES_INTRADAY", "30min"),
    "1h": ("TIME_SERIES_INTRADAY", "60min"),
    "4h": ("TIME_SERIES_INTRADAY", "240min"),
    "1d": ("TIME_SERIES_DAILY", None),
}


async def fetch_candles(
    http: httpx.AsyncClient,
    symbol: str,
    timeframe: str = "1h",
    count: int = 100,
) -> list[dict]:
    """Fetch OHLCV candles. Tries TwelveData first, falls back to Alpha Vantage."""
    candles = await _fetch_twelvedata(http, symbol, timeframe, count)
    if candles:
        return candles

    candles = await _fetch_alphavantage(http, symbol, timeframe, count)
    if candles:
        return candles

    logger.warning(f"All data sources failed for {symbol} {timeframe}")
    return []


async def fetch_current_price(http: httpx.AsyncClient, symbol: str) -> Optional[float]:
    """Get the latest price for a forex pair."""
    api_key = os.environ.get("TWELVE_DATA_API_KEY", "")
    symbol_clean = symbol.replace("/", "")

    if api_key:
        try:
            r = await http.get(
                f"{TWELVE_DATA_BASE}/price",
                params={"symbol": symbol_clean, "apikey": api_key},
                timeout=10,
            )
            r.raise_for_status()
            data = r.json()
            if "price" in data:
                return float(data["price"])
        except Exception as e:
            logger.warning(f"TwelveData price fetch failed for {symbol}: {e}")

    candles = await fetch_candles(http, symbol, "5m", 1)
    if candles:
        return candles[0]["close"]

    return None


async def _fetch_twelvedata(
    http: httpx.AsyncClient,
    symbol: str,
    timeframe: str,
    count: int,
) -> list[dict]:
    api_key = os.environ.get("TWELVE_DATA_API_KEY", "")
    if not api_key:
        return []

    td_interval = TIMEFRAME_MAP_TWELVE.get(timeframe)
    if not td_interval:
        return []

    symbol_clean = symbol.replace("/", "")
    try:
        r = await http.get(
            f"{TWELVE_DATA_BASE}/time_series",
            params={
                "symbol": symbol_clean,
                "interval": td_interval,
                "outputsize": str(count),
                "apikey": api_key,
                "format": "JSON",
            },
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()

        if "values" not in data:
            logger.warning(f"TwelveData returned no values for {symbol}: {data.get('message', '')}")
            return []

        candles = []
        for v in reversed(data["values"]):
            candles.append({
                "timestamp": v["datetime"],
                "open": float(v["open"]),
                "high": float(v["high"]),
                "low": float(v["low"]),
                "close": float(v["close"]),
                "volume": float(v.get("volume", 0)),
            })
        return candles

    except Exception as e:
        logger.warning(f"TwelveData fetch failed: {e}")
        return []


async def _fetch_alphavantage(
    http: httpx.AsyncClient,
    symbol: str,
    timeframe: str,
    count: int,
) -> list[dict]:
    api_key = os.environ.get("ALPHA_VANTAGE_API_KEY", "")
    if not api_key:
        return []

    mapping = TIMEFRAME_MAP_AV.get(timeframe)
    if not mapping:
        return []

    function, interval = mapping
    symbol_clean = symbol.replace("/", "")
    params = {
        "function": function,
        "symbol": symbol_clean,
        "apikey": api_key,
        "outputsize": "compact",
    }
    if interval:
        params["interval"] = interval

    try:
        r = await http.get(ALPHA_VANTAGE_BASE, params=params, timeout=15)
        r.raise_for_status()
        data = r.json()

        ts_key = None
        for key in data:
            if "Time Series" in key:
                ts_key = key
                break

        if not ts_key:
            return []

        series = data[ts_key]
        candles = []
        for dt_str, values in sorted(series.items()):
            o = float(values.get("1. open", 0))
            h = float(values.get("2. high", 0))
            lo = float(values.get("3. low", 0))
            c = float(values.get("4. close", 0))
            vol = float(values.get("5. volume", 0)) if "5. volume" in values else 0
            candles.append({
                "timestamp": dt_str,
                "open": o,
                "high": h,
                "low": lo,
                "close": c,
                "volume": vol,
            })

        return candles[-count:]

    except Exception as e:
        logger.warning(f"Alpha Vantage fetch failed: {e}")
        return []
