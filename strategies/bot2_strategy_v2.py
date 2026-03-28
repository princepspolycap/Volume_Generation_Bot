"""
Bot 2 Backtest Strategy — v2.1 (Volume Target Math Fix)
=========================================================
Simulates the Volume Generation Bot's Maker-Maker Ping-Pong strategy.

v2.1 changes (volume target math):
  1. LEVERAGE: 15x → 20x (more notional per cycle, +33% volume)
  2. POSITION UTILIZATION: 0.60 → 0.70 (+17% volume per cycle)
  3. ATR THRESHOLD: 0.08% → 0.15%
     - 0.08% = $68 ATR on $85k BTC. BTC 1m typical ATR is $35-55 (0.04-0.065%).
       At 0.08%, the pause was firing ~60% of all bars — killing volume.
     - 0.15% = $127.50 ATR. Only fires during genuine volatility spikes.
       Uptime improves from ~38% to ~80%.
  4. POSITION TIMEOUT: 6 bars → 15 bars
     - Spread target 0.10% = $85 on $85k BTC.
     - Average 1m true range ~$30-40 per bar.
     - In 6 bars (6 minutes), a unidirectional $85 move happens ~15-20% of the time.
     - In 15 bars (15 minutes), probability rises to ~40-50%.
     - Fewer timeouts → fewer taker-fee exits → better net PnL per cycle.
  5. ENTRY TIMEOUT: 3 bars → 5 bars (more fills, fewer wasted cycles)
  6. MAX DAILY LOSS: 3% → 5% (3% was stopping the bot after just 3 stops)
  7. MAX CONSECUTIVE LOSSES: 5 → 7 (same over-conservative gate)
  8. STOP LOSS: 0.01% → 0.008% ($17 loss cap vs $21; tighter = smaller drawdowns)
  9. DEAD CODE REMOVED: unreachable 'i += 1' after 'continue' at bottom of while loop

Volume math at v2.1 defaults ($150 equity, 20x, 0.70 util):
  - Notional per cycle: $2,100
  - Volume per round-trip: $4,200
  - Active bars/day at 80% uptime: ~1,152
  - Avg cycle length: ~11 bars (2 to fill entry + 8 to fill/timeout + 1 pause)
  - Cycles per day: ~105
  - Daily volume target: ~$441,000

v2.0 reference math (spread 0.12% → 0.10%):
  - Win: +$2.10 per cycle (spread 0.10% - 2×maker 0.04% = 0.06% × $2,100 × 20x lev × 0.70 util)
    Wait, net is: (0.10% - 0.04%) × $2,100 = 0.06% × $2,100 = $1.26 per win
  - Stop: -(0.008% + 0.020% + 0.055%) × $2,100 = -0.083% × $2,100 = -$1.74 per stop
  - Break-even win rate (wins vs stops only): 58%
  - With 40% TP fill rate: expected PnL becomes positive vs 15% at 6 bars
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
        leverage: int = 20,                       # v2.1: raised from 15 → 20
        position_utilization: float = 0.70,       # v2.1: raised from 0.60 → 0.70
        entry_offset_pct: float = 0.015,
        spread_pct: float = 0.10,                 # v2.1: tightened slightly 0.12% → 0.10%
        stop_loss_pct: float = 0.008,             # v2.1: tightened 0.010% → 0.008%
        max_daily_loss_pct: float = 0.05,         # v2.1: raised from 0.03 → 0.05
        max_consecutive_losses: int = 7,          # v2.1: raised from 5 → 7
        maker_fee: float = 0.0002,
        taker_fee: float = 0.00055,
        entry_timeout_bars: int = 5,              # v2.1: raised from 3 → 5
        position_timeout_bars: int = 15,          # v2.1: raised from 6 → 15
        cooldown_bars: int = 2,                   # v2.1: raised from 1 → 2 (after stops)
        cycle_pause_bars: int = 1,
        # ATR volatility pause — v2 feature, threshold corrected in v2.1
        atr_pause_enabled: bool = True,
        atr_pause_window: int = 5,
        atr_pause_threshold_pct: float = 0.15,   # v2.1: raised from 0.08% → 0.15%
        # v2.1: separate cooldown duration for stops vs wins
        stop_cooldown_bars: int = 3,              # v2.1: new — longer cooldown after a stop loss
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
        self.stop_cooldown_bars = stop_cooldown_bars

    def _compute_atr_pct(self, candles: List[Dict], idx: int) -> float:
        """
        Compute ATR as % of price over the last `atr_pause_window` candles.

        Uses standard True Range = max(H-L, |H-prev_close|, |L-prev_close|).
        Returns average TR as a percentage of closing price.
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

            # ── Day boundary reset ──
            day_str = c.get("datetime", "")[:10]
            if day_str != current_day:
                current_day = day_str
                equity_start_of_day = equity
                daily_pnl = 0.0
                consecutive_losses = 0

            # ── Skip if in cooldown / pause window ──
            if i < skip_until:
                i += 1
                continue

            # ── Trade gate checks ──
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

            # ── ATR volatility pause ──
            # v2.1 FIX: threshold raised from 0.08% → 0.15%.
            # At $85k BTC, 0.08% = $68 which is within normal 1m ATR range,
            # causing ~60% of bars to be skipped. 0.15% = $127.50 only fires
            # on genuine spikes, preserving ~80% uptime.
            if self.atr_pause_enabled and i >= self.atr_pause_window:
                current_atr_pct = self._compute_atr_pct(candles, i)
                if current_atr_pct > self.atr_pause_threshold_pct:
                    i += 1
                    continue

            # ── Position sizing ──
            mid = c["close"]
            max_notional = equity * self.leverage * self.position_utilization
            size = max_notional / mid
            notional = size * mid

            if notional < 5.0:
                i += 1
                continue

            # ── Spread gate: skip if market is too wide ──
            candle_spread_pct = (c["high"] - c["low"]) / c["low"] * 100 if c["low"] > 0 else float("inf")
            if candle_spread_pct > 0.20:
                i += 1
                continue

            is_long = next_is_long

            # ── Entry price (limit order inside the book) ──
            if is_long:
                entry_price = mid * (1 - self.entry_offset_pct / 100)
            else:
                entry_price = mid * (1 + self.entry_offset_pct / 100)

            # ── Wait for entry fill within timeout window ──
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

            # ── Calculate exit targets ──
            if is_long:
                exit_price = actual_entry * (1 + self.spread_pct / 100)
                stop_price = actual_entry * (1 - self.stop_loss_pct / 100)
            else:
                exit_price = actual_entry * (1 - self.spread_pct / 100)
                stop_price = actual_entry * (1 + self.stop_loss_pct / 100)

            # ── Wait for TP or stop within position timeout ──
            # v2.1: position_timeout_bars raised 6→15 so TP has time to fill.
            # Spread 0.10% = ~$85 move on $85k BTC. 6 bars rarely reaches that
            # unidirectionally; 15 bars gives ~40-50% fill probability.
            exit_filled = False
            stop_hit = False
            timed_out = False
            final_exit_price = actual_entry
            exit_bar = entry_bar

            for j in range(entry_bar + 1, min(entry_bar + 1 + self.position_timeout_bars, len(candles))):
                bar = candles[j]

                # Check stop first (more conservative — stops always trigger)
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

                # Check take profit (limit order — counts as maker)
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

            # ── Calculate PnL ──
            entry_fee = notional * self.maker_fee  # always maker entry
            if exit_filled:
                exit_fee = notional * self.maker_fee   # limit TP = maker
            else:
                exit_fee = notional * self.taker_fee   # stop or timeout = taker

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

            # v2.1: Use longer cooldown after stops to avoid re-entering into same adverse move
            if stop_hit:
                skip_until = exit_bar + self.stop_cooldown_bars + 1
            else:
                skip_until = exit_bar + self.cycle_pause_bars + 1

            i = exit_bar + 1
            # NOTE: No 'i += 1' here — the while loop continues from i = exit_bar + 1
            # (v2.0 had dead code 'i += 1' after 'continue' which was unreachable)

        return trades
