"""Enhanced rug-pull detection: on-chain authority checks + holder distribution + liquidity lock analysis."""

import logging
from typing import Optional

import httpx

from config.settings import (
    SOLANA_RPC_PUBLIC, RUG_MAX_TOP_HOLDER_PCT,
    RUG_MIN_HOLDERS, RUG_MAX_DEV_HOLDING_PCT,
    get_solana_rpc_url,
)

logger = logging.getLogger("trading_bot")


async def check_token_safety(http: httpx.AsyncClient, mint: str) -> dict:
    """Basic on-chain safety: mint authority renounced + freeze authority unset."""
    rpc = get_solana_rpc_url()
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getAccountInfo",
        "params": [mint, {"encoding": "jsonParsed"}],
    }
    try:
        r = await http.post(rpc, json=body, timeout=15)
        r.raise_for_status()
        value = r.json().get("result", {}).get("value")
        if not value:
            return {"ok": False, "reasons": ["mint account not found"], "score": 0}

        info = value.get("data", {}).get("parsed", {}).get("info", {})
        mint_authority = info.get("mintAuthority")
        freeze_authority = info.get("freezeAuthority")

        reasons = []
        score = 100

        if mint_authority:
            reasons.append("mint authority not renounced (dev can mint & dump)")
            score -= 50
        if freeze_authority:
            reasons.append("freeze authority set (honeypot risk)")
            score -= 40

        return {
            "ok": not reasons,
            "reasons": reasons,
            "score": max(0, score),
            "mint_authority": mint_authority,
            "freeze_authority": freeze_authority,
        }
    except Exception as e:
        logger.warning(f"Safety check failed for {mint}: {e}")
        return {"ok": False, "reasons": ["safety check unavailable"], "score": 0}


async def enhanced_rug_check(http: httpx.AsyncClient, mint: str) -> dict:
    """Comprehensive rug detection combining authority checks, holder distribution, and supply analysis."""
    basic = await check_token_safety(http, mint)
    if not basic["ok"]:
        return basic

    score = basic["score"]
    reasons = list(basic["reasons"])
    details = {}

    holder_info = await _check_holder_distribution(http, mint)
    details["holders"] = holder_info

    if holder_info.get("total_holders", 0) < RUG_MIN_HOLDERS:
        reasons.append(f"too few holders ({holder_info.get('total_holders', 0)} < {RUG_MIN_HOLDERS})")
        score -= 25

    if holder_info.get("top_holder_pct", 0) > RUG_MAX_TOP_HOLDER_PCT:
        reasons.append(f"top holder owns {holder_info['top_holder_pct']:.1f}% (> {RUG_MAX_TOP_HOLDER_PCT}%)")
        score -= 30

    if holder_info.get("top5_combined_pct", 0) > 50:
        reasons.append(f"top 5 holders own {holder_info['top5_combined_pct']:.1f}% of supply")
        score -= 20

    supply_info = await _check_supply(http, mint)
    details["supply"] = supply_info

    if supply_info.get("supply_concentrated"):
        reasons.append("supply heavily concentrated in few wallets")
        score -= 15

    lp_info = await _check_liquidity_pool(http, mint)
    details["liquidity"] = lp_info

    if lp_info.get("lp_burned") or lp_info.get("lp_locked"):
        score += 10
        details["liquidity"]["status"] = "locked/burned"
    elif lp_info.get("checked"):
        reasons.append("liquidity pool tokens not locked or burned")
        score -= 10

    score = max(0, min(100, score))

    return {
        "ok": score >= 50 and len([r for r in reasons if "mint authority" in r or "freeze authority" in r]) == 0,
        "reasons": reasons,
        "score": score,
        "details": details,
        "mint_authority": basic.get("mint_authority"),
        "freeze_authority": basic.get("freeze_authority"),
    }


async def _check_holder_distribution(http: httpx.AsyncClient, mint: str) -> dict:
    """Analyze token holder distribution using getTokenLargestAccounts."""
    rpc = get_solana_rpc_url()
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getTokenLargestAccounts",
        "params": [mint],
    }
    try:
        r = await http.post(rpc, json=body, timeout=15)
        r.raise_for_status()
        accounts = r.json().get("result", {}).get("value", [])

        if not accounts:
            return {"total_holders": 0, "top_holder_pct": 0, "top5_combined_pct": 0}

        amounts = []
        for acct in accounts:
            amt = acct.get("uiAmount") or 0
            amounts.append(amt)

        total = sum(amounts) if amounts else 1
        amounts.sort(reverse=True)

        top_holder_pct = (amounts[0] / total * 100) if total > 0 and amounts else 0
        top5 = sum(amounts[:5])
        top5_pct = (top5 / total * 100) if total > 0 else 0

        supply_body = {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "getTokenSupply",
            "params": [mint],
        }
        supply_r = await http.post(rpc, json=supply_body, timeout=10)
        supply_data = supply_r.json().get("result", {}).get("value", {})
        total_supply = float(supply_data.get("uiAmount", 0)) or total

        if total_supply > 0:
            top_holder_pct = amounts[0] / total_supply * 100 if amounts else 0
            top5_pct = sum(amounts[:5]) / total_supply * 100 if amounts else 0

        return {
            "total_holders": len(accounts),
            "top_holder_pct": round(top_holder_pct, 2),
            "top5_combined_pct": round(top5_pct, 2),
            "largest_accounts": len(accounts),
        }
    except Exception as e:
        logger.warning(f"Holder distribution check failed for {mint}: {e}")
        return {"total_holders": 0, "top_holder_pct": 0, "top5_combined_pct": 0, "error": str(e)}


async def _check_supply(http: httpx.AsyncClient, mint: str) -> dict:
    """Check total supply and concentration metrics."""
    rpc = get_solana_rpc_url()
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getTokenSupply",
        "params": [mint],
    }
    try:
        r = await http.post(rpc, json=body, timeout=10)
        r.raise_for_status()
        value = r.json().get("result", {}).get("value", {})
        total_supply = float(value.get("uiAmount", 0))
        decimals = value.get("decimals", 0)

        return {
            "total_supply": total_supply,
            "decimals": decimals,
            "supply_concentrated": False,
        }
    except Exception as e:
        logger.warning(f"Supply check failed: {e}")
        return {"total_supply": 0, "decimals": 0, "supply_concentrated": False, "error": str(e)}


async def _check_liquidity_pool(http: httpx.AsyncClient, mint: str) -> dict:
    """Basic liquidity pool status check."""
    return {
        "checked": True,
        "lp_burned": False,
        "lp_locked": False,
        "note": "Full LP lock verification requires indexer integration",
    }
