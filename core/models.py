from typing import Optional
from pydantic import BaseModel, Field
from config.settings import (
    MEMECOIN_MAX_OPEN_POSITIONS, FOREX_MAX_OPEN_POSITIONS,
    FOREX_MAX_LEVERAGE, FOREX_MAX_LOT_SIZE,
)


# ── Memecoin Models ────────────────────────────────────────────────

class MemecoinBotRequest(BaseModel):
    paper_trading: bool = Field(True, description="True = simulate, False = live on-chain trades")
    approve_first: bool = Field(False, description="Require Telegram approval before each trade")
    max_positions: int = Field(3, ge=1, le=MEMECOIN_MAX_OPEN_POSITIONS)
    per_trade_usd: float = Field(2.0, gt=0, le=10000.0)
    paper_starting_usd: float = Field(1000.0, gt=0)
    min_liquidity_usd: float = Field(30000.0, ge=0)
    min_volume_24h: float = Field(50000.0, ge=0)
    max_age_hours: int = Field(120, ge=1, le=336)
    slippage_bps: int = Field(700, ge=50, le=2000)
    schedule_interval_minutes: int = Field(20, ge=1, le=1440)
    register_schedule: bool = Field(True)
    enhanced_rug_check: bool = Field(True, description="Use enhanced rug detection (holder analysis + liquidity lock check)")


class MemecoinCycleResponse(BaseModel):
    cycle_time: str
    paper_trading: bool
    approve_first: bool
    candidates_found: int
    approvals_processed: int
    alerts_sent: int
    decisions: list[dict]
    entries: list[dict]
    exits: list[dict]
    pending_approvals: int
    balance_usd: float
    open_positions: int
    rugs_blocked: int = 0
    message: str


# ── Forex Models ───────────────────────────────────────────────────

class ForexBotRequest(BaseModel):
    paper_trading: bool = Field(True, description="True = simulate, False = live broker execution")
    approve_first: bool = Field(False, description="Require Telegram approval before each trade")
    pairs: list[str] = Field(
        default=["EUR/USD", "GBP/USD", "USD/JPY", "XAU/USD", "BTC/USD"],
        description="Forex pairs to monitor"
    )
    timeframe: str = Field("1h", description="Analysis timeframe: 5m, 15m, 30m, 1h, 4h, 1d")
    max_positions: int = Field(3, ge=1, le=FOREX_MAX_OPEN_POSITIONS)
    risk_per_trade_pct: float = Field(1.0, gt=0, le=3.0, description="Risk per trade as % of account balance")
    leverage: int = Field(50, ge=1, le=FOREX_MAX_LEVERAGE)
    lot_size: float = Field(0.01, gt=0, le=FOREX_MAX_LOT_SIZE)
    paper_starting_usd: float = Field(10000.0, gt=0)
    schedule_interval_minutes: int = Field(60, ge=1, le=1440)
    register_schedule: bool = Field(True)
    multi_timeframe: bool = Field(True, description="Use multi-timeframe analysis for stronger signals")
    signal_only: bool = Field(False, description="Send qualified signals without opening paper or live positions")


class ForexSignal(BaseModel):
    pair: str
    direction: str  # "BUY" or "SELL"
    entry_price: float
    stop_loss: float
    take_profit: float
    sl_pips: float
    tp_pips: float
    risk_reward: float
    confidence: float
    timeframe: str
    reasoning: str
    indicators: dict = {}


class ForexCycleResponse(BaseModel):
    cycle_time: str
    paper_trading: bool
    approve_first: bool
    pairs_analyzed: int
    signals_generated: int
    trades_executed: int
    trades_closed: int
    alerts_sent: int
    signals: list[dict]
    open_positions: int
    balance_usd: float
    equity_usd: float
    message: str


# ── Shared Models ──────────────────────────────────────────────────

class StatusResponse(BaseModel):
    memecoin_balance_usd: float = 0.0
    memecoin_positions: list[dict] = []
    memecoin_pending: list[dict] = []
    forex_balance_usd: float = 0.0
    forex_positions: list[dict] = []
    forex_equity_usd: float = 0.0
    trade_log: list[dict] = []


class ResetResponse(BaseModel):
    reset: bool
    memecoin_balance_usd: float
    forex_balance_usd: float


class DashboardResponse(BaseModel):
    memecoin: dict = {}
    forex: dict = {}
    combined_pnl_usd: float = 0.0
    total_trades: int = 0
    win_rate: float = 0.0
