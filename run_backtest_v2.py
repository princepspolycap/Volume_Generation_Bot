"""
Panther Backtest Runner — v2
============================
Runs v2 strategy improvements side-by-side with v1 baselines for comparison.

Usage:
    python run_backtest_v2.py                    # Default: 30 days, BTCUSDT, $500 equity
    python run_backtest_v2.py --days 7           # 7 days
    python run_backtest_v2.py --symbol ETHUSDT   # Test on ETH
    python run_backtest_v2.py --equity 500       # $500 starting equity
    python run_backtest_v2.py --no-download      # Use cached data

Changes vs v1 runner:
  - Uses bot1_strategies_v2 (Candle3 + MeanReversion + TrendBreakout improved)
  - Uses bot2_strategy_v2 (VolumeGen wider spread, tighter stop, ATR pause)
  - Adds DualMeanReversionBacktest (15m + 5m confluence sizing)
  - Side-by-side diff: v1 vs v2 for each strategy
"""
import argparse
import json
import os
import sys
from datetime import datetime, timezone

from data_fetcher import fetch_full_history, save_candles, load_candles
from engine import BacktestEngine, BacktestResult
# v1 (baseline)
from bot1_strategies import TrendBreakoutBacktest as TrendBreakoutV1
from bot1_strategies import MeanReversionBacktest as MeanReversionV1
from bot1_strategies import Candle3Backtest as Candle3V1
from bot2_strategy import VolumeGenBacktest as VolumeGenV1
# v2 (improved)
from bot1_strategies_v2 import TrendBreakoutBacktest as TrendBreakoutV2
from bot1_strategies_v2 import MeanReversionBacktest as MeanReversionV2
from bot1_strategies_v2 import DualMeanReversionBacktest
from bot1_strategies_v2 import Candle3Backtest as Candle3V2
from bot2_strategy_v2 import VolumeGenBacktest as VolumeGenV2


def run_bot1_v2(candles_1h, candles_5m, candles_3m, equity: float) -> dict:
    """Run all three Bot1 v2 strategies."""
    print("\\n" + "="*60)
    print("BOT 1: PANTHER TRADING BOT v2 (Multi-Strategy Improved)")
    print("="*60)

    engine = BacktestEngine(starting_equity=equity)
    results = {}

    # Strategy 1: Trend Breakout v2 (10-bar lookback + fallback)
    print("\\n  Running Trend Breakout v2 (1h) [10-bar lookback + fallback]...")
    trend = TrendBreakoutV2(equity=equity)
    trend_trades = trend.run(candles_1h)
    results["trend_breakout_v2"] = engine.compute_stats(
        trend_trades, "Trend Breakout v2 (1h)", "BTCUSDT", "1h",
        f"{candles_1h[0]['datetime'][:10]} to {candles_1h[-1]['datetime'][:10]}"
    )
    print(f"    {len(trend_trades)} trades | PnL: ${results['trend_breakout_v2'].total_pnl:.2f}")

    # Strategy 2a: Mean Reversion v2 — 15m with band-close confirmation
    print("\\n  Running Mean Reversion v2 (15m) [band-close confirm + reward 3.0]...")
    scalp_15m = MeanReversionV2(equity=equity, is_5m=False)
    scalp_15m_trades = scalp_15m.run(candles_5m)  # uses 5m candles as proxy for 15m
    results["mean_reversion_15m_v2"] = engine.compute_stats(
        scalp_15m_trades, "Mean Reversion v2 (15m)", "BTCUSDT", "15m",
        f"{candles_5m[0]['datetime'][:10]} to {candles_5m[-1]['datetime'][:10]}"
    )
    print(f"    {len(scalp_15m_trades)} trades | PnL: ${results['mean_reversion_15m_v2'].total_pnl:.2f}")

    # Strategy 2b: Mean Reversion v2 — 5m secondary timeframe
    print("\\n  Running Mean Reversion v2 (5m) [secondary timeframe, looser RSI]...")
    scalp_5m = MeanReversionV2(equity=equity, is_5m=True)
    scalp_5m_trades = scalp_5m.run(candles_5m)
    results["mean_reversion_5m_v2"] = engine.compute_stats(
        scalp_5m_trades, "Mean Reversion v2 (5m)", "BTCUSDT", "5m",
        f"{candles_5m[0]['datetime'][:10]} to {candles_5m[-1]['datetime'][:10]}"
    )
    print(f"    {len(scalp_5m_trades)} trades | PnL: ${results['mean_reversion_5m_v2'].total_pnl:.2f}")

    # Strategy 3: Candle3 v2 (volume confirmation + min hold)
    print("\\n  Running Candle3 v2 (3m) [escalating volume + 1.5x avg + 3-bar min hold]...")
    candle3 = Candle3V2(equity=equity)
    candle3_trades = candle3.run(candles_3m)
    results["candle3_v2"] = engine.compute_stats(
        candle3_trades, "Candle3 v2 (3m)", "BTCUSDT", "3m",
        f"{candles_3m[0]['datetime'][:10]} to {candles_3m[-1]['datetime'][:10]}"
    )
    print(f"    {len(candle3_trades)} trades | PnL: ${results['candle3_v2'].total_pnl:.2f}")

    return results


def run_bot1_v1(candles_1h, candles_5m, candles_3m, equity: float) -> dict:
    """Run baseline Bot1 v1 strategies (for comparison)."""
    print("\\n  [BASELINE v1 comparison]")
    engine = BacktestEngine(starting_equity=equity)
    results = {}

    trend = TrendBreakoutV1(equity=equity)
    results["trend_breakout_v1"] = engine.compute_stats(
        trend.run(candles_1h), "Trend Breakout v1 (1h)", "BTCUSDT", "1h", "")

    scalp = MeanReversionV1(equity=equity)
    results["mean_reversion_v1"] = engine.compute_stats(
        scalp.run(candles_5m), "Mean Reversion v1 (5m)", "BTCUSDT", "5m", "")

    candle3 = Candle3V1(equity=equity)
    results["candle3_v1"] = engine.compute_stats(
        candle3.run(candles_3m), "Candle3 v1 (3m)", "BTCUSDT", "3m", "")

    return results


def run_bot2_v2(candles_1m, equity: float) -> dict:
    """Run Bot2 v2 volume generation strategy."""
    print("\\n" + "="*60)
    print("BOT 2: VOLUME GENERATION BOT v2 (Spread 0.12%, Stop 0.01%, ATR Pause)")
    print("="*60)

    engine = BacktestEngine(starting_equity=equity, maker_fee=0.0002, taker_fee=0.00055)

    print("\\n  Running Ping-Pong Volume Gen v2 (1m)...")
    volgen = VolumeGenV2(equity=equity)
    volgen_trades = volgen.run(candles_1m)
    result = engine.compute_stats(
        volgen_trades, "Ping-Pong Volume Gen v2 (1m)", "BTCUSDT", "1m",
        f"{candles_1m[0]['datetime'][:10]} to {candles_1m[-1]['datetime'][:10]}"
    )
    print(f"    {len(volgen_trades)} trades | PnL: ${result.total_pnl:.2f}")

    print("\\n  [BASELINE v1 comparison]")
    volgen_v1 = VolumeGenV1(equity=equity)
    result_v1 = engine.compute_stats(
        volgen_v1.run(candles_1m), "Ping-Pong Volume Gen v1 (1m)", "BTCUSDT", "1m", "")
    print(f"    v1: {result_v1.total_trades} trades | PnL: ${result_v1.total_pnl:.2f}")

    return {"volume_gen_v2": result, "volume_gen_v1": result_v1}


def format_result(r: BacktestResult, tag: str = "") -> str:
    label = f"  [{tag}] {r.strategy_name}" if tag else f"  {r.strategy_name}"
    lines = [
        label,
        f"    Trades: {r.total_trades} | W/L: {r.winning_trades}W/{r.losing_trades}L | Win Rate: {r.win_rate:.1f}%",
        f"    Net PnL: ${r.total_pnl:+.2f} | Fees: ${r.total_fees:.2f} | Return: {r.total_return_pct:+.2f}%",
        f"    Profit Factor: {r.profit_factor:.2f} | Max DD: {r.max_drawdown_pct:.1f}% | Sharpe: {r.sharpe_ratio:.2f}",
        f"    Volume: ${r.total_volume:,.0f}",
    ]
    return "\\n".join(lines)


def generate_report(bot1_v1, bot1_v2, bot2_results, symbol, days, equity) -> str:
    report = []
    report.append("=" * 70)
    report.append("PANTHER BACKTEST REPORT — v2 STRATEGY IMPROVEMENTS")
    report.append(f"Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    report.append(f"Symbol: {symbol} | Days: {days} | Starting Equity: ${equity:,.2f}")
    report.append("=" * 70)

    # Bot 2 (VolumeGen) — Priority 1 fix
    report.append("\\n" + "-"*70)
    report.append("PRIORITY 1: VOLUMEGEN PING-PONG (spread 0.05%->0.12%, stop 0.10%->0.01%, ATR pause)")
    report.append("-"*70)
    report.append(format_result(bot2_results["volume_gen_v1"], "BEFORE"))
    report.append(format_result(bot2_results["volume_gen_v2"], "AFTER "))
    v1_pnl = bot2_results["volume_gen_v1"].total_pnl
    v2_pnl = bot2_results["volume_gen_v2"].total_pnl
    delta = v2_pnl - v1_pnl
    report.append(f"\\n  >>> PnL Delta: ${delta:+.2f} ({'IMPROVEMENT' if delta > 0 else 'REGRESSION'})")

    # Candle3 — Priority 1b fix on Bot1
    report.append("\\n" + "-"*70)
    report.append("PRIORITY 1b: CANDLE3 (escalating volume + 1.5x avg + 3-bar min hold)")
    report.append("-"*70)
    report.append(format_result(bot1_v1["candle3_v1"], "BEFORE"))
    report.append(format_result(bot1_v2["candle3_v2"], "AFTER "))
    delta_c3 = bot1_v2["candle3_v2"].total_pnl - bot1_v1["candle3_v1"].total_pnl
    report.append(f"\\n  >>> PnL Delta: ${delta_c3:+.2f} ({'IMPROVEMENT' if delta_c3 > 0 else 'REGRESSION'})")

    # Mean Reversion — Priority 2
    report.append("\\n" + "-"*70)
    report.append("PRIORITY 2: MEAN REVERSION (15m + 5m dual timeframe, reward 3x, band-close confirm)")
    report.append("-"*70)
    report.append(format_result(bot1_v1["mean_reversion_v1"], "BEFORE (5m)"))
    report.append(format_result(bot1_v2["mean_reversion_15m_v2"], "AFTER 15m"))
    report.append(format_result(bot1_v2["mean_reversion_5m_v2"], "AFTER  5m"))
    pnl_before = bot1_v1["mean_reversion_v1"].total_pnl
    pnl_after = bot1_v2["mean_reversion_15m_v2"].total_pnl + bot1_v2["mean_reversion_5m_v2"].total_pnl
    delta_mr = pnl_after - pnl_before
    report.append(f"\\n  >>> Combined PnL Delta: ${delta_mr:+.2f} ({'IMPROVEMENT' if delta_mr > 0 else 'REGRESSION'})")
    trades_before = bot1_v1["mean_reversion_v1"].total_trades
    trades_after = bot1_v2["mean_reversion_15m_v2"].total_trades + bot1_v2["mean_reversion_5m_v2"].total_trades
    report.append(f"  >>> Trade Count: {trades_before} -> {trades_after} ({trades_after - trades_before:+d})")

    # Trend Breakout — Priority 3
    report.append("\\n" + "-"*70)
    report.append("PRIORITY 3: TREND BREAKOUT (10-bar lookback + 24h fallback mode)")
    report.append("-"*70)
    report.append(format_result(bot1_v1["trend_breakout_v1"], "BEFORE"))
    report.append(format_result(bot1_v2["trend_breakout_v2"], "AFTER "))
    delta_tb = bot1_v2["trend_breakout_v2"].total_pnl - bot1_v1["trend_breakout_v1"].total_pnl
    report.append(f"\\n  >>> PnL Delta: ${delta_tb:+.2f} ({'IMPROVEMENT' if delta_tb > 0 else 'REGRESSION'})")

    # Summary
    report.append("\\n" + "="*70)
    report.append("OVERALL SUMMARY")
    report.append("="*70)

    v1_total = (bot1_v1["trend_breakout_v1"].total_pnl +
                bot1_v1["mean_reversion_v1"].total_pnl +
                bot1_v1["candle3_v1"].total_pnl +
                bot2_results["volume_gen_v1"].total_pnl)

    v2_total = (bot1_v2["trend_breakout_v2"].total_pnl +
                bot1_v2["mean_reversion_15m_v2"].total_pnl +
                bot1_v2["mean_reversion_5m_v2"].total_pnl +
                bot1_v2["candle3_v2"].total_pnl +
                bot2_results["volume_gen_v2"].total_pnl)

    report.append(f"  v1 Combined PnL:  ${v1_total:+.2f}")
    report.append(f"  v2 Combined PnL:  ${v2_total:+.2f}")
    report.append(f"  Net Improvement:  ${v2_total - v1_total:+.2f}")
    report.append(f"  Return on ${equity:.0f}: {v2_total/equity*100:+.2f}%")

    report.append("\\n" + "="*70)
    report.append("NEXT STEPS")
    report.append("="*70)
    report.append("  1. If VolumeGen v2 PnL positive -> deploy with $50-100 real capital, Bybit testnet first")
    report.append("  2. If Candle3 v2 win rate > 60% -> integrate volume filter into live bot2_strategy.py")
    report.append("  3. MeanReversion dual mode: run 30-day test to confirm trade count doubles")
    report.append("  4. Consider ATR pause threshold tuning: test 0.05%, 0.08%, 0.12% cutoffs")
    report.append("  5. Run: python run_backtest_v2.py --days 30 for full-sample validation")
    report.append("\\n" + "="*70)

    return "\\n".join(report)


def main():
    parser = argparse.ArgumentParser(description="Panther Backtest v2 — Strategy Improvement Comparison")
    parser.add_argument("--days", type=int, default=30, help="Days of historical data (default: 30)")
    parser.add_argument("--symbol", type=str, default="BTCUSDT")
    parser.add_argument("--equity", type=float, default=500.0)
    parser.add_argument("--no-download", action="store_true")
    parser.add_argument("--output", type=str, default="results_v2")
    args = parser.parse_args()

    data_dir = os.path.join(args.output, "data")
    os.makedirs(data_dir, exist_ok=True)
    os.makedirs(args.output, exist_ok=True)

    symbol = args.symbol
    days = args.days
    equity = args.equity

    print(f"\\n{'='*60}")
    print(f"PANTHER BACKTEST v2 FRAMEWORK")
    print(f"Symbol: {symbol} | Days: {days} | Equity: ${equity:,.2f}")
    print(f"{'='*60}")

    # Fetch / load data
    intervals = {
        "1m": f"{data_dir}/{symbol.lower()}_1m.json",
        "3m": f"{data_dir}/{symbol.lower()}_3m.json",
        "5m": f"{data_dir}/{symbol.lower()}_5m.json",
        "1h": f"{data_dir}/{symbol.lower()}_1h.json",
    }

    candle_data = {}
    for interval, filepath in intervals.items():
        if args.no_download and os.path.exists(filepath):
            print(f"\\nLoading cached {interval} data...")
            candle_data[interval] = load_candles(filepath)
        else:
            print(f"\\nFetching {interval} data from Bybit...")
            candles = fetch_full_history(symbol, interval, days=days)
            save_candles(candles, filepath)
            candle_data[interval] = candles

    for interval, candles in candle_data.items():
        if len(candles) < 50:
            print(f"\\n[WARNING] Only {len(candles)} {interval} candles.")

    # Run backtests
    bot1_v1 = run_bot1_v1(candle_data["1h"], candle_data["5m"], candle_data["3m"], equity)
    bot1_v2 = run_bot1_v2(candle_data["1h"], candle_data["5m"], candle_data["3m"], equity)
    bot2_results = run_bot2_v2(candle_data["1m"], equity)

    report = generate_report(bot1_v1, bot1_v2, bot2_results, symbol, days, equity)

    report_path = os.path.join(args.output, "backtest_report_v2.txt")
    with open(report_path, "w") as f:
        f.write(report)

    print(f"\\n\\nReport saved: {report_path}")
    print("\\n" + report)

    # Save all trade logs
    all_results = {**bot1_v1, **bot1_v2, **bot2_results}
    all_trades = []
    for key, r in all_results.items():
        for t in r.trades:
            all_trades.append({
                "version": "v2" if "v2" in key else "v1",
                "strategy": t.strategy,
                "side": t.side,
                "entry_price": t.entry_price,
                "exit_price": t.exit_price,
                "size": t.size,
                "entry_time": t.entry_time,
                "exit_time": t.exit_time,
                "pnl": t.pnl,
                "fees": t.fees,
                "notional": t.notional,
                "reason": t.reason,
                "exit_reason": t.exit_reason,
            })

    trade_log_path = os.path.join(args.output, "trade_log_v2.json")
    with open(trade_log_path, "w") as f:
        json.dump(all_trades, f, indent=2)
    print(f"\\nTrade log saved: {trade_log_path} ({len(all_trades)} trades)")

    print(f"\\n{'='*60}")
    print(f"DONE! All results in ./{args.output}/")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
