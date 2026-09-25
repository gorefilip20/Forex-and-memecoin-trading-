"""Token discovery via DexScreener with enhanced filtering."""

import logging
import os
import time
from typing import Optional

import httpx

from config.settings import (
    DEXSCREENER_BASE, SOL_MINT, MEMECOIN_AI_CANDIDATE_LIMIT,
)
from core.state import _f

logger = logging.getLogger("trading_bot")


async def fetch_sol_usd_price(http: httpx.AsyncClient) -> Optional[float]:
    try:
        r = await http.get(f"{DEXSCREENER_BASE}/latest/dex/tokens/{SOL_MINT}", timeout=15)
        r.raise_for_status()
        pairs = r.json().get("pairs", [])
        if not pairs:
            return None
        best = max(pairs, key=lambda p: (p.get("liquidity", {}).get("usd", 0) or 0))
        return _f(best.get("priceUsd"))
    except Exception as e:
        logger.warning(f"SOL price fetch failed: {e}")
        return None


async def fetch_token_price_usd(http: httpx.AsyncClient, mint: str) -> Optional[float]:
    try:
        r = await http.get(f"{DEXSCREENER_BASE}/latest/dex/tokens/{mint}", timeout=15)
        r.raise_for_status()
        pairs = r.json().get("pairs", [])
        if not pairs:
            return None
        sol_pairs = [p for p in pairs if p.get("chainId") == "solana"] or pairs
        best = max(sol_pairs, key=lambda p: (p.get("liquidity", {}).get("usd", 0) or 0))
        return _f(best.get("priceUsd"))
    except Exception as e:
        logger.warning(f"Price fetch failed for {mint}: {e}")
        return None


async def _fetch_solana_mints_from_endpoint(http: httpx.AsyncClient, url: str) -> list[str]:
    """Fetch Solana token mints from a DexScreener endpoint."""
    try:
        r = await http.get(url, timeout=20)
        r.raise_for_status()
        data = r.json()
        if isinstance(data, list):
            return [
                p.get("tokenAddress")
                for p in data
                if p.get("chainId") == "solana" and p.get("tokenAddress")
            ]
    except Exception as e:
        logger.warning(f"DexScreener fetch failed for {url}: {e}")
    return []


async def _fetch_trending_solana(http: httpx.AsyncClient) -> list[str]:
    """Fetch trending Solana pairs from DexScreener search."""
    try:
        r = await http.get(
            f"{DEXSCREENER_BASE}/latest/dex/search",
            params={"q": "SOL"},
            timeout=20,
        )
        r.raise_for_status()
        pairs = r.json().get("pairs", [])
        mints = []
        for p in pairs:
            if p.get("chainId") == "solana":
                base = p.get("baseToken", {})
                addr = base.get("address")
                if addr and addr != SOL_MINT:
                    mints.append(addr)
        return mints
    except Exception as e:
        logger.warning(f"Trending fetch failed: {e}")
        return []


async def check_sol_trend(http: httpx.AsyncClient) -> dict:
    """Check SOL/USD price trend to filter memecoin buys in downtrends."""
    try:
        r = await http.get(f"{DEXSCREENER_BASE}/latest/dex/tokens/{SOL_MINT}", timeout=15)
        r.raise_for_status()
        pairs = r.json().get("pairs", [])
        if not pairs:
            return {"ok": True, "change_1h": 0, "reason": "no data"}
        best = max(pairs, key=lambda p: (p.get("liquidity", {}).get("usd", 0) or 0))
        change_1h = _f((best.get("priceChange", {}) or {}).get("h1"))
        change_5m = _f((best.get("priceChange", {}) or {}).get("m5"))
        min_change = float(os.environ.get("SOL_TREND_MIN_CHANGE_1H", "-3.0"))
        return {
            "ok": change_1h > min_change,
            "change_1h": change_1h,
            "change_5m": change_5m,
            "price_usd": _f(best.get("priceUsd")),
            "reason": f"SOL 1h: {change_1h:+.1f}%",
        }
    except Exception as e:
        logger.warning(f"SOL trend check failed: {e}")
        return {"ok": True, "change_1h": 0, "reason": "check failed"}


async def discover_candidates(
    http: httpx.AsyncClient,
    min_liquidity: float,
    min_volume: float,
    max_age_hours: int,
) -> list[dict]:
    """Discover and rank memecoin candidates from multiple DexScreener sources."""
    all_mints: list[str] = []

    profiles = await _fetch_solana_mints_from_endpoint(
        http, f"{DEXSCREENER_BASE}/token-profiles/latest/v1"
    )
    all_mints.extend(profiles)

    boosts = await _fetch_solana_mints_from_endpoint(
        http, f"{DEXSCREENER_BASE}/token-boosts/latest/v1"
    )
    all_mints.extend(boosts)

    trending = await _fetch_trending_solana(http)
    all_mints.extend(trending)

    seen: set[str] = set()
    solana_mints: list[str] = []
    for m in all_mints:
        if m and m not in seen:
            seen.add(m)
            solana_mints.append(m)

    solana_mints = solana_mints[:60]
    if not solana_mints:
        logger.warning("No Solana mints found from any DexScreener source")
        return []

    logger.info(f"Discovery: {len(profiles)} profiles, {len(boosts)} boosts, {len(trending)} trending = {len(solana_mints)} unique mints")

    candidates: list[dict] = []
    now_s = int(time.time())

    for i in range(0, len(solana_mints), 30):
        chunk = solana_mints[i:i + 30]
        try:
            r = await http.get(
                f"{DEXSCREENER_BASE}/latest/dex/tokens/{','.join(chunk)}",
                timeout=20,
            )
            r.raise_for_status()
            batches = r.json().get("pairs", [])
        except Exception as e:
            logger.warning(f"Token enrichment failed: {e}")
            continue

        for pair in batches:
            if pair.get("chainId") != "solana":
                continue

            base = pair.get("baseToken", {})
            mint = base.get("address", "")
            liq = _f((pair.get("liquidity", {}) or {}).get("usd"))
            vol = _f((pair.get("volume", {}) or {}).get("h24"))
            mcap = _f(pair.get("marketCap") or pair.get("fdv"))
            created = pair.get("pairCreatedAt")
            age_hours = (now_s - created / 1000) / 3600 if created else None

            if liq < min_liquidity or vol < min_volume:
                continue
            if age_hours is None or age_hours < 0 or age_hours > max_age_hours:
                continue

            txns = (pair.get("txns", {}) or {}).get("h1", {})
            buys = txns.get("buys", 0) or 0
            sells = txns.get("sells", 0) or 0
            ratio = buys / max(sells, 1)

            price_change_5m = _f((pair.get("priceChange", {}) or {}).get("m5"))
            price_change_1h = _f((pair.get("priceChange", {}) or {}).get("h1"))
            price_change_24h = _f((pair.get("priceChange", {}) or {}).get("h24"))

            vol_to_mcap = vol / mcap if mcap and mcap > 0 else 0
            volume_spike = min(vol_to_mcap * 2, 3.0) if vol_to_mcap > 0.5 else 0

            score = (
                min(vol / 100_000, 5) +
                min(ratio, 3) +
                min(liq / 50_000, 3) +
                max(0, (max_age_hours - age_hours) / max_age_hours) * 2 +
                (1.0 if price_change_1h > 0 else 0) +
                (0.5 if price_change_5m > 0 else 0) +
                (0.5 if buys > 20 else 0) +
                volume_spike +
                (1.5 if price_change_5m > 5 else 0) +
                (1.0 if ratio > 2.0 and buys > 30 else 0)
            )

            candidates.append({
                "mint": mint,
                "symbol": base.get("symbol", "???"),
                "name": base.get("name", "Unknown"),
                "price_usd": _f(pair.get("priceUsd")),
                "liquidity_usd": liq,
                "volume_24h": vol,
                "market_cap": mcap,
                "buys_1h": buys,
                "sells_1h": sells,
                "buy_sell_ratio": round(ratio, 2),
                "age_hours": round(age_hours, 1),
                "price_change_5m": price_change_5m,
                "price_change_1h": price_change_1h,
                "price_change_24h": price_change_24h,
                "dex": pair.get("dexId", "?"),
                "pair_address": pair.get("pairAddress", ""),
                "score": round(score, 2),
            })

    best: dict[str, dict] = {}
    for c in candidates:
        if c["mint"] not in best or c["liquidity_usd"] > best[c["mint"]]["liquidity_usd"]:
            best[c["mint"]] = c
    ranked = sorted(best.values(), key=lambda c: c["score"], reverse=True)
    return ranked[:MEMECOIN_AI_CANDIDATE_LIMIT]
