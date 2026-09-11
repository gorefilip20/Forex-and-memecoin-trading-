# Forex & Memecoin Trading Bot

Dual-market autonomous trading platform that handles both **forex** and **Solana memecoin** trading from a single system.

## What It Does

### Forex Trading
- Fetches live price data from TwelveData / Alpha Vantage
- Runs full technical analysis: RSI, MACD, EMA crossovers, Bollinger Bands, ATR, Stochastic, ADX, Support/Resistance levels
- Multi-timeframe confirmation (e.g. 1h signals confirmed by daily trend)
- AI reviews and confirms/rejects signals before execution
- Paper trading engine built in, live execution via OANDA API
- Sends detailed signals to Telegram with entry/SL/TP/R:R

### Memecoin Trading
- Discovers trending Solana tokens via DexScreener
- Enhanced rug-pull detection: mint/freeze authority checks + holder distribution analysis + supply concentration + liquidity pool verification
- AI selects strongest candidates based on volume, momentum, buy/sell ratio
- Executes swaps via Jupiter aggregator on Solana
- Manages take-profit / stop-loss exits per position
- Cooldown system prevents re-buying recently sold tokens

### Shared Infrastructure
- Telegram bot for alerts, signals, and trade approval flow (approve/skip inline buttons)
- Redis state management for positions, balances, trade logs
- Unified dashboard with combined P&L and win rate
- Paper trading mode for both markets (no real money needed to test)

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| POST | `/memecoin` | Run memecoin discovery + trading cycle |
| POST | `/forex` | Run forex analysis + trading cycle |
| POST | `/forex/analyze` | Generate forex signals (no execution) |
| POST | `/forex/analyze/{pair}` | Deep analysis of a single pair |
| GET | `/status` | Full status of both markets |
| GET | `/dashboard` | Combined dashboard with stats |
| POST | `/reset` | Reset all state to defaults |
| POST | `/` | Legacy endpoint (runs memecoin cycle) |

## Setup

1. Copy `.env.example` to `.env` and fill in your keys
2. Install dependencies: `pip install -r requirements.txt`
3. Start Redis: `redis-server`
4. Run: `python main.py`

The API will be available at `http://localhost:8000`. Docs at `/docs`.

## Required API Keys

| Key | Purpose | Where to Get |
|-----|---------|--------------|
| `OPENAI_API_KEY` | AI trading decisions | platform.openai.com |
| `TELEGRAM_BOT_TOKEN` | Alerts & approvals | @BotFather on Telegram |
| `TELEGRAM_CHAT_ID` | Your chat ID | @userinfobot on Telegram |
| `TWELVE_DATA_API_KEY` | Forex price data | twelvedata.com (free tier: 800/day) |

### Optional (for live trading)

| Key | Purpose |
|-----|---------|
| `SOLANA_PRIVATE_KEY` | Memecoin live trades (base58 key or seed phrase) |
| `SOLANA_WALLET_ADDRESS` | Your Solana wallet public address |
| `HELIUS_RPC_URL` | Better Solana RPC (recommended over public node) |
| `OANDA_API_KEY` | Live forex execution |
| `OANDA_ACCOUNT_ID` | OANDA trading account |

## Project Structure

```
├── main.py              # FastAPI app, all endpoints
├── config/
│   ├── settings.py      # All constants and configuration
│   └── pairs.py         # Forex pairs and crypto watchlist
├── core/
│   ├── models.py        # Pydantic request/response models
│   ├── state.py         # Redis state management
│   └── telegram.py      # Telegram notifications
├── forex/
│   ├── data.py          # Price data fetching (TwelveData/AlphaVantage)
│   ├── analysis.py      # Technical analysis engine (pure Python)
│   ├── signals.py       # Signal generation with multi-TF
│   └── executor.py      # Paper + OANDA live execution
├── memecoin/
│   ├── discovery.py     # DexScreener token discovery
│   ├── safety.py        # Enhanced rug-pull detection
│   └── executor.py      # Paper + Jupiter live execution
├── ai/
│   └── analyst.py       # AI decision engine for both markets
├── requirements.txt
└── .env.example
```

## Risk Warning

This bot does not guarantee profit. Forex and memecoins are both high-risk markets. The bot includes paper trading mode so you can test strategies without real money. Only trade with funds you can afford to lose. Stop-loss checks only run each cycle, so fast moves can fill well past the stop.
