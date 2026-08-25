from __future__ import annotations

import argparse
import math
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
SCRIPTS = ROOT / "scripts"
for path in (SRC, SCRIPTS):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from backtest.config import DEFAULT_WORKBOOK, normalize_underlying
from backtest.replay import CsvReplayFeed, register_token_tickers
from backtest.sessions import build_backtest_sessions
from export_1s_batch_restrike_workbooks import (
    SegmentInfo,
    in_time_window,
    portfolio_liquidation_premiums,
    portfolio_premiums,
    synthetic_trade_premiums,
)
from export_sample_hf_diagnostics import close_to_close_variance, scaled_volatility_or_none
from export_single_restrike_cheapest_synth_diagnostics import (
    best_market_synthetic_strike,
    closest_um_synthetic_strike,
)
from fit_sensex.pricing.black_scholes import implied_volatility
from light_run import load_breeze_parquet_option_dataset
from run_nifty_1m_light_batch import complete_strikes
from backtest_sample_hf.__main__ import load_sample_hf_option_dataset
from run_sample_hf_pnl_only import (
    DirectPriceBook,
    build_light_sample_portfolio,
    capture_light_frozen_ivs,
    direct_portfolio_values,
    hedge_multiplier,
)


DEFAULT_BREEZE_DIR = Path(r"C:\Users\rishi\my_project\historical\breeze 1s")
DEFAULT_SAMPLE_HF_DIR = Path(r"C:\options data\1s data")
TRADING_SECONDS = 21_300
DEFAULT_RV_WINDOW_SECONDS = 300


@dataclass
class SwitchingHedgeState:
    portfolio: object
    options_pv_snapshot: float | None
    hedge_strike: int
    candidate_strikes: list[int]
    hedge_trades: list[dict] = field(default_factory=list)
    cumulative_hedge_lots: float = 0.0
    last_options_pv: float | None = None
    last_delta_lots: float | None = None
    last_gamma_lots: float | None = None
    last_total_pnl: float | None = None

    def update(self, price_book: DirectPriceBook, row: pd.Series, snapshot, timestamp, threshold_lots: float) -> float | None:
        values = direct_portfolio_values(self.portfolio.positions, price_book, row, snapshot, {})
        if values is None:
            return None
        options_pv, delta_lots, gamma_lots = values
        self.last_options_pv = options_pv
        self.last_delta_lots = delta_lots
        self.last_gamma_lots = gamma_lots
        if self.options_pv_snapshot is None:
            self.options_pv_snapshot = options_pv
        combined_delta = delta_lots + self.cumulative_hedge_lots
        if abs(combined_delta) > threshold_lots:
            self.add_hedge_trade(round(-combined_delta), price_book, row, snapshot, timestamp)
        hedge_pnl = self.hedge_pnl(price_book, row, snapshot)
        self.last_total_pnl = (options_pv - self.options_pv_snapshot) + (hedge_pnl or 0.0)
        return self.last_total_pnl

    def add_hedge_trade(self, lots_change: float, price_book: DirectPriceBook, row: pd.Series, snapshot, timestamp) -> None:
        if abs(lots_change) < 1e-9:
            return
        hedge_strike = best_market_synthetic_strike(price_book, row, snapshot, self.candidate_strikes, lots_change)
        prices = price_book.hedge_prices(row, snapshot, hedge_strike)
        if prices is None:
            return
        synth_bid, synth_ask, _ = prices
        self.hedge_trades.append(
            {
                "timestamp": timestamp,
                "lots_change": lots_change,
                "trade_price": synth_ask if lots_change > 0 else synth_bid,
                "hedge_strike": hedge_strike,
            }
        )
        self.cumulative_hedge_lots += lots_change
        self.hedge_strike = hedge_strike

    def hedge_pnl(self, price_book: DirectPriceBook, row: pd.Series, snapshot) -> float | None:
        if not self.hedge_trades:
            return None
        multiplier = hedge_multiplier(self.portfolio.positions)
        total = 0.0
        for trade in self.hedge_trades:
            prices = price_book.hedge_prices(row, snapshot, int(trade["hedge_strike"]))
            if prices is None:
                return None
            _, _, synth_mid = prices
            total += float(trade["lots_change"]) * (synth_mid - float(trade["trade_price"])) * multiplier / 1000
        return total


def parse_date(value: str) -> date:
    for fmt in ("%d%m%Y", "%Y%m%d", "%d%b%y", "%d-%b-%y", "%Y-%m-%d"):
        try:
            return pd.to_datetime(value, format=fmt).date()
        except ValueError:
            continue
    return pd.to_datetime(value).date()


def find_breeze_file(folder: Path, trade_date: date, underlying: str) -> Path:
    candidates = sorted(folder.glob(f"breeze_download_{trade_date:%Y-%m-%d}_atm_*_1s.parquet"))
    candidates.extend(sorted(folder.glob(f"breeze_download_{trade_date:%Y-%m-%d}_{underlying.lower()}_atm_*_1s.parquet")))
    for path in candidates:
        try:
            meta = pd.read_parquet(path, columns=["trade_date", "expiry_date", "underlying", "product_type"])
        except Exception:
            continue
        meta = meta[meta["product_type"].astype(str).str.lower().eq("options")]
        if (
            meta["underlying"].astype(str).str.upper().eq(underlying).any()
            and pd.to_datetime(meta["trade_date"], errors="coerce").dt.date.eq(trade_date).any()
            and pd.to_datetime(meta["expiry_date"], errors="coerce").dt.date.eq(trade_date).any()
        ):
            return path
    raise FileNotFoundError(f"No {underlying} 0DTE Breeze parquet found for {trade_date:%Y-%m-%d} in {folder}.")


def live_portfolio_iv(positions, price_book: DirectPriceBook, row: pd.Series, snapshot) -> float | None:
    vols = []
    for position in positions:
        price = price_book.option_price(row, position.strike, position.option_type)
        if price is None:
            continue
        try:
            vol = implied_volatility(
                price,
                snapshot.universal_spot,
                position.strike,
                snapshot.time,
                price_book.market.funding_rate,
                position.option_type,
            )
        except (ValueError, ZeroDivisionError, OverflowError):
            vol = None
        if vol is not None and math.isfinite(vol):
            vols.append(vol)
    return sum(vols) / len(vols) if vols else None


def rv5_annualized(points: deque, calendar_days: float, intraday_var: float) -> float | None:
    if len(points) < 2 or intraday_var <= 0:
        return None
    variance = 0.0
    returns = 0
    prev = None
    for _, universal_mid in points:
        if prev is not None and prev > 0 and universal_mid > 0:
            variance += math.log(universal_mid / prev) ** 2
            returns += 1
        prev = universal_mid
    if returns <= 0:
        return None
    return math.sqrt((variance / returns) * TRADING_SECONDS * calendar_days / intraday_var)


def dynamic_threshold_lots(gamma_lots: float | None, floor_lots: float) -> float:
    if gamma_lots is None or not math.isfinite(gamma_lots):
        return floor_lots
    return max(abs(0.10 * gamma_lots), floor_lots)


def make_segment(session, price_book: DirectPriceBook, row: pd.Series, timestamp, user_value: int) -> SegmentInfo | None:
    snapshot = price_book.market_snapshot(row, timestamp, user_value)
    if snapshot is None:
        return None
    strikes = complete_strikes(price_book, row)
    portfolio = build_light_sample_portfolio(session, snapshot, strikes)
    if portfolio is None:
        return None
    frozen_ivs = capture_light_frozen_ivs(portfolio.positions, price_book, row, snapshot)
    if len(frozen_ivs) != len(portfolio.positions):
        return None
    hedge_strike = closest_um_synthetic_strike(price_book, row, strikes, snapshot.universal_mid)
    short_calls = [p.strike for p in portfolio.positions if p.option_type == "CE" and float(p.lots) < 0]
    short_puts = [p.strike for p in portfolio.positions if p.option_type == "PE" and float(p.lots) < 0]
    return SegmentInfo(
        state=SwitchingHedgeState(
            portfolio=portfolio,
            options_pv_snapshot=None,
            hedge_strike=hedge_strike,
            candidate_strikes=strikes,
        ),
        short_call=max(short_calls) if short_calls else None,
        short_put=min(short_puts) if short_puts else None,
        hedge_strike=hedge_strike,
        user_value=snapshot.user_value,
        universal_mid=snapshot.universal_mid,
    )


def run_one(
    path: Path,
    trade_date: date,
    underlying: str,
    start_time: str,
    end_time: str,
    fixed_floor: float | None = None,
    source: str = "breeze",
    iv_average_mode: str = "rolling5",
    rv_window_seconds: int = DEFAULT_RV_WINDOW_SECONDS,
    iv_window_seconds: int | None = None,
    iv_trigger_offset_vol_points: float = 0.0,
) -> dict[str, object]:
    total_start = time.perf_counter()
    if source == "sample-hf":
        dataset = load_sample_hf_option_dataset(path, trade_date, underlying)
    else:
        dataset = load_breeze_parquet_option_dataset(path, trade_date, underlying, start_time)
    load_seconds = time.perf_counter() - total_start
    replay = None
    current_time = lambda: replay.now()
    sessions = build_backtest_sessions(dataset, DEFAULT_WORKBOOK, current_time, refresh_ms=0)
    replay = CsvReplayFeed(dataset, sessions)
    register_token_tickers(sessions, dataset)
    session = sessions[0]
    surface = dataset.option_surface(session.spec.expiry)
    price_book = DirectPriceBook(dataset, surface, session)

    state_info = None
    waiting_reentry = False
    reentry_done = False
    user_value = session.config.market.user_value
    realized_offset = 0.0
    breach = None
    prev_um = None
    prev_hedge_um = None
    c2c_var = 0.0
    hedge_var = 0.0
    hedge_events = 0
    synth_lots = 0.0
    premium_bought = 0.0
    premium_sold = 0.0
    multiplier = None
    um_window = deque()
    iv_window = deque()
    iv_sum = 0.0
    iv_count = 0
    floor_counts = {1.2: 0, 2.2: 0}
    signal_ready = 0
    last_pnl = None
    last_rv5 = None
    last_iv5ma = None
    rows = 0
    sim_start = time.perf_counter()

    while replay.advance():
        timestamp = replay.now()
        if not in_time_window(timestamp, start_time, end_time):
            continue
        row = surface.loc[dataset.timestamps[replay.position]]

        if waiting_reentry:
            state_info = make_segment(session, price_book, row, timestamp, user_value)
            if state_info is None:
                continue
            multiplier = hedge_multiplier(state_info.state.portfolio.positions)
            bought, sold = portfolio_premiums(state_info.state.portfolio.positions, price_book, row, multiplier)
            premium_bought += bought
            premium_sold += sold
            user_value = state_info.user_value
            waiting_reentry = False

        if state_info is None:
            state_info = make_segment(session, price_book, row, timestamp, user_value)
            if state_info is None:
                continue
            multiplier = hedge_multiplier(state_info.state.portfolio.positions)
            bought, sold = portfolio_premiums(state_info.state.portfolio.positions, price_book, row, multiplier)
            premium_bought += bought
            premium_sold += sold
            user_value = state_info.user_value

        snapshot = price_book.market_snapshot(row, timestamp, user_value)
        if snapshot is None:
            continue
        user_value = snapshot.user_value

        um_window.append((timestamp, snapshot.universal_mid))
        while um_window and (timestamp - um_window[0][0]).total_seconds() > rv_window_seconds:
            um_window.popleft()
        iv_now = live_portfolio_iv(state_info.state.portfolio.positions, price_book, row, snapshot)
        if iv_now is not None:
            iv_window.append((timestamp, iv_now))
            iv_sum += iv_now
            iv_count += 1
        active_iv_window_seconds = iv_window_seconds if iv_window_seconds is not None else rv_window_seconds
        while iv_window and (timestamp - iv_window[0][0]).total_seconds() > active_iv_window_seconds:
            iv_window.popleft()

        rv5 = rv5_annualized(um_window, price_book.market.calendar_days, price_book.market.intraday_var)
        iv5ma = (
            iv_sum / iv_count
            if iv_average_mode == "expanding"
            else sum(iv for _, iv in iv_window) / len(iv_window) if iv_window else None
        )
        full_window = bool(um_window) and (timestamp - um_window[0][0]).total_seconds() >= rv_window_seconds - 1
        if fixed_floor is not None:
            floor_lots = fixed_floor
        else:
            iv_trigger_level = iv5ma - (iv_trigger_offset_vol_points / 100.0) if iv5ma is not None else None
            floor_lots = (
                1.2
                if full_window
                and rv5 is not None
                and iv_trigger_level is not None
                and rv5 > iv_trigger_level
                else 2.2
            )
        floor_counts[floor_lots] = floor_counts.get(floor_lots, 0) + 1
        if full_window and rv5 is not None and iv5ma is not None:
            signal_ready += 1

        values = direct_portfolio_values(state_info.state.portfolio.positions, price_book, row, snapshot, {})
        if values is None:
            continue
        _, _, gamma_lots = values
        threshold_lots = dynamic_threshold_lots(gamma_lots, floor_lots)
        prior_trade_count = len(state_info.state.hedge_trades)
        segment_pnl = state_info.state.update(price_book, row, snapshot, timestamp, threshold_lots)
        if segment_pnl is None:
            continue
        new_trades = state_info.state.hedge_trades[prior_trade_count:]
        for trade in new_trades:
            lots_change = float(trade["lots_change"])
            synth_lots += abs(lots_change)
            bought, sold = synthetic_trade_premiums(
                price_book,
                row,
                int(trade.get("hedge_strike", state_info.hedge_strike)),
                lots_change,
                multiplier,
            )
            premium_bought += bought
            premium_sold += sold
        hedge_events += len(new_trades)
        running_pnl = realized_offset + segment_pnl
        if prev_um is not None:
            c2c_var += close_to_close_variance(prev_um, snapshot.universal_mid)
        prev_um = snapshot.universal_mid
        if new_trades:
            if prev_hedge_um is not None:
                hedge_var += close_to_close_variance(prev_hedge_um, snapshot.universal_mid)
            prev_hedge_um = snapshot.universal_mid
        rows += 1
        last_pnl = running_pnl
        last_rv5 = rv5
        last_iv5ma = iv5ma

        if not reentry_done:
            breach_side = None
            breach_strike = None
            if state_info.short_call is not None and snapshot.universal_mid > state_info.short_call:
                breach_side = "CALL"
                breach_strike = state_info.short_call
            elif state_info.short_put is not None and snapshot.universal_mid < state_info.short_put:
                breach_side = "PUT"
                breach_strike = state_info.short_put
            if breach_side:
                bought, sold = portfolio_liquidation_premiums(
                    state_info.state.portfolio.positions,
                    price_book,
                    row,
                    multiplier,
                )
                premium_bought += bought
                premium_sold += sold
                breach = {
                    "timestamp": timestamp,
                    "side": breach_side,
                    "strike": breach_strike,
                    "pnl": running_pnl,
                }
                realized_offset = running_pnl
                state_info = None
                waiting_reentry = True
                reentry_done = True

    return {
        "date": trade_date.strftime("%d-%b-%y"),
        "pnl": last_pnl,
        "c2c_vol": scaled_volatility_or_none(c2c_var, session.config.market.calendar_days, session.config.market.intraday_var),
        "hedge_vol": scaled_volatility_or_none(hedge_var, session.config.market.calendar_days, session.config.market.intraday_var),
        "hedge_events": hedge_events,
        "synth_lots": synth_lots,
        "premium_bought": premium_bought,
        "premium_sold": premium_sold,
        "floor_1p2_seconds": floor_counts[1.2],
        "floor_2p2_seconds": floor_counts[2.2],
        "signal_ready_seconds": signal_ready,
        "last_rv5": last_rv5 * 100 if last_rv5 is not None else None,
        "last_iv5ma": last_iv5ma * 100 if last_iv5ma is not None else None,
        "breach_time": breach["timestamp"].strftime("%H:%M:%S") if breach else "",
        "pnl_at_breach": breach["pnl"] if breach else None,
        "rows": rows,
        "load_seconds": load_seconds,
        "simulation_seconds": time.perf_counter() - sim_start,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--underlying", required=True, type=normalize_underlying, choices=("NIFTY", "SENSEX"))
    parser.add_argument("--dates", nargs="+", required=True)
    parser.add_argument("--breeze-dir", type=Path, default=DEFAULT_BREEZE_DIR)
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_SAMPLE_HF_DIR)
    parser.add_argument("--source", choices=("breeze", "sample-hf"), default="breeze")
    parser.add_argument("--start-time", default="09:20")
    parser.add_argument("--end-time", default="15:15")
    parser.add_argument("--fixed-floor", type=positive_float, default=None)
    parser.add_argument(
        "--iv-average-mode",
        choices=("rolling5", "expanding"),
        default="rolling5",
        help="Compare RV5 to either rolling 5-minute IV average or day-to-date expanding IV average.",
    )
    parser.add_argument(
        "--rv-window-minutes",
        type=float,
        default=5.0,
        help="Trailing realised-vol window in minutes.",
    )
    parser.add_argument(
        "--iv-window-minutes",
        type=float,
        default=None,
        help="Trailing IV moving-average window in minutes. Defaults to the RV window.",
    )
    parser.add_argument(
        "--iv-trigger-offset-vol-points",
        type=float,
        default=0.0,
        help="Subtract this many vol points from the IV average before comparing to RV.",
    )
    args = parser.parse_args()

    rows = []
    for raw_date in args.dates:
        trade_date = parse_date(raw_date)
        path = (
            args.data_dir
            if args.source == "sample-hf"
            else find_breeze_file(args.breeze_dir, trade_date, args.underlying)
        )
        rows.append(
            run_one(
                path,
                trade_date,
                args.underlying,
                args.start_time,
                args.end_time,
                args.fixed_floor,
                args.source,
                args.iv_average_mode,
                max(1, round(args.rv_window_minutes * 60)),
                None if args.iv_window_minutes is None else max(1, round(args.iv_window_minutes * 60)),
                args.iv_trigger_offset_vol_points,
            )
        )

    columns = [
        "date",
        "pnl",
        "c2c_vol",
        "hedge_vol",
        "hedge_events",
        "synth_lots",
        "premium_bought",
        "premium_sold",
        "floor_1p2_seconds",
        "floor_2p2_seconds",
        "signal_ready_seconds",
        "last_rv5",
        "last_iv5ma",
        "breach_time",
        "pnl_at_breach",
        "rows",
        "load_seconds",
        "simulation_seconds",
    ]
    print(",".join(columns))
    for row in rows:
        print(",".join(format_value(row.get(column)) for column in columns))
    pnls = [float(row["pnl"]) for row in rows if row.get("pnl") is not None]
    if pnls:
        print(f"TOTAL,{sum(pnls):.4f}")
        print(f"AVERAGE,{sum(pnls) / len(pnls):.4f}")


def format_value(value: object) -> str:
    if isinstance(value, float):
        return f"{value:.4f}"
    return "" if value is None else str(value)


def positive_float(value: str) -> float:
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be positive")
    return parsed


if __name__ == "__main__":
    main()
