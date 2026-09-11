"""Token discovery via DexScreener with enhanced filtering."""

import logging
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


async def discover_candidates(
    http: httpx.AsyncClient,
    min_liquidity: float,
    min_volume: float,
    max_age_hours: int,
) -> list[dict]:
    """Discover and rank memecoin candidates from DexScreener."""
    try:
        r = await http.get(f"{DEXSCREENER_BASE}/token-profiles/latest/v1", timeout=20)
        r.raise_for_status()
        profiles = r.json()
    except Exception as e:
        logger.warning(f"Token profile fetch failed: {e}")
        return []

    solana_mints = [
        p.get("tokenAddress")
        for p in profiles
        if p.get("chainId") == "solana" and p.get("tokenAddress")
    ]
    solana_mints = solana_mints[:40]
    if not solana_mints:
        return []

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

            score = (
                min(vol / 100_000, 5) +
                min(ratio, 3) +
                min(liq / 50_000, 3) +
                max(0, (max_age_hours - age_hours) / max_age_hours) * 2 +
                (1.0 if price_change_1h > 0 else 0) +
                (0.5 if price_change_5m > 0 else 0) +
                (0.5 if buys > 20 else 0)
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
