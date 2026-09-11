FOREX_PAIRS = {
    "majors": [
        {"symbol": "EUR/USD", "pip_value": 0.0001, "spread_avg": 1.0, "description": "Euro / US Dollar"},
        {"symbol": "GBP/USD", "pip_value": 0.0001, "spread_avg": 1.2, "description": "British Pound / US Dollar"},
        {"symbol": "USD/JPY", "pip_value": 0.01, "spread_avg": 1.0, "description": "US Dollar / Japanese Yen"},
        {"symbol": "USD/CHF", "pip_value": 0.0001, "spread_avg": 1.3, "description": "US Dollar / Swiss Franc"},
        {"symbol": "AUD/USD", "pip_value": 0.0001, "spread_avg": 1.2, "description": "Australian Dollar / US Dollar"},
        {"symbol": "USD/CAD", "pip_value": 0.0001, "spread_avg": 1.5, "description": "US Dollar / Canadian Dollar"},
        {"symbol": "NZD/USD", "pip_value": 0.0001, "spread_avg": 1.5, "description": "New Zealand Dollar / US Dollar"},
    ],
    "minors": [
        {"symbol": "EUR/GBP", "pip_value": 0.0001, "spread_avg": 1.5, "description": "Euro / British Pound"},
        {"symbol": "EUR/JPY", "pip_value": 0.01, "spread_avg": 1.5, "description": "Euro / Japanese Yen"},
        {"symbol": "GBP/JPY", "pip_value": 0.01, "spread_avg": 2.0, "description": "British Pound / Japanese Yen"},
        {"symbol": "EUR/AUD", "pip_value": 0.0001, "spread_avg": 2.0, "description": "Euro / Australian Dollar"},
        {"symbol": "EUR/CHF", "pip_value": 0.0001, "spread_avg": 1.8, "description": "Euro / Swiss Franc"},
        {"symbol": "AUD/JPY", "pip_value": 0.01, "spread_avg": 1.8, "description": "Australian Dollar / Japanese Yen"},
    ],
    "exotics": [
        {"symbol": "USD/ZAR", "pip_value": 0.0001, "spread_avg": 8.0, "description": "US Dollar / South African Rand"},
        {"symbol": "USD/MXN", "pip_value": 0.0001, "spread_avg": 5.0, "description": "US Dollar / Mexican Peso"},
        {"symbol": "USD/SGD", "pip_value": 0.0001, "spread_avg": 3.0, "description": "US Dollar / Singapore Dollar"},
    ],
    "metals": [
        {"symbol": "XAU/USD", "pip_value": 0.01, "spread_avg": 3.0, "description": "Gold / US Dollar"},
        {"symbol": "XAG/USD", "pip_value": 0.001, "spread_avg": 2.5, "description": "Silver / US Dollar"},
    ],
    "crypto": [
        {"symbol": "BTC/USD", "pip_value": 0.01, "spread_avg": 5.0, "description": "Bitcoin / US Dollar"},
        {"symbol": "ETH/USD", "pip_value": 0.01, "spread_avg": 3.0, "description": "Ethereum / US Dollar"},
    ],
}

CRYPTO_WATCHLIST = [
    {"symbol": "SOL", "mint": "So11111111111111111111111111111111111111112", "description": "Solana"},
    {"symbol": "BONK", "mint": "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263", "description": "Bonk"},
    {"symbol": "WIF", "mint": "EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm", "description": "dogwifhat"},
    {"symbol": "JUP", "mint": "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN", "description": "Jupiter"},
]


def get_all_forex_symbols() -> list[str]:
    symbols = []
    for category in FOREX_PAIRS.values():
        symbols.extend(p["symbol"] for p in category)
    return symbols


def get_pip_value(symbol: str) -> float:
    for category in FOREX_PAIRS.values():
        for pair in category:
            if pair["symbol"] == symbol:
                return pair["pip_value"]
    return 0.0001
