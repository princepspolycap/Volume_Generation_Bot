"""
Bot 2 Backtest Strategy — v2 (Improved)
========================================
Simulates the Volume Generation Bot's Maker-Maker Ping-Pong strategy.

v2 changes:
  1. WIDEN SPREAD: spread_pct 0.05% → 0.12% (3x more P&L per win; trade-off is fewer fills)
  2. TIGHTEN STOP: stop_loss_pct 0.10% → 0.01% (survivable losses; each stop no longer wipes 11 wins)
  3. ATR VOLATILITY PAUSE: If 5-candle ATR > atr_pause_threshold, skip cycle.
     BTC is spiky during high-vol windows — sit them out, don't fight them.
  4. LEVERAGE REDUCED: 30x → 15x during tuning phase (less exposure while math is calibrated)
  5. POSITION UTILIZATION: kept at 0.60 (unchanged)

The core math problem in v1:
  - Win: +$0.12 per $100 equity (spread 0.05% minus 2x maker fee 0.04% = 0.01% net)
  - Stop: -$1.38 per $100 equity (stop 0.10% × 20x leverage)
  - Needed 92% win rate to break even. Got 52%.

v2 math (at default params):
  - Win: ~$0.16 per $100 equity (spread 0.12% minus 2x maker fee 0.04% = 0.08% net, × 15x lev × 0.60 util)
  - Stop: ~$0.015 per $100 equity (stop 0.01% × 15x leverage × 0.60 util)  — 10x smaller than wins
  - Break-even win rate: ~9%. At 52%+ this should be profitable.
"""
from typing import List, Dict
from engine import Trade


class VolumeGenBacktest:
    """
    Simulates the Volume Generation Bot's ping-pong strategy against historical data.
    """

    def __init__(
        self,
        equity: float = 150.0,
        leverage: int = 15,                  # v2: reduced from 30 → 15 during tuning
        position_utilization: float = 0.60,
        entry_offset_pct: float = 0.015,
        spread_pct: float = 0.12,            # v2: widened from 0.05% → 0.12%
        stop_loss_pct: float = 0.01,         # v2: tightened from 0.10% → 0.01%
        max_daily_loss_pct: float = 0.03,
        max_consecutive_losses: int = 5,
        maker_fee: float = 0.0002,
        taker_fee: float = 0.00055,
        entry_timeout_bars: int = 3,
        position_timeout_bars: int = 6,
        cooldown_bars: int = 1,
        cycle_pause_bars: int = 1,
        # v2: ATR volatility pause
        atr_pause_enabled: bool = True,      # v2: new — skip cycles during high volatility
        atr_pause_window: int = 5,           # v2: new — 5-candle ATR window
        atr_pause_threshold_pct: float = 0.08,  # v2: new — if 5m ATR > 0.08%, pause
    ):
        self.starting_equity = equity
        self.leverage = leverage
        self.position_utilization = position_utilization
        self.entry_offset_pct = entry_offset_pct
        self.spread_pct = spread_pct
        self.stop_loss_pct = stop_loss_pct
        self.max_daily_loss_pct = max_daily_loss_pct
        self.max_consecutive_losses = max_consecutive_losses
        self.maker_fee = maker_fee
        self.taker_fee = taker_fee
        self.entry_timeout_bars = entry_timeout_bars
        self.position_timeout_bars = position_timeout_bars
        self.cooldown_bars = cooldown_bars
        self.cycle_pause_bars = cycle_pause_bars
        self.atr_pause_enabled = atr_pause_enabled
        self.atr_pause_window = atr_pause_window
        self.atr_pause_threshold_pct = atr_pause_threshold_pct

    def _compute_atr_pct(self, candles: List[Dict], idx: int) -> float:
        """
        v2: Simple ATR as % of price over the last `atr_pause_window` candles.
        Returns the average True Range as a percentage of the closing price.
        """
        window = self.atr_pause_window
        if idx < window:
            return 0.0
        trs = []
        for j in range(idx - window + 1, idx + 1):
            high = candles[j]["high"]
            low = candles[j]["low"]
            prev_close = candles[j - 1]["close"]
            tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
            trs.append(tr)
        avg_tr = sum(trs) / len(trs)
        price = candles[idx]["close"]
        return (avg_tr / price) * 100 if price > 0 else 0.0

    def run(self, candles: List[Dict]) -> List[Trade]:
        trades = []
        equity = self.starting_equity
        equity_start_of_day = equity
        daily_pnl = 0.0
        consecutive_losses = 0
        next_is_long = True
        current_day = ""
        skip_until = 0

        i = 0
        while i < len(candles):
            c = candles[i]

            # Day management
            day_str = c.get("datetime", "")[:10]
            if day_str != current_day:
                current_day = day_str
                equity_start_of_day = equity
                daily_pnl = 0.0
                consecutive_losses = 0

            # Skip if in cooldown/pause
            if i < skip_until:
                i += 1
                continue

            # Trade gate checks
            if equity < 20:
                i += 1
                continue

            max_daily_loss = equity_start_of_day * self.max_daily_loss_pct
            if -daily_pnl >= max_daily_loss:
                i += 1
                continue

            if consecutive_losses >= self.max_consecutive_losses:
                i += 1
                continue

            # v2: ATR volatility pause
            if self.atr_pause_enabled and i >= self.atr_pause_window:
                current_atr_pct = self._compute_atr_pct(candles, i)
                if current_atr_pct > self.atr_pause_threshold_pct:
                    i += 1
                    continue

            # Calculate position size
            mid = c["close"]
            max_notional = equity * self.leverage * self.position_utilization
            size = max_notional / mid
            notional = size * mid

            if notional < 5.0:
                i += 1
                continue

            # Check spread (use high-low as proxy for book spread)
            candle_spread_pct = (c["high"] - c["low"]) / c["low"] * 100 if c["low"] > 0 else float("inf")
            if candle_spread_pct > 0.20:
                i += 1
                continue

            is_long = next_is_long

            # Entry price
            if is_long:
                entry_price = mid * (1 - self.entry_offset_pct / 100)
            else:
                entry_price = mid * (1 + self.entry_offset_pct / 100)

            # Check if entry would fill within timeout window
            entry_filled = False
            actual_entry = entry_price
            entry_bar = i

            for j in range(i, min(i + self.entry_timeout_bars, len(candles))):
                bar = candles[j]
                if is_long and bar["low"] <= entry_price:
                    entry_filled = True
                    actual_entry = entry_price
                    entry_bar = j
                    break
                elif not is_long and bar["high"] >= entry_price:
                    entry_filled = True
                    actual_entry = entry_price
                    entry_bar = j
                    break

            if not entry_filled:
                i += self.entry_timeout_bars
                continue

            # Entry filled — calculate exit targets
            if is_long:
                exit_price = actual_entry * (1 + self.spread_pct / 100)
                stop_price = actual_entry * (1 - self.stop_loss_pct / 100)
            else:
                exit_price = actual_entry * (1 - self.spread_pct / 100)
                stop_price = actual_entry * (1 + self.stop_loss_pct / 100)

            # Check exit within position timeout
            exit_filled = False
            stop_hit = False
            timed_out = False
            final_exit_price = actual_entry
            exit_bar = entry_bar

            for j in range(entry_bar + 1, min(entry_bar + 1 + self.position_timeout_bars, len(candles))):
                bar = candles[j]

                # Check stop first (more conservative)
                if is_long and bar["low"] <= stop_price:
                    stop_hit = True
                    final_exit_price = stop_price
                    exit_bar = j
                    break
                elif not is_long and bar["high"] >= stop_price:
                    stop_hit = True
                    final_exit_price = stop_price
                    exit_bar = j
                    break

                # Check take profit
                if is_long and bar["high"] >= exit_price:
                    exit_filled = True
                    final_exit_price = exit_price
                    exit_bar = j
                    break
                elif not is_long and bar["low"] <= exit_price:
                    exit_filled = True
                    final_exit_price = exit_price
                    exit_bar = j
                    break

            if not exit_filled and not stop_hit:
                timed_out = True
                timeout_idx = min(entry_bar + 1 + self.position_timeout_bars, len(candles) - 1)
                final_exit_price = candles[timeout_idx]["close"]
                exit_bar = timeout_idx

            # Calculate PnL
            entry_fee = notional * self.maker_fee
            if exit_filled:
                exit_fee = notional * self.maker_fee
            else:
                exit_fee = notional * self.taker_fee

            fees = entry_fee + exit_fee

            if is_long:
                raw_pnl = (final_exit_price - actual_entry) * size
            else:
                raw_pnl = (actual_entry - final_exit_price) * size

            net_pnl = raw_pnl - fees
            equity += net_pnl
            daily_pnl += net_pnl

            if net_pnl < 0:
                consecutive_losses += 1
            else:
                consecutive_losses = 0

            exit_reason = "take_profit" if exit_filled else ("stop_loss" if stop_hit else "timeout")

            trades.append(Trade(
                strategy="volume_gen_pingpong",
                side="BUY" if is_long else "SELL",
                entry_price=actual_entry,
                exit_price=final_exit_price,
                size=size,
                entry_time=candles[entry_bar].get("datetime", ""),
                exit_time=candles[exit_bar].get("datetime", ""),
                pnl=net_pnl,
                fees=fees,
                notional=notional,
                reason="pingpong_" + ("long" if is_long else "short"),
                exit_reason=exit_reason,
            ))

            next_is_long = not next_is_long

            if stop_hit:
                skip_until = exit_bar + self.cooldown_bars + 1
            else:
                skip_until = exit_bar + self.cycle_pause_bars + 1

            i = exit_bar + 1
            continue

            i += 1

        return trades
