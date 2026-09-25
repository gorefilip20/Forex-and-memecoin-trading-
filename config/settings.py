import os

# ── Solana / Memecoin ──────────────────────────────────────────────
DEXSCREENER_BASE = "https://api.dexscreener.com"
JUPITER_QUOTE_URL = "https://api.jup.ag/swap/v1/quote"
JUPITER_SWAP_URL = "https://api.jup.ag/swap/v1/swap"
SOL_MINT = "So11111111111111111111111111111111111111112"
LAMPORTS_PER_SOL = 1_000_000_000
SOLANA_RPC_PUBLIC = "https://api.mainnet-beta.solana.com"

MEMECOIN_MIN_CONFIDENCE = 45.0
MEMECOIN_MAX_OPEN_POSITIONS = 10
MEMECOIN_MIN_SL_PCT = 1.0
MEMECOIN_MAX_SL_PCT = 50.0
MEMECOIN_MIN_TP_PCT = 2.0
MEMECOIN_MAX_TP_PCT = 500.0
MEMECOIN_MAX_PRICE_IMPACT_PCT = 5.0
MEMECOIN_AI_CANDIDATE_LIMIT = 12
MEMECOIN_COOLDOWN_MINUTES = 90

# Rug detection thresholds
RUG_MAX_TOP_HOLDER_PCT = 30.0
RUG_MIN_HOLDERS = 50
RUG_MAX_DEV_HOLDING_PCT = 10.0

# ── Forex ──────────────────────────────────────────────────────────
FOREX_DEFAULT_TIMEFRAME = "1h"
FOREX_SUPPORTED_TIMEFRAMES = ["5m", "15m", "30m", "1h", "4h", "1d"]
FOREX_MIN_CONFIDENCE = 45.0
FOREX_MAX_OPEN_POSITIONS = 5
FOREX_DEFAULT_RISK_PCT = 1.0
FOREX_MAX_RISK_PCT = 3.0
FOREX_DEFAULT_LEVERAGE = 50
FOREX_MAX_LEVERAGE = 100
FOREX_DEFAULT_LOT_SIZE = 0.01
FOREX_MAX_LOT_SIZE = 1.0
FOREX_MIN_SL_PIPS = 10
FOREX_MAX_SL_PIPS = 200
FOREX_MIN_TP_PIPS = 15
FOREX_MAX_TP_PIPS = 500

# Technical analysis parameters
TA_RSI_PERIOD = 14
TA_RSI_OVERBOUGHT = 70
TA_RSI_OVERSOLD = 30
TA_MACD_FAST = 12
TA_MACD_SLOW = 26
TA_MACD_SIGNAL = 9
TA_EMA_SHORT = 9
TA_EMA_MEDIUM = 21
TA_EMA_LONG = 50
TA_BB_PERIOD = 20
TA_BB_STD = 2.0
TA_ATR_PERIOD = 14
TA_STOCH_K = 14
TA_STOCH_D = 3
TA_ADX_PERIOD = 14

# ── Shared ─────────────────────────────────────────────────────────
TELEGRAM_API = "https://api.telegram.org"
APPROVAL_TTL_SECONDS = 1800
AI_MODEL = "gpt-4o-mini"
AI_ENGINE = os.environ.get("AI_ENGINE", "auto")  # "jev", "openai", or "auto" (jev if key present)

# Data provider URLs (free tier)
TWELVE_DATA_BASE = "https://api.twelvedata.com"
ALPHA_VANTAGE_BASE = "https://www.alphavantage.co/query"


def get_solana_rpc_url() -> str:
    raw = (os.environ.get("HELIUS_RPC_URL") or "").strip()
    if not raw:
        return SOLANA_RPC_PUBLIC
    if raw.startswith("http://") or raw.startswith("https://"):
        return raw
    return f"https://mainnet.helius-rpc.com/?api-key={raw}"
