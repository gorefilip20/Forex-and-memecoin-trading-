"""Technical analysis engine: RSI, MACD, EMA, Bollinger Bands, ATR, Stochastic, ADX, S/R levels."""

import math
from typing import Optional
from config.settings import (
    TA_RSI_PERIOD, TA_RSI_OVERBOUGHT, TA_RSI_OVERSOLD,
    TA_MACD_FAST, TA_MACD_SLOW, TA_MACD_SIGNAL,
    TA_EMA_SHORT, TA_EMA_MEDIUM, TA_EMA_LONG,
    TA_BB_PERIOD, TA_BB_STD,
    TA_ATR_PERIOD, TA_STOCH_K, TA_STOCH_D,
    TA_ADX_PERIOD,
)


class TechnicalAnalyzer:
    """Pure-Python technical analysis without external TA libraries."""

    def __init__(self, candles: list[dict]):
        self.candles = candles
        self.closes = [c["close"] for c in candles]
        self.highs = [c["high"] for c in candles]
        self.lows = [c["low"] for c in candles]
        self.opens = [c["open"] for c in candles]
        self.volumes = [c.get("volume", 0) for c in candles]

    def full_analysis(self) -> dict:
        if len(self.closes) < 50:
            return {"error": "insufficient data", "candle_count": len(self.closes)}

        rsi = self.rsi()
        macd_line, signal_line, histogram = self.macd()
        ema_short = self.ema(TA_EMA_SHORT)
        ema_medium = self.ema(TA_EMA_MEDIUM)
        ema_long = self.ema(TA_EMA_LONG)
        bb_upper, bb_middle, bb_lower = self.bollinger_bands()
        atr = self.atr()
        stoch_k, stoch_d = self.stochastic()
        adx_val = self.adx()
        support, resistance = self.support_resistance()

        current = self.closes[-1]
        trend = self._determine_trend(ema_short, ema_medium, ema_long)
        momentum = self._assess_momentum(rsi, macd_line, signal_line, histogram)
        volatility = self._assess_volatility(atr, bb_upper, bb_lower, current)
        strength = "strong" if adx_val and adx_val > 25 else "weak" if adx_val and adx_val < 20 else "moderate"

        signals = self._generate_signals(
            rsi, macd_line, signal_line, histogram,
            ema_short, ema_medium, ema_long,
            bb_upper, bb_lower, stoch_k, stoch_d,
            current, trend, adx_val,
        )

        return {
            "current_price": current,
            "rsi": round(rsi, 2) if rsi else None,
            "macd_line": round(macd_line, 6) if macd_line else None,
            "macd_signal": round(signal_line, 6) if signal_line else None,
            "macd_histogram": round(histogram, 6) if histogram else None,
            "ema_short": round(ema_short, 5) if ema_short else None,
            "ema_medium": round(ema_medium, 5) if ema_medium else None,
            "ema_long": round(ema_long, 5) if ema_long else None,
            "bb_upper": round(bb_upper, 5) if bb_upper else None,
            "bb_middle": round(bb_middle, 5) if bb_middle else None,
            "bb_lower": round(bb_lower, 5) if bb_lower else None,
            "atr": round(atr, 5) if atr else None,
            "stoch_k": round(stoch_k, 2) if stoch_k else None,
            "stoch_d": round(stoch_d, 2) if stoch_d else None,
            "adx": round(adx_val, 2) if adx_val else None,
            "support": round(support, 5) if support else None,
            "resistance": round(resistance, 5) if resistance else None,
            "trend": trend,
            "momentum": momentum,
            "volatility": volatility,
            "trend_strength": strength,
            "signals": signals,
        }

    # ── Indicator Calculations ─────────────────────────────────────

    def rsi(self, period: int = TA_RSI_PERIOD) -> Optional[float]:
        if len(self.closes) < period + 1:
            return None
        deltas = [self.closes[i] - self.closes[i - 1] for i in range(1, len(self.closes))]
        gains = [d if d > 0 else 0 for d in deltas]
        losses = [-d if d < 0 else 0 for d in deltas]

        avg_gain = sum(gains[:period]) / period
        avg_loss = sum(losses[:period]) / period

        for i in range(period, len(gains)):
            avg_gain = (avg_gain * (period - 1) + gains[i]) / period
            avg_loss = (avg_loss * (period - 1) + losses[i]) / period

        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return 100 - (100 / (1 + rs))

    def macd(self) -> tuple[Optional[float], Optional[float], Optional[float]]:
        if len(self.closes) < TA_MACD_SLOW + TA_MACD_SIGNAL:
            return None, None, None
        ema_fast_values = self._ema_series(self.closes, TA_MACD_FAST)
        ema_slow_values = self._ema_series(self.closes, TA_MACD_SLOW)

        start = TA_MACD_SLOW - 1
        macd_line_values = [
            ema_fast_values[i] - ema_slow_values[i]
            for i in range(start, len(ema_slow_values))
            if i < len(ema_fast_values)
        ]

        if len(macd_line_values) < TA_MACD_SIGNAL:
            return None, None, None

        signal_values = self._ema_series(macd_line_values, TA_MACD_SIGNAL)
        macd_val = macd_line_values[-1]
        signal_val = signal_values[-1]
        hist = macd_val - signal_val
        return macd_val, signal_val, hist

    def ema(self, period: int) -> Optional[float]:
        if len(self.closes) < period:
            return None
        values = self._ema_series(self.closes, period)
        return values[-1] if values else None

    def bollinger_bands(self) -> tuple[Optional[float], Optional[float], Optional[float]]:
        if len(self.closes) < TA_BB_PERIOD:
            return None, None, None
        window = self.closes[-TA_BB_PERIOD:]
        middle = sum(window) / len(window)
        std = math.sqrt(sum((x - middle) ** 2 for x in window) / len(window))
        upper = middle + TA_BB_STD * std
        lower = middle - TA_BB_STD * std
        return upper, middle, lower

    def atr(self, period: int = TA_ATR_PERIOD) -> Optional[float]:
        if len(self.closes) < period + 1:
            return None
        true_ranges = []
        for i in range(1, len(self.closes)):
            tr = max(
                self.highs[i] - self.lows[i],
                abs(self.highs[i] - self.closes[i - 1]),
                abs(self.lows[i] - self.closes[i - 1]),
            )
            true_ranges.append(tr)

        if len(true_ranges) < period:
            return None

        atr_val = sum(true_ranges[:period]) / period
        for i in range(period, len(true_ranges)):
            atr_val = (atr_val * (period - 1) + true_ranges[i]) / period
        return atr_val

    def stochastic(self) -> tuple[Optional[float], Optional[float]]:
        if len(self.closes) < TA_STOCH_K:
            return None, None

        k_values = []
        for i in range(TA_STOCH_K - 1, len(self.closes)):
            window_highs = self.highs[i - TA_STOCH_K + 1:i + 1]
            window_lows = self.lows[i - TA_STOCH_K + 1:i + 1]
            highest = max(window_highs)
            lowest = min(window_lows)
            if highest == lowest:
                k_values.append(50.0)
            else:
                k_values.append((self.closes[i] - lowest) / (highest - lowest) * 100)

        if len(k_values) < TA_STOCH_D:
            return k_values[-1] if k_values else None, None

        d_values = []
        for i in range(TA_STOCH_D - 1, len(k_values)):
            d_values.append(sum(k_values[i - TA_STOCH_D + 1:i + 1]) / TA_STOCH_D)

        return k_values[-1], d_values[-1]

    def adx(self, period: int = TA_ADX_PERIOD) -> Optional[float]:
        if len(self.closes) < period * 2:
            return None

        plus_dm = []
        minus_dm = []
        true_ranges = []

        for i in range(1, len(self.closes)):
            high_diff = self.highs[i] - self.highs[i - 1]
            low_diff = self.lows[i - 1] - self.lows[i]

            pdm = high_diff if high_diff > low_diff and high_diff > 0 else 0
            mdm = low_diff if low_diff > high_diff and low_diff > 0 else 0
            plus_dm.append(pdm)
            minus_dm.append(mdm)

            tr = max(
                self.highs[i] - self.lows[i],
                abs(self.highs[i] - self.closes[i - 1]),
                abs(self.lows[i] - self.closes[i - 1]),
            )
            true_ranges.append(tr)

        if len(true_ranges) < period:
            return None

        smoothed_tr = sum(true_ranges[:period])
        smoothed_pdm = sum(plus_dm[:period])
        smoothed_mdm = sum(minus_dm[:period])

        dx_values = []
        for i in range(period, len(true_ranges)):
            smoothed_tr = smoothed_tr - smoothed_tr / period + true_ranges[i]
            smoothed_pdm = smoothed_pdm - smoothed_pdm / period + plus_dm[i]
            smoothed_mdm = smoothed_mdm - smoothed_mdm / period + minus_dm[i]

            if smoothed_tr == 0:
                continue
            plus_di = 100 * smoothed_pdm / smoothed_tr
            minus_di = 100 * smoothed_mdm / smoothed_tr

            di_sum = plus_di + minus_di
            if di_sum == 0:
                dx_values.append(0)
            else:
                dx_values.append(100 * abs(plus_di - minus_di) / di_sum)

        if len(dx_values) < period:
            return None

        adx_val = sum(dx_values[:period]) / period
        for i in range(period, len(dx_values)):
            adx_val = (adx_val * (period - 1) + dx_values[i]) / period

        return adx_val

    def support_resistance(self, lookback: int = 30) -> tuple[Optional[float], Optional[float]]:
        if len(self.closes) < lookback:
            return None, None

        recent_lows = self.lows[-lookback:]
        recent_highs = self.highs[-lookback:]
        current = self.closes[-1]

        supports = sorted([l for l in recent_lows if l < current])
        resistances = sorted([h for h in recent_highs if h > current])

        support = supports[-1] if supports else min(recent_lows)
        resistance = resistances[0] if resistances else max(recent_highs)

        return support, resistance

    # ── Private helpers ────────────────────────────────────────────

    def _ema_series(self, data: list[float], period: int) -> list[float]:
        if len(data) < period:
            return []
        multiplier = 2 / (period + 1)
        ema_values = [sum(data[:period]) / period]
        for i in range(period, len(data)):
            ema_values.append((data[i] - ema_values[-1]) * multiplier + ema_values[-1])
        return ema_values

    def _determine_trend(self, ema_s, ema_m, ema_l) -> str:
        if not all([ema_s, ema_m, ema_l]):
            return "unknown"
        if ema_s > ema_m > ema_l:
            return "bullish"
        if ema_s < ema_m < ema_l:
            return "bearish"
        return "ranging"

    def _assess_momentum(self, rsi, macd_line, signal_line, histogram) -> str:
        if rsi is None:
            return "unknown"
        if rsi > TA_RSI_OVERBOUGHT:
            return "overbought"
        if rsi < TA_RSI_OVERSOLD:
            return "oversold"
        if histogram and histogram > 0 and rsi > 50:
            return "bullish"
        if histogram and histogram < 0 and rsi < 50:
            return "bearish"
        return "neutral"

    def _assess_volatility(self, atr, bb_upper, bb_lower, current) -> str:
        if not all([atr, bb_upper, bb_lower]):
            return "unknown"
        bb_width = (bb_upper - bb_lower) / current * 100 if current else 0
        if bb_width > 3:
            return "high"
        if bb_width < 1:
            return "low"
        return "moderate"

    def _generate_signals(
        self, rsi, macd_line, signal_line, histogram,
        ema_s, ema_m, ema_l, bb_upper, bb_lower,
        stoch_k, stoch_d, current, trend, adx_val,
    ) -> list[dict]:
        signals = []

        if rsi is not None:
            if rsi < TA_RSI_OVERSOLD:
                signals.append({"type": "RSI_OVERSOLD", "direction": "BUY", "strength": 0.7})
            elif rsi > TA_RSI_OVERBOUGHT:
                signals.append({"type": "RSI_OVERBOUGHT", "direction": "SELL", "strength": 0.7})

        if histogram is not None and macd_line is not None and signal_line is not None:
            if macd_line > signal_line and histogram > 0:
                signals.append({"type": "MACD_BULLISH", "direction": "BUY", "strength": 0.6})
            elif macd_line < signal_line and histogram < 0:
                signals.append({"type": "MACD_BEARISH", "direction": "SELL", "strength": 0.6})

        if all([ema_s, ema_m, ema_l]):
            if trend == "bullish":
                signals.append({"type": "EMA_BULLISH_ALIGN", "direction": "BUY", "strength": 0.8})
            elif trend == "bearish":
                signals.append({"type": "EMA_BEARISH_ALIGN", "direction": "SELL", "strength": 0.8})

        if bb_lower is not None and current <= bb_lower:
            signals.append({"type": "BB_LOWER_TOUCH", "direction": "BUY", "strength": 0.5})
        elif bb_upper is not None and current >= bb_upper:
            signals.append({"type": "BB_UPPER_TOUCH", "direction": "SELL", "strength": 0.5})

        if stoch_k is not None and stoch_d is not None:
            if stoch_k < 20 and stoch_d < 20 and stoch_k > stoch_d:
                signals.append({"type": "STOCH_OVERSOLD_CROSS", "direction": "BUY", "strength": 0.65})
            elif stoch_k > 80 and stoch_d > 80 and stoch_k < stoch_d:
                signals.append({"type": "STOCH_OVERBOUGHT_CROSS", "direction": "SELL", "strength": 0.65})

        if adx_val and adx_val > 25:
            for s in signals:
                s["strength"] = min(s["strength"] + 0.1, 1.0)

        return signals
