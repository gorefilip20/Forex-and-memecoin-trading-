"""Forex signal generator: combines technical analysis with AI to produce actionable trade signals."""

import logging
from typing import Optional

import httpx

from config.settings import (
    FOREX_MIN_CONFIDENCE, FOREX_MIN_SL_PIPS, FOREX_MAX_SL_PIPS,
    FOREX_MIN_TP_PIPS, FOREX_MAX_TP_PIPS,
)
from config.pairs import get_pip_value
from forex.data import fetch_candles, fetch_current_price
from forex.analysis import TechnicalAnalyzer

logger = logging.getLogger("trading_bot")


class ForexSignalGenerator:

    def __init__(self, http: httpx.AsyncClient):
        self.http = http

    async def analyze_pair(self, symbol: str, timeframe: str = "1h", multi_tf: bool = True) -> Optional[dict]:
        """Run full analysis on a single pair and generate a signal if conditions align."""
        candles = await fetch_candles(self.http, symbol, timeframe, 200)
        if len(candles) < 50:
            logger.warning(f"Insufficient candle data for {symbol} ({len(candles)} candles)")
            return None

        analyzer = TechnicalAnalyzer(candles)
        analysis = analyzer.full_analysis()

        if "error" in analysis:
            return None

        mtf_confirmation = None
        if multi_tf:
            mtf_confirmation = await self._multi_timeframe_check(symbol, timeframe)

        signal = self._evaluate_signal(symbol, timeframe, analysis, mtf_confirmation)
        return signal

    async def scan_pairs(self, pairs: list[str], timeframe: str = "1h", multi_tf: bool = True) -> list[dict]:
        """Scan multiple pairs and return signals sorted by confidence."""
        signals = []
        for pair in pairs:
            try:
                signal = await self.analyze_pair(pair, timeframe, multi_tf)
                if signal and signal["confidence"] >= 40:
                    signals.append(signal)
            except Exception as e:
                logger.warning(f"Failed to analyze {pair}: {e}")
                continue

        signals.sort(key=lambda s: s["confidence"], reverse=True)
        return signals

    async def _multi_timeframe_check(self, symbol: str, base_tf: str) -> Optional[dict]:
        """Check higher timeframe for trend confirmation with rich scoring data."""
        higher_tf_map = {
            "5m": "1h", "15m": "4h", "30m": "4h",
            "1h": "4h", "4h": "1d",
        }
        top_tf_map = {
            "5m": "4h", "15m": "1d", "30m": "1d",
            "1h": "1d",
        }
        higher_tf = higher_tf_map.get(base_tf)
        if not higher_tf:
            return None

        candles = await fetch_candles(self.http, symbol, higher_tf, 100)
        if len(candles) < 50:
            return None

        analyzer = TechnicalAnalyzer(candles)
        analysis = analyzer.full_analysis()

        mtf = {
            "timeframe": higher_tf,
            "trend": analysis.get("trend", "unknown"),
            "trend_strength": analysis.get("trend_strength", "unknown"),
            "momentum": analysis.get("momentum", "unknown"),
            "rsi": analysis.get("rsi"),
            "macd_histogram": analysis.get("macd_histogram"),
            "atr": analysis.get("atr"),
            "support": analysis.get("support"),
            "resistance": analysis.get("resistance"),
        }

        top_tf = top_tf_map.get(base_tf)
        if top_tf and top_tf != higher_tf:
            top_candles = await fetch_candles(self.http, symbol, top_tf, 100)
            if len(top_candles) >= 50:
                top_analyzer = TechnicalAnalyzer(top_candles)
                top_analysis = top_analyzer.full_analysis()
                mtf["top_timeframe"] = top_tf
                mtf["top_trend"] = top_analysis.get("trend", "unknown")
                mtf["top_trend_strength"] = top_analysis.get("trend_strength", "unknown")
                mtf["top_rsi"] = top_analysis.get("rsi")
                mtf["top_momentum"] = top_analysis.get("momentum", "unknown")

        return mtf

    def _evaluate_signal(
        self,
        symbol: str,
        timeframe: str,
        analysis: dict,
        mtf: Optional[dict],
    ) -> Optional[dict]:
        """Evaluate technical signals and produce a trade recommendation."""
        signals = analysis.get("signals", [])
        if not signals:
            return None

        buy_signals = [s for s in signals if s["direction"] == "BUY"]
        sell_signals = [s for s in signals if s["direction"] == "SELL"]
        buy_score = sum(s["strength"] for s in buy_signals)
        sell_score = sum(s["strength"] for s in sell_signals)

        if buy_score < 1.0 and sell_score < 1.0:
            return None

        direction = "BUY" if buy_score > sell_score else "SELL"
        raw_confidence = max(buy_score, sell_score)
        dominant = buy_signals if direction == "BUY" else sell_signals

        if mtf:
            mtf_trend = mtf.get("trend", "unknown")
            mtf_strength = mtf.get("trend_strength", "unknown")
            mtf_momentum = mtf.get("momentum", "unknown")

            aligned = (direction == "BUY" and mtf_trend == "bullish") or \
                      (direction == "SELL" and mtf_trend == "bearish")
            opposed = (direction == "BUY" and mtf_trend == "bearish") or \
                      (direction == "SELL" and mtf_trend == "bullish")

            if aligned:
                raw_confidence += 0.4
                if mtf_strength == "strong":
                    raw_confidence += 0.15
                if mtf_momentum == mtf_trend:
                    raw_confidence += 0.1
            elif opposed:
                raw_confidence -= 0.5
                if mtf_strength == "strong":
                    raw_confidence -= 0.15

            top_trend = mtf.get("top_trend")
            if top_trend:
                top_aligned = (direction == "BUY" and top_trend == "bullish") or \
                              (direction == "SELL" and top_trend == "bearish")
                top_opposed = (direction == "BUY" and top_trend == "bearish") or \
                              (direction == "SELL" and top_trend == "bullish")
                if top_aligned:
                    raw_confidence += 0.25
                elif top_opposed:
                    raw_confidence -= 0.3

        trend = analysis.get("trend", "unknown")
        if (direction == "BUY" and trend == "bullish") or (direction == "SELL" and trend == "bearish"):
            raw_confidence += 0.2
        elif (direction == "BUY" and trend == "bearish") or (direction == "SELL" and trend == "bullish"):
            raw_confidence -= 0.3

        strength = analysis.get("trend_strength", "moderate")
        if strength == "strong":
            raw_confidence += 0.15
        elif strength == "weak":
            raw_confidence -= 0.15

        confidence = max(0, min(100, raw_confidence * 35))

        if confidence < FOREX_MIN_CONFIDENCE:
            return None

        current = analysis["current_price"]
        atr = analysis.get("atr")
        pip_val = get_pip_value(symbol)
        support = analysis.get("support")
        resistance = analysis.get("resistance")

        sl_price, tp_price, sl_pips, tp_pips = self._calculate_levels(
            direction, current, atr, pip_val, support, resistance,
        )

        if sl_pips < FOREX_MIN_SL_PIPS or tp_pips < FOREX_MIN_TP_PIPS:
            return None

        rr = tp_pips / sl_pips if sl_pips > 0 else 0
        if rr < 1.2:
            return None

        reasoning_parts = [s["type"] for s in dominant]
        reasoning = f"{direction} signal from {', '.join(reasoning_parts)}. "
        reasoning += f"Trend: {trend} ({strength}). "
        if mtf:
            reasoning += f"Higher TF ({mtf['timeframe']}): {mtf['trend']} ({mtf.get('trend_strength', '?')}). "
            if mtf.get("top_timeframe"):
                reasoning += f"Top TF ({mtf['top_timeframe']}): {mtf.get('top_trend', '?')}. "
        reasoning += f"RSI: {analysis.get('rsi', '?'):.1f}. " if analysis.get("rsi") else ""
        reasoning += f"R:R = 1:{rr:.1f}."

        return {
            "pair": symbol,
            "direction": direction,
            "entry_price": current,
            "stop_loss": sl_price,
            "take_profit": tp_price,
            "sl_pips": round(sl_pips, 1),
            "tp_pips": round(tp_pips, 1),
            "risk_reward": round(rr, 2),
            "confidence": round(confidence, 1),
            "timeframe": timeframe,
            "reasoning": reasoning,
            "indicators": {
                "rsi": analysis.get("rsi"),
                "macd_histogram": analysis.get("macd_histogram"),
                "trend": trend,
                "trend_strength": strength,
                "momentum": analysis.get("momentum"),
                "atr": analysis.get("atr"),
                "support": support,
                "resistance": resistance,
                "bb_upper": analysis.get("bb_upper"),
                "bb_lower": analysis.get("bb_lower"),
            },
            "signal_components": [s["type"] for s in dominant],
            "mtf_confirmation": mtf,
        }

    def _calculate_levels(
        self,
        direction: str,
        current: float,
        atr: Optional[float],
        pip_val: float,
        support: Optional[float],
        resistance: Optional[float],
    ) -> tuple[float, float, float, float]:
        """Calculate SL/TP prices and pips using ATR and S/R levels."""
        if atr and atr > 0:
            sl_distance = atr * 1.5
            tp_distance = atr * 2.5
        else:
            sl_distance = current * 0.005
            tp_distance = current * 0.01

        if direction == "BUY":
            sl_price = current - sl_distance
            tp_price = current + tp_distance
            if support and support < current:
                sl_candidate = support - (current - support) * 0.1
                if abs(current - sl_candidate) < abs(current - sl_price) * 1.3:
                    sl_price = sl_candidate
            if resistance and resistance > current:
                tp_candidate = resistance
                if tp_candidate > tp_price * 0.8:
                    tp_price = tp_candidate
        else:
            sl_price = current + sl_distance
            tp_price = current - tp_distance
            if resistance and resistance > current:
                sl_candidate = resistance + (resistance - current) * 0.1
                if abs(sl_candidate - current) < abs(sl_price - current) * 1.3:
                    sl_price = sl_candidate
            if support and support < current:
                tp_candidate = support
                if tp_candidate < tp_price * 1.2:
                    tp_price = tp_candidate

        sl_pips = abs(current - sl_price) / pip_val
        tp_pips = abs(tp_price - current) / pip_val

        sl_pips = max(FOREX_MIN_SL_PIPS, min(FOREX_MAX_SL_PIPS, sl_pips))
        tp_pips = max(FOREX_MIN_TP_PIPS, min(FOREX_MAX_TP_PIPS, tp_pips))

        if direction == "BUY":
            sl_price = current - sl_pips * pip_val
            tp_price = current + tp_pips * pip_val
        else:
            sl_price = current + sl_pips * pip_val
            tp_price = current - tp_pips * pip_val

        return sl_price, tp_price, sl_pips, tp_pips
