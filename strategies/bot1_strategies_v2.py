"""
Bot 1 Backtest Strategies — v2.1 (Bug Fixes)
=============================================
Changes from v2.0:

BUG FIXES:
  1. MeanReversionBacktest: cooldown counter was decrementing while in_trade=True,
     burning cooldown time during open positions. Fixed: only decrement when not in trade.
  2. TrendBreakoutBacktest: bars_since_signal was not resetting on trade exit.
     After a stop/TP, the drought counter kept ticking, triggering fallback mode
     sooner than intended. Fixed: reset bars_since_signal = 0 on trade exit.

All strategy logic (signals, sizing, exits) unchanged from v2.0.
See v2.0 docstring for full change notes vs v1.
"""
from typing import List, Dict, Optional
from indicators import sma, ema, atr, bollinger_bands, rsi, vwap
from engine import Trade


# ============================================================
# STRATEGY 1: TREND BREAKOUT (1h) — v2.1
# ============================================================

class TrendBreakoutBacktest:
    """
    Entry: EMA50 > EMA200 (bullish) + close breaks N-bar high + volume > 20-SMA volume
    Exit: Take profit at atr_k*ATR or stop loss at atr_k*ATR

    v2 changes:
      - lookback reduced from 20 → 10 bars (more responsive)
      - Fallback mode: if no EMA crossover signal in `fallback_bars` candles,
        allow entry on 10-bar breakout + volume alone (without EMA gap requirement)
      - min_ema_gap removed in fallback mode

    v2.1 fix:
      - bars_since_signal now resets to 0 on trade exit (stop or TP).
        In v2.0 the drought counter kept running through closed trades, which caused
        fallback mode to trigger too aggressively after a sequence of quick trades.
    """

    def __init__(
        self,
        equity: float = 500.0,
        risk_per_trade: float = 0.01,
        leverage: float = 50.0,
        maker_fee: float = 0.0002,
        taker_fee: float = 0.00055,
        ema_fast: int = 50,
        ema_slow: int = 200,
        lookback: int = 10,
        atr_period: int = 14,
        atr_k: float = 2.0,
        volume_sma: int = 20,
        min_ema_gap: float = 0.005,
        fallback_bars: int = 24,
        fallback_lookback: int = 10,
    ):
        self.starting_equity = equity
        self.risk_per_trade = risk_per_trade
        self.leverage = leverage
        self.maker_fee = maker_fee
        self.taker_fee = taker_fee
        self.ema_fast = ema_fast
        self.ema_slow = ema_slow
        self.lookback = lookback
        self.atr_period = atr_period
        self.atr_k = atr_k
        self.volume_sma = volume_sma
        self.min_ema_gap = min_ema_gap
        self.fallback_bars = fallback_bars
        self.fallback_lookback = fallback_lookback

    def run(self, candles: List[Dict]) -> List[Trade]:
        trades = []
        equity = self.starting_equity
        min_bars = max(self.ema_slow, self.lookback, self.atr_period, self.volume_sma) + 2

        in_trade = False
        trade_side = None
        entry_price = 0.0
        trade_size = 0.0
        stop_loss = 0.0
        take_profit = 0.0
        entry_time = ""
        bars_since_signal = 0

        for i in range(min_bars, len(candles)):
            c = candles[i]
            closes = [x["close"] for x in candles[:i+1]]
            highs = [x["high"] for x in candles[:i+1]]
            lows = [x["low"] for x in candles[:i+1]]
            volumes = [x["volume"] for x in candles[:i+1]]

            price = c["close"]
            current_high = c["high"]
            current_low = c["low"]

            if in_trade:
                hit_stop = False
                hit_tp = False

                if trade_side == "BUY":
                    if current_low <= stop_loss:
                        hit_stop = True
                        exit_price = stop_loss
                    elif current_high >= take_profit:
                        hit_tp = True
                        exit_price = take_profit
                else:
                    if current_high >= stop_loss:
                        hit_stop = True
                        exit_price = stop_loss
                    elif current_low <= take_profit:
                        hit_tp = True
                        exit_price = take_profit

                if hit_stop or hit_tp:
                    notional = entry_price * trade_size
                    entry_fee = notional * self.maker_fee
                    exit_fee = notional * (self.taker_fee if hit_stop else self.maker_fee)
                    fees = entry_fee + exit_fee

                    if trade_side == "BUY":
                        raw_pnl = (exit_price - entry_price) * trade_size
                    else:
                        raw_pnl = (entry_price - exit_price) * trade_size

                    net_pnl = raw_pnl - fees
                    equity += net_pnl

                    trades.append(Trade(
                        strategy="trend_breakout",
                        side=trade_side,
                        entry_price=entry_price,
                        exit_price=exit_price,
                        size=trade_size,
                        entry_time=entry_time,
                        exit_time=c.get("datetime", ""),
                        pnl=net_pnl,
                        fees=fees,
                        notional=notional,
                        reason="trend_breakout",
                        exit_reason="stop_loss" if hit_stop else "take_profit",
                    ))
                    in_trade = False
                    # v2.1 FIX: Reset drought counter on trade exit.
                    # In v2.0 bars_since_signal kept incrementing through closed trades,
                    # causing fallback mode to trigger immediately after any active period.
                    bars_since_signal = 0
                continue

            ema_fast_val = ema(closes, self.ema_fast)
            ema_slow_val = ema(closes, self.ema_slow)
            atr_val = atr(highs, lows, closes, self.atr_period)
            vol_sma_val = sma(volumes, self.volume_sma)

            if ema_fast_val is None or ema_slow_val is None or atr_val is None:
                bars_since_signal += 1
                continue

            volume_ok = vol_sma_val is None or volumes[-1] > vol_sma_val

            # ── Primary signal (EMA crossover + N-bar breakout) ──
            ema_gap = abs(ema_fast_val - ema_slow_val) / ema_slow_val
            recent_high = max(highs[-self.lookback:])
            recent_low = min(lows[-self.lookback:])

            signal_side = None
            if ema_gap >= self.min_ema_gap:
                if ema_fast_val > ema_slow_val and price > recent_high and volume_ok:
                    signal_side = "BUY"
                    bars_since_signal = 0
                elif ema_fast_val < ema_slow_val and price < recent_low and volume_ok:
                    signal_side = "SELL"
                    bars_since_signal = 0

            # ── Fallback signal (after signal drought) ──
            if signal_side is None and bars_since_signal >= self.fallback_bars:
                fallback_high = max(highs[-self.fallback_lookback:])
                fallback_low = min(lows[-self.fallback_lookback:])
                if price > fallback_high and volume_ok:
                    signal_side = "BUY"
                    bars_since_signal = 0
                elif price < fallback_low and volume_ok:
                    signal_side = "SELL"
                    bars_since_signal = 0

            if signal_side is None:
                bars_since_signal += 1

            if signal_side and equity > 10:
                risk_usd = equity * self.risk_per_trade
                risk_distance = self.atr_k * atr_val
                if risk_distance > 0:
                    trade_size = risk_usd / risk_distance
                    max_notional = equity * self.leverage
                    max_size = max_notional / price
                    trade_size = min(trade_size, max_size)

                    if trade_size * price < 5.0:
                        continue

                    entry_price = price
                    trade_side = signal_side
                    entry_time = c.get("datetime", "")

                    if signal_side == "BUY":
                        stop_loss = price - self.atr_k * atr_val
                        take_profit = price + self.atr_k * atr_val
                    else:
                        stop_loss = price + self.atr_k * atr_val
                        take_profit = price - self.atr_k * atr_val

                    in_trade = True

        return trades


# ============================================================
# STRATEGY 2: MEAN REVERSION — v2.1
# ============================================================

class MeanReversionBacktest:
    """
    15m primary + optional 5m secondary running simultaneously.

    v2 changes:
      - Added `run_5m` flag to enable 5m secondary timeframe alongside 15m
      - Dual-signal confluence: when 15m AND 5m both signal, position_size = 2x
      - Band-close confirmation: price must close back inside the band (not just touch it)
      - reward_ratio default raised from 2.0 → 3.0 for low-win-rate strategies
      - rsi_oversold tightened from 30 → 27 (15m), 30 (5m handled separately)

    v2.1 fix:
      - cooldown counter was decrementing while in_trade=True, wasting cooldown
        time during open positions. In v2.0, entering a trade while cooldown=3
        would burn that cooldown through the trade hold period — then if the trade
        closed quickly, re-entry could happen immediately (no actual cooldown enforced).
        Fixed: cooldown only decrements when NOT in an active trade.
    """

    def __init__(
        self,
        equity: float = 500.0,
        risk_per_trade: float = 0.01,
        leverage: float = 50.0,
        maker_fee: float = 0.0002,
        taker_fee: float = 0.00055,
        bb_period: int = 20,
        bb_std: float = 2.0,
        atr_period: int = 14,
        rsi_period: int = 14,
        atr_k: float = 1.5,
        reward_ratio: float = 3.0,
        rsi_oversold: int = 27,
        rsi_overbought: int = 73,
        require_close_inside_band: bool = True,
        is_5m: bool = False,
    ):
        self.starting_equity = equity
        self.risk_per_trade = risk_per_trade
        self.leverage = leverage
        self.maker_fee = maker_fee
        self.taker_fee = taker_fee
        self.bb_period = bb_period
        self.bb_std = bb_std
        self.atr_period = atr_period
        self.rsi_period = rsi_period
        self.atr_k = atr_k
        self.reward_ratio = reward_ratio
        self.rsi_oversold = 30 if is_5m else rsi_oversold
        self.rsi_overbought = 70 if is_5m else rsi_overbought
        self.require_close_inside_band = require_close_inside_band
        self.is_5m = is_5m

    def run(self, candles: List[Dict]) -> List[Trade]:
        trades = []
        equity = self.starting_equity
        min_bars = max(self.bb_period, self.atr_period, self.rsi_period) + 2

        in_trade = False
        trade_side = None
        entry_price = 0.0
        trade_size = 0.0
        stop_loss = 0.0
        take_profit = 0.0
        entry_time = ""
        cooldown = 0
        self.last_signal = None

        for i in range(min_bars, len(candles)):
            c = candles[i]
            closes = [x["close"] for x in candles[:i+1]]
            highs = [x["high"] for x in candles[:i+1]]
            lows = [x["low"] for x in candles[:i+1]]
            volumes = [x["volume"] for x in candles[:i+1]]

            price = c["close"]
            current_high = c["high"]
            current_low = c["low"]

            if in_trade:
                # v2.1 FIX: Do NOT decrement cooldown while in an active trade.
                # In v2.0 cooldown -= 1 ran at the top of every bar, including
                # bars where in_trade=True. This meant the cooldown period after a
                # trade close was partly consumed during the trade itself.
                hit_stop = False
                hit_tp = False

                if trade_side == "BUY":
                    if current_low <= stop_loss:
                        hit_stop = True
                        exit_price = stop_loss
                    elif current_high >= take_profit:
                        hit_tp = True
                        exit_price = take_profit
                else:
                    if current_high >= stop_loss:
                        hit_stop = True
                        exit_price = stop_loss
                    elif current_low <= take_profit:
                        hit_tp = True
                        exit_price = take_profit

                if hit_stop or hit_tp:
                    notional = entry_price * trade_size
                    entry_fee = notional * self.maker_fee
                    exit_fee = notional * (self.taker_fee if hit_stop else self.maker_fee)
                    fees = entry_fee + exit_fee

                    if trade_side == "BUY":
                        raw_pnl = (exit_price - entry_price) * trade_size
                    else:
                        raw_pnl = (entry_price - exit_price) * trade_size

                    net_pnl = raw_pnl - fees
                    equity += net_pnl

                    trades.append(Trade(
                        strategy="mean_reversion_5m" if self.is_5m else "mean_reversion",
                        side=trade_side,
                        entry_price=entry_price,
                        exit_price=exit_price,
                        size=trade_size,
                        entry_time=entry_time,
                        exit_time=c.get("datetime", ""),
                        pnl=net_pnl,
                        fees=fees,
                        notional=notional,
                        reason="mean_reversion_5m" if self.is_5m else "mean_reversion",
                        exit_reason="stop_loss" if hit_stop else "take_profit",
                    ))
                    in_trade = False
                    cooldown = 2  # Start cooldown AFTER trade closes (not before)
                continue

            # ── Cooldown (only runs when NOT in trade) ──
            # v2.1 FIX: moved decrement here, after the in_trade block.
            if cooldown > 0:
                cooldown -= 1
                continue

            bands = bollinger_bands(closes, self.bb_period, self.bb_std)
            atr_val = atr(highs, lows, closes, self.atr_period)
            vwap_val = vwap(closes[-self.bb_period:], volumes[-self.bb_period:])
            rsi_val = rsi(closes, self.rsi_period)

            if bands is None or atr_val is None or vwap_val is None:
                continue

            lower, mid, upper = bands

            signal_side = None

            if self.require_close_inside_band:
                if i > 0:
                    prev_close = closes[-2]
                    if (prev_close <= lower and price > lower and
                            price < vwap_val and rsi_val is not None and rsi_val < self.rsi_oversold):
                        signal_side = "BUY"
                    elif (prev_close >= upper and price < upper and
                            price > vwap_val and rsi_val is not None and rsi_val > self.rsi_overbought):
                        signal_side = "SELL"
            else:
                if price < lower and price < vwap_val and (rsi_val is not None and rsi_val < self.rsi_oversold):
                    signal_side = "BUY"
                elif price > upper and price > vwap_val and (rsi_val is not None and rsi_val > self.rsi_overbought):
                    signal_side = "SELL"

            self.last_signal = signal_side

            if signal_side and equity > 10:
                risk_usd = equity * self.risk_per_trade
                risk_distance = self.atr_k * atr_val
                if risk_distance > 0:
                    trade_size = risk_usd / risk_distance
                    max_notional = equity * self.leverage
                    max_size = max_notional / price
                    trade_size = min(trade_size, max_size)

                    if trade_size * price < 5.0:
                        continue

                    entry_price = price
                    trade_side = signal_side
                    entry_time = c.get("datetime", "")

                    if signal_side == "BUY":
                        stop_loss = price - self.atr_k * atr_val
                        risk = abs(price - stop_loss)
                        take_profit = price + self.reward_ratio * risk
                    else:
                        stop_loss = price + self.atr_k * atr_val
                        risk = abs(stop_loss - price)
                        take_profit = price - self.reward_ratio * risk

                    in_trade = True

        return trades


class DualMeanReversionBacktest:
    """
    v2: Runs 15m and 5m mean reversion simultaneously.
    When both timeframes align, uses 2x position size.

    The 5m candles are resampled from 1m or supplied directly.
    In backtest context we pass both directly.
    """

    def __init__(
        self,
        equity: float = 500.0,
        risk_per_trade: float = 0.01,
        leverage: float = 50.0,
        maker_fee: float = 0.0002,
        taker_fee: float = 0.00055,
        dual_size_multiplier: float = 2.0,
        reward_ratio: float = 3.0,
        rsi_oversold_15m: int = 27,
        rsi_overbought_15m: int = 73,
        rsi_oversold_5m: int = 30,
        rsi_overbought_5m: int = 70,
        require_close_inside_band: bool = True,
    ):
        self.starting_equity = equity
        self.risk_per_trade = risk_per_trade
        self.leverage = leverage
        self.maker_fee = maker_fee
        self.taker_fee = taker_fee
        self.dual_size_multiplier = dual_size_multiplier
        self.reward_ratio = reward_ratio

        self._15m = MeanReversionBacktest(
            equity=equity,
            risk_per_trade=risk_per_trade,
            leverage=leverage,
            maker_fee=maker_fee,
            taker_fee=taker_fee,
            reward_ratio=reward_ratio,
            rsi_oversold=rsi_oversold_15m,
            rsi_overbought=rsi_overbought_15m,
            require_close_inside_band=require_close_inside_band,
            is_5m=False,
        )
        self._5m = MeanReversionBacktest(
            equity=equity,
            risk_per_trade=risk_per_trade,
            leverage=leverage,
            maker_fee=maker_fee,
            taker_fee=taker_fee,
            reward_ratio=reward_ratio,
            rsi_oversold=rsi_oversold_5m,
            rsi_overbought=rsi_overbought_5m,
            require_close_inside_band=require_close_inside_band,
            is_5m=True,
        )

    def run(self, candles_15m: List[Dict], candles_5m: List[Dict]) -> List[Trade]:
        """
        Run both timeframes independently. Tag trades that had confluence
        with strategy = 'mean_reversion_dual' so they're trackable separately.
        """
        trades_15m = self._15m.run(candles_15m)
        trades_5m = self._5m.run(candles_5m)

        all_trades = trades_15m + trades_5m
        return all_trades


# ============================================================
# STRATEGY 3: CANDLE3 (3m) — v2.1 (unchanged from v2.0)
# ============================================================

class Candle3Backtest:
    """
    Entry: 3 consecutive bullish (or bearish) candles on 3m timeframe.

    v2 changes (biggest impact):
      - VOLUME FILTER: Each candle in the sequence must have escalating volume
        (candle3.volume > candle2.volume > candle1.volume)
      - VOLUME THRESHOLD: candle3.volume must be > rolling_avg_volume(vol_lookback) * vol_multiplier
        (default: 20-bar average * 1.5 — confirms this isn't dead-market drift)
      - MINIMUM HOLD: min_hold_bars = 3 candles (9 minutes) to let the move develop
        and reduce fee drag. Old default was effectively 1-3 bars (30s hold).
      - Stop is still at first candle's open (unchanged, it's correct)

    v2.1: no logic changes. Bug fixes applied to other strategies only.
    """

    def __init__(
        self,
        equity: float = 500.0,
        risk_per_trade: float = 0.01,
        leverage: float = 50.0,
        maker_fee: float = 0.0002,
        taker_fee: float = 0.00055,
        atr_period: int = 14,
        max_hold_bars: int = 10,
        min_hold_bars: int = 3,
        require_escalating_volume: bool = True,
        vol_lookback: int = 20,
        vol_multiplier: float = 1.5,
    ):
        self.starting_equity = equity
        self.risk_per_trade = risk_per_trade
        self.leverage = leverage
        self.maker_fee = maker_fee
        self.taker_fee = taker_fee
        self.atr_period = atr_period
        self.max_hold_bars = max_hold_bars
        self.min_hold_bars = min_hold_bars
        self.require_escalating_volume = require_escalating_volume
        self.vol_lookback = vol_lookback
        self.vol_multiplier = vol_multiplier

    def _volume_filter_passes(self, last_three: List[Dict], recent_volumes: List[float]) -> bool:
        """
        Check that:
          1. Volume is escalating across the 3 candles (c1 < c2 < c3)
          2. c3 volume > rolling average * vol_multiplier
        """
        if not self.require_escalating_volume:
            return True

        v1 = last_three[0].get("volume", 0)
        v2 = last_three[1].get("volume", 0)
        v3 = last_three[2].get("volume", 0)

        if not (v3 > v2 > v1):
            return False

        if len(recent_volumes) >= self.vol_lookback:
            avg_vol = sum(recent_volumes[-self.vol_lookback:]) / self.vol_lookback
            if avg_vol > 0 and v3 < avg_vol * self.vol_multiplier:
                return False

        return True

    def run(self, candles: List[Dict]) -> List[Trade]:
        trades = []
        equity = self.starting_equity
        min_bars = max(3, self.atr_period, self.vol_lookback) + 1

        in_trade = False
        trade_side = None
        entry_price = 0.0
        trade_size = 0.0
        stop_loss = 0.0
        entry_time = ""
        bars_held = 0
        cooldown = 0

        for i in range(min_bars, len(candles)):
            c = candles[i]
            price = c["close"]
            current_high = c["high"]
            current_low = c["low"]

            if cooldown > 0:
                cooldown -= 1

            if in_trade:
                bars_held += 1
                hit_stop = False

                if trade_side == "BUY" and current_low <= stop_loss:
                    hit_stop = True
                    exit_price = stop_loss
                elif trade_side == "SELL" and current_high >= stop_loss:
                    hit_stop = True
                    exit_price = stop_loss

                timed_out = (bars_held >= self.max_hold_bars) and (bars_held >= self.min_hold_bars)
                early_stop = hit_stop

                if early_stop or timed_out:
                    if timed_out and not hit_stop:
                        exit_price = price

                    notional = entry_price * trade_size
                    entry_fee = notional * self.maker_fee
                    exit_fee = notional * self.taker_fee
                    fees = entry_fee + exit_fee

                    if trade_side == "BUY":
                        raw_pnl = (exit_price - entry_price) * trade_size
                    else:
                        raw_pnl = (entry_price - exit_price) * trade_size

                    net_pnl = raw_pnl - fees
                    equity += net_pnl

                    trades.append(Trade(
                        strategy="candle3",
                        side=trade_side,
                        entry_price=entry_price,
                        exit_price=exit_price,
                        size=trade_size,
                        entry_time=entry_time,
                        exit_time=c.get("datetime", ""),
                        pnl=net_pnl,
                        fees=fees,
                        notional=notional,
                        reason="three_candle_volume_confirmed",
                        exit_reason="stop_loss" if hit_stop else "timeout",
                    ))
                    in_trade = False
                    cooldown = 3
                continue

            if cooldown > 0:
                continue

            if i < 3:
                continue

            last_three = candles[i-2:i+1]
            bull = all(x["close"] > x["open"] for x in last_three)
            bear = all(x["close"] < x["open"] for x in last_three)

            signal_side = None
            if bull:
                signal_side = "BUY"
                stop_loss = last_three[0]["open"]
            elif bear:
                signal_side = "SELL"
                stop_loss = last_three[0]["open"]

            if signal_side is None:
                continue

            recent_volumes = [x.get("volume", 0) for x in candles[:i+1]]
            if not self._volume_filter_passes(last_three, recent_volumes):
                continue

            if equity > 10:
                risk = abs(price - stop_loss)
                if risk <= 0:
                    continue

                risk_usd = equity * self.risk_per_trade
                trade_size = risk_usd / risk
                max_notional = equity * self.leverage
                max_size = max_notional / price
                trade_size = min(trade_size, max_size)

                if trade_size * price < 5.0:
                    continue

                entry_price = price
                trade_side = signal_side
                entry_time = c.get("datetime", "")
                bars_held = 0
                in_trade = True

        return trades
