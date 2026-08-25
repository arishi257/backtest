from __future__ import annotations

import argparse
import csv
import math
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from backtest.config import DEFAULT_WORKBOOK, normalize_underlying
from backtest.replay import CsvReplayFeed, register_token_tickers
from backtest.sessions import build_backtest_sessions
from backtest_1m.__main__ import (
    DynamicGammaThresholdFrozenIvState,
    DynamicGammaThresholdPortfolioState,
    dynamic_gamma_threshold,
)
from backtest.headless_portfolio import hedge_multiplier, nearest_result_strike
from backtest.portfolio import (
    HEDGE_DISTANCE,
    LONG_LOTS,
    SENSEX_LONG_LOTS,
    SENSEX_SHORT_COUNT,
    SENSEX_SHORT_LOTS,
    SENSEX_WING_DISTANCE,
    SHORT_LOTS,
    SamplePortfolioRisk,
    StaticPortfolioRiskEngine,
    build_position,
    nearest_downside_strikes,
    nearest_strike,
    nearest_upside_strikes,
)
from backtest_sample_hf.__main__ import (
    DEFAULT_DATA_DIR,
    cache_file_path,
    load_sample_hf_option_dataset,
    parse_date_key,
)
from fit_sensex.pricing.black_scholes import (
    black_scholes_delta,
    black_scholes_gamma,
    black_scholes_price,
    implied_volatility,
)
from fit_sensex.services.risk import synthetic_prices


NODE_EXE = Path(
    r"C:\Users\rishi\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin\node.exe"
)
NODE_MODULES = Path(
    r"C:\Users\rishi\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\node_modules"
)
PNL_START_TIME = "09:20"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run a fast 1-second PnL-only backtest and write a small workbook."
    )
    parser.add_argument("--date", required=True)
    parser.add_argument(
        "--underlying",
        required=True,
        type=normalize_underlying,
        choices=("NIFTY", "SENSEX"),
    )
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument("--workbook", type=Path, default=DEFAULT_WORKBOOK)
    parser.add_argument("--dynamic-gamma-threshold-ratio", type=float, default=0.40)
    parser.add_argument("--run-dir", type=Path, default=None)
    parser.add_argument(
        "--full-analytics",
        action="store_true",
        help="Use the older full-surface analytics path instead of the fast direct pricer.",
    )
    args = parser.parse_args()

    trade_date = parse_date_key(args.date)
    run_dir = args.run_dir or default_run_dir(args.underlying, trade_date)
    run_dir.mkdir(parents=True, exist_ok=True)

    total_start = time.perf_counter()
    cache_path = cache_file_path(args.data_dir, trade_date, args.underlying)
    cache_was_present = cache_path.exists()

    data_start = time.perf_counter()
    dataset = load_sample_hf_option_dataset(args.data_dir, trade_date, args.underlying)
    data_seconds = time.perf_counter() - data_start

    sim_start = time.perf_counter()
    if args.full_analytics:
        rows, final_metrics, cycles, analytics = run_pnl_only(
            dataset,
            args.workbook,
            args.dynamic_gamma_threshold_ratio,
        )
        pricer_mode = "full_analytics"
    else:
        rows, final_metrics, cycles, analytics = run_direct_pnl_only(
            dataset,
            args.workbook,
            args.dynamic_gamma_threshold_ratio,
        )
        pricer_mode = "direct_selected_strikes"
    simulation_seconds = time.perf_counter() - sim_start
    if not rows:
        raise SystemExit("No PnL rows were produced.")

    pnl_csv = run_dir / "pnl_timeseries.csv"
    summary_csv = run_dir / "summary_metrics.csv"
    write_pnl_csv(rows, pnl_csv)

    workbook_start = time.perf_counter()
    # Write summary after simulation but before workbook creation; workbook_seconds is patched after build.
    summary_metrics = {
        **final_metrics,
        "universal_mid_start": rows[0]["universal_mid"],
        "data_load_seconds": data_seconds,
        "simulation_seconds": simulation_seconds,
        "workbook_seconds": 0.0,
        "total_seconds": 0.0,
        "pricer_mode": pricer_mode,
    }
    write_summary_csv(summary_metrics, summary_csv)
    workbook_path = build_workbook(run_dir, args.underlying, trade_date)
    workbook_seconds = time.perf_counter() - workbook_start

    total_seconds = time.perf_counter() - total_start
    summary_metrics["workbook_seconds"] = workbook_seconds
    summary_metrics["total_seconds"] = total_seconds
    write_summary_csv(summary_metrics, summary_csv)

    print(f"Completed {cycles} 1-second replay cycles. Analytics rows: {analytics}.")
    print(f"Raw parsed-data cache: {cache_path}")
    print(f"Cache used at start: {'yes' if cache_was_present else 'no; created during this run'}")
    print(f"Run dir: {run_dir}")
    print(f"Workbook: {workbook_path}")
    print(f"Pricer mode: {pricer_mode}")
    print("Final PnL:")
    print(f"  portfolio_total_pnl: {format_number(final_metrics.get('portfolio_total_pnl'))}")
    print(f"  frozen_iv_total_pnl: {format_number(final_metrics.get('frozen_iv_total_pnl'))}")
    print("Timing:")
    print(f"  data_load_seconds: {data_seconds:.2f}")
    print(f"  simulation_seconds: {simulation_seconds:.2f}")
    print(f"  workbook_seconds: {workbook_seconds:.2f}")
    print(f"  total_seconds: {total_seconds:.2f}")


def run_pnl_only(dataset, workbook: Path, threshold_ratio: float):
    replay = None
    current_time = lambda: replay.now()
    sessions = build_backtest_sessions(dataset, workbook, current_time, refresh_ms=0)
    replay = CsvReplayFeed(dataset, sessions)
    register_token_tickers(sessions, dataset)

    states = {
        session.spec.tab_name: DynamicGammaThresholdPortfolioState(session=session)
        for session in sessions
    }
    frozen_states = {
        session.spec.tab_name: DynamicGammaThresholdFrozenIvState(session)
        for session in sessions
    }

    rows: list[dict[str, object]] = []
    final_metrics: dict[str, float | None] = {}
    cycles = 0
    analytics = 0
    while replay.advance():
        cycles += 1
        timestamp = replay.now()
        for session in sessions:
            result = session.analytics.calculate(session.store.snapshot())
            if result is None:
                continue
            analytics += 1
            state = states[session.spec.tab_name]
            frozen_state = frozen_states[session.spec.tab_name]
            portfolio_metrics = state.update(
                result,
                timestamp,
                session.config.market.funding_rate,
                session.config.market.brokerage_rate,
                threshold_ratio,
            )
            frozen_metrics = frozen_state.update(
                result,
                timestamp,
                session.config.market.funding_rate,
                session.config.market.brokerage_rate,
                threshold_ratio,
            )
            if portfolio_metrics.total_pnl is None and frozen_metrics.total_pnl is None:
                continue
            rows.append(
                {
                    "timestamp": timestamp.strftime("%Y-%m-%d %H:%M:%S"),
                    "universal_mid": round_or_none(result.universal_mid),
                    "running_total_pnl": round_or_none(portfolio_metrics.total_pnl),
                    "frozen_iv_running_total_pnl": round_or_none(
                        frozen_metrics.total_pnl
                    ),
                }
            )
            final_metrics = {
                "portfolio_total_pnl": portfolio_metrics.total_pnl,
                "frozen_iv_total_pnl": frozen_metrics.total_pnl,
            }
    return rows, final_metrics, cycles, analytics


def run_direct_pnl_only(dataset, workbook: Path, threshold_ratio: float):
    replay = None
    current_time = lambda: replay.now()
    sessions = build_backtest_sessions(dataset, workbook, current_time, refresh_ms=0)
    replay = CsvReplayFeed(dataset, sessions)
    register_token_tickers(sessions, dataset)
    if len(sessions) != 1:
        raise ValueError("The direct PnL-only pricer expects one expiry session.")
    session = sessions[0]

    full_state = DynamicGammaThresholdPortfolioState(session=session)
    frozen_bootstrap = DynamicGammaThresholdFrozenIvState(session)
    initial_result = None
    initial_timestamp = None
    cycles = 0
    analytics = 0
    while replay.advance():
        cycles += 1
        timestamp = replay.now()
        if timestamp.strftime("%H:%M") < PNL_START_TIME:
            continue
        result = session.analytics.calculate(session.store.snapshot())
        if result is None:
            continue
        analytics += 1
        full_metrics = full_state.update(
            result,
            timestamp,
            session.config.market.funding_rate,
            session.config.market.brokerage_rate,
            threshold_ratio,
        )
        frozen_metrics = frozen_bootstrap.update(
            result,
            timestamp,
            session.config.market.funding_rate,
            session.config.market.brokerage_rate,
            threshold_ratio,
        )
        if (
            full_state.portfolio is not None
            and full_metrics.total_pnl is not None
            and frozen_bootstrap.portfolio is not None
            and frozen_metrics.total_pnl is not None
        ):
            initial_result = result
            initial_timestamp = timestamp
            break
    if initial_result is None or initial_timestamp is None or full_state.portfolio is None:
        raise ValueError("Could not initialize the sample portfolio for direct pricing.")

    surface = dataset.option_surface(session.spec.expiry)
    price_book = DirectPriceBook(dataset, surface, session)
    hedge_strike = nearest_result_strike(initial_result, initial_result.universal_mid)
    market_state = DirectHedgeState(
        portfolio=full_state.portfolio,
        options_pv_snapshot=full_state.options_pv_snapshot,
        hedge_strike=hedge_strike,
    )
    frozen_state = DirectHedgeState(
        portfolio=frozen_bootstrap.portfolio,
        options_pv_snapshot=frozen_bootstrap.options_pv_snapshot,
        hedge_strike=hedge_strike,
        frozen_ivs=frozen_bootstrap.frozen_ivs.copy(),
    )
    user_value = initial_result.user_value
    rows: list[dict[str, object]] = []
    final_metrics: dict[str, float | None] = {}

    # Reprocess the initialized timestamp through the direct pricer, then continue.
    direct_timestamps = dataset.timestamps[replay.position :]
    for timestamp_value in direct_timestamps:
        cycles += 0 if timestamp_value == dataset.timestamps[replay.position] else 1
        timestamp = timestamp_value.to_pydatetime()
        row = surface.loc[timestamp_value]
        market_snapshot = price_book.market_snapshot(
            row,
            timestamp,
            user_value,
        )
        if market_snapshot is None:
            continue
        user_value = market_snapshot.user_value
        market_pnl = market_state.update(
            price_book,
            row,
            market_snapshot,
            timestamp,
            threshold_ratio,
        )
        frozen_pnl = frozen_state.update(
            price_book,
            row,
            market_snapshot,
            timestamp,
            threshold_ratio,
        )
        if market_pnl is None and frozen_pnl is None:
            continue
        rows.append(
            {
                "timestamp": timestamp.strftime("%Y-%m-%d %H:%M:%S"),
                "universal_mid": round_or_none(market_snapshot.universal_mid),
                "running_total_pnl": round_or_none(market_pnl),
                "frozen_iv_running_total_pnl": round_or_none(frozen_pnl),
            }
        )
        final_metrics = {
            "portfolio_total_pnl": market_pnl,
            "frozen_iv_total_pnl": frozen_pnl,
        }

    return rows, final_metrics, len(dataset.timestamps), analytics


@dataclass
class MarketSnapshot:
    timestamp: datetime
    time: float
    funding_factor: float
    discount_factor: float
    universal_mid: float
    universal_spot: float
    user_value: int


class DirectPriceBook:
    def __init__(self, dataset, surface: pd.DataFrame, session) -> None:
        self.dataset = dataset
        self.surface = surface
        self.session = session
        self.market = session.config.market
        self.ticker_lookup = {
            (int(row.strike), row.option_type): row.ticker
            for row in dataset.frame[
                dataset.frame["expiry"].eq(session.spec.expiry)
            ][["strike", "option_type", "ticker"]]
            .drop_duplicates()
            .itertuples(index=False)
        }
        self.strikes = sorted(
            {
                int(row.strike)
                for row in dataset.frame[
                    dataset.frame["expiry"].eq(session.spec.expiry)
                ][["strike"]].drop_duplicates().itertuples(index=False)
            }
        )

    def market_snapshot(
        self,
        row: pd.Series,
        timestamp: datetime,
        user_value: int,
    ) -> MarketSnapshot | None:
        intraday = self.session.analytics._intraday_remaining(timestamp)
        fraction_days = intraday * self.market.intraday_var
        time_value = (self.market.full_days + fraction_days) / self.market.calendar_days
        funding_factor = math.exp(time_value * self.market.funding_rate)
        discount_factor = math.exp(self.market.risk_free_rate * time_value)
        best_bid = None
        best_ask = None
        for strike in self.strikes:
            ce = self.option_price(row, strike, "CE")
            pe = self.option_price(row, strike, "PE")
            if ce is None or pe is None:
                continue
            synth_bid = (
                strike
                + (ce - pe) * funding_factor
                - self.market.brokerage_rate * (ce + pe)
            )
            synth_ask = (
                strike
                + (ce - pe) * funding_factor
                + self.market.brokerage_rate * (ce + pe)
            )
            if abs(strike - user_value) <= self.market.synthetic_search_width:
                best_bid = synth_bid if best_bid is None else max(best_bid, synth_bid)
                best_ask = synth_ask if best_ask is None else min(best_ask, synth_ask)
        if best_bid is None or best_ask is None:
            return None
        universal_mid = (best_bid + best_ask) / 2
        next_user_value = (
            round(universal_mid / self.market.strike_round_base)
            * self.market.strike_round_base
        )
        return MarketSnapshot(
            timestamp=timestamp,
            time=time_value,
            funding_factor=funding_factor,
            discount_factor=discount_factor,
            universal_mid=universal_mid,
            universal_spot=universal_mid / discount_factor,
            user_value=next_user_value,
        )

    def option_price(self, row: pd.Series, strike: int, option_type: str) -> float | None:
        ticker = self.ticker_lookup.get((int(strike), option_type))
        if ticker is None or ticker not in row.index:
            return None
        value = row[ticker]
        if pd.isna(value):
            return None
        return float(value)

    def hedge_prices(
        self,
        row: pd.Series,
        snapshot: MarketSnapshot,
        strike: int,
    ) -> tuple[float, float, float] | None:
        ce = self.option_price(row, strike, "CE")
        pe = self.option_price(row, strike, "PE")
        if ce is None or pe is None:
            return None
        synth_bid, synth_ask = synthetic_prices(
            strike=strike,
            ce_bid=ce,
            ce_ask=ce,
            pe_bid=pe,
            pe_ask=pe,
            funding_rate=self.market.funding_rate,
            brokerage_rate=self.market.brokerage_rate,
            time=snapshot.time,
        )
        return synth_bid, synth_ask, (synth_bid + synth_ask) / 2


@dataclass
class DirectHedgeState:
    portfolio: object
    options_pv_snapshot: float | None
    hedge_strike: int
    frozen_ivs: dict[tuple[int, str], float] = field(default_factory=dict)
    hedge_trades: list[dict[str, float | datetime]] = field(default_factory=list)
    cumulative_hedge_lots: float = 0.0
    last_options_pv: float | None = None
    last_delta_lots: float | None = None
    last_gamma_lots: float | None = None
    last_total_pnl: float | None = None

    def update(
        self,
        price_book: DirectPriceBook,
        row: pd.Series,
        snapshot: MarketSnapshot,
        timestamp: datetime,
        threshold_ratio: float,
    ) -> float | None:
        values = direct_portfolio_values(
            self.portfolio.positions,
            price_book,
            row,
            snapshot,
            self.frozen_ivs,
        )
        if values is None:
            return None
        options_pv, delta_lots, gamma_lots = values
        self.last_options_pv = options_pv
        self.last_delta_lots = delta_lots
        self.last_gamma_lots = gamma_lots
        if self.options_pv_snapshot is None:
            self.options_pv_snapshot = options_pv
        threshold = dynamic_gamma_threshold(gamma_lots, threshold_ratio)
        combined_delta = delta_lots + self.cumulative_hedge_lots
        if abs(combined_delta) > threshold:
            self.add_hedge_trade(
                round(-combined_delta),
                price_book,
                row,
                snapshot,
                timestamp,
            )
        hedge_pnl = self.hedge_pnl(price_book, row, snapshot)
        self.last_total_pnl = (options_pv - self.options_pv_snapshot) + (hedge_pnl or 0.0)
        return self.last_total_pnl

    def add_hedge_trade(
        self,
        lots_change: float,
        price_book: DirectPriceBook,
        row: pd.Series,
        snapshot: MarketSnapshot,
        timestamp: datetime,
    ) -> None:
        if abs(lots_change) < 1e-9:
            return
        prices = price_book.hedge_prices(row, snapshot, self.hedge_strike)
        if prices is None:
            return
        synth_bid, synth_ask, _ = prices
        trade_price = synth_ask if lots_change > 0 else synth_bid
        self.hedge_trades.append(
            {
                "timestamp": timestamp,
                "lots_change": lots_change,
                "trade_price": trade_price,
            }
        )
        self.cumulative_hedge_lots += lots_change

    def hedge_pnl(
        self,
        price_book: DirectPriceBook,
        row: pd.Series,
        snapshot: MarketSnapshot,
    ) -> float | None:
        if not self.hedge_trades:
            return None
        prices = price_book.hedge_prices(row, snapshot, self.hedge_strike)
        if prices is None:
            return None
        _, _, synth_mid = prices
        multiplier = hedge_multiplier(self.portfolio.positions)
        return sum(
            float(trade["lots_change"])
            * (synth_mid - float(trade["trade_price"]))
            * multiplier
            / 1000
            for trade in self.hedge_trades
        )


def direct_portfolio_values(
    positions,
    price_book: DirectPriceBook,
    row: pd.Series,
    snapshot: MarketSnapshot,
    frozen_ivs: dict[tuple[int, str], float],
) -> tuple[float, float, float] | None:
    options_pv = 0.0
    delta_lots = 0.0
    gamma_lots = 0.0
    for position in positions:
        market_mid = price_book.option_price(row, position.strike, position.option_type)
        if market_mid is None:
            return None
        frozen_iv = frozen_ivs.get((position.strike, position.option_type))
        if frozen_iv is None:
            option_price = market_mid
            try:
                vol = implied_volatility(
                    market_mid,
                    snapshot.universal_spot,
                    position.strike,
                    snapshot.time,
                    price_book.market.funding_rate,
                    position.option_type,
                )
            except (ValueError, ZeroDivisionError, OverflowError):
                return None
        else:
            vol = frozen_iv
            option_price = black_scholes_price(
                snapshot.universal_spot,
                position.strike,
                snapshot.time,
                price_book.market.funding_rate,
                vol,
                position.option_type,
            )
        options_pv += position.qty * option_price / 1000
        delta = black_scholes_delta(
            snapshot.universal_spot,
            position.strike,
            snapshot.time,
            price_book.market.funding_rate,
            vol,
            position.option_type,
        )
        gamma = black_scholes_gamma(
            snapshot.universal_spot,
            position.strike,
            snapshot.time,
            price_book.market.funding_rate,
            vol,
        )
        delta_ccy = delta * snapshot.universal_spot * position.qty / 100000
        delta_lots += (
            delta_ccy * 100000 / position.mult / snapshot.universal_spot
            if position.mult and snapshot.universal_spot
            else 0.0
        )
        gamma_ccy_10bps = (
            gamma * snapshot.universal_spot * snapshot.universal_spot * 0.01
            * position.qty
            / 100000
            / 10
        )
        gamma_lots += (
            gamma_ccy_10bps * 100000 / snapshot.universal_spot / position.mult
            if position.mult and snapshot.universal_spot
            else 0.0
        )
    return options_pv, delta_lots, gamma_lots


def build_light_sample_portfolio(session, snapshot: MarketSnapshot, strikes: list[int]):
    if session.spec.underlying == "SENSEX":
        put_otm = nearest_downside_strikes(
            strikes,
            snapshot.universal_mid,
            SENSEX_SHORT_COUNT,
        )
        call_otm = nearest_upside_strikes(
            strikes,
            snapshot.universal_mid,
            SENSEX_SHORT_COUNT,
        )
        if len(put_otm) < SENSEX_SHORT_COUNT or len(call_otm) < SENSEX_SHORT_COUNT:
            return None
        long_put = nearest_strike(strikes, min(put_otm) - SENSEX_WING_DISTANCE)
        long_call = nearest_strike(strikes, max(call_otm) + SENSEX_WING_DISTANCE)
        maturity = session.spec.expiry.strftime("%d-%b-%y")
        positions = [
            build_position(session, maturity, strike, "PE", SENSEX_SHORT_LOTS)
            for strike in put_otm
        ]
        positions.extend(
            build_position(session, maturity, strike, "CE", SENSEX_SHORT_LOTS)
            for strike in call_otm
        )
        positions.append(build_position(session, maturity, long_put, "PE", SENSEX_LONG_LOTS))
        positions.append(build_position(session, maturity, long_call, "CE", SENSEX_LONG_LOTS))
    else:
        put_shorts = nearest_downside_strikes(strikes, snapshot.universal_mid, 3)
        call_shorts = nearest_upside_strikes(strikes, snapshot.universal_mid, 3)
        if len(put_shorts) < 3 or len(call_shorts) < 3:
            return None
        long_put = nearest_strike(strikes, min(put_shorts) - HEDGE_DISTANCE)
        long_call = nearest_strike(strikes, max(call_shorts) + HEDGE_DISTANCE)
        maturity = session.spec.expiry.strftime("%d-%b-%y")
        positions = [
            build_position(session, maturity, strike, "PE", SHORT_LOTS)
            for strike in put_shorts
        ]
        positions.append(build_position(session, maturity, long_put, "PE", LONG_LOTS))
        positions.extend(
            build_position(session, maturity, strike, "CE", SHORT_LOTS)
            for strike in call_shorts
        )
        positions.append(build_position(session, maturity, long_call, "CE", LONG_LOTS))
    positions.sort(key=lambda position: (position.strike, position.option_type))
    return SamplePortfolioRisk(
        session=session,
        positions=positions,
        risk_engine=StaticPortfolioRiskEngine(session.config.market),
    )


def capture_light_frozen_ivs(
    positions,
    price_book: DirectPriceBook,
    row: pd.Series,
    snapshot: MarketSnapshot,
) -> dict[tuple[int, str], float]:
    frozen = {}
    for position in positions:
        market_mid = price_book.option_price(row, position.strike, position.option_type)
        if market_mid is None:
            continue
        try:
            frozen[(position.strike, position.option_type)] = implied_volatility(
                market_mid,
                snapshot.universal_spot,
                position.strike,
                snapshot.time,
                price_book.market.funding_rate,
                position.option_type,
            )
        except (ValueError, ZeroDivisionError, OverflowError):
            continue
    return frozen


def write_pnl_csv(rows: list[dict[str, object]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def write_summary_csv(metrics: dict[str, object], path: Path) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["metric", "value"])
        writer.writeheader()
        for key, value in metrics.items():
            writer.writerow(
                {
                    "metric": key,
                    "value": round_or_none(value)
                    if isinstance(value, (int, float))
                    else value,
                }
            )


def build_workbook(run_dir: Path, underlying: str, trade_date) -> Path:
    node_modules_link = ROOT / "node_modules"
    created_link = ensure_node_modules_link(node_modules_link)
    try:
        env = os.environ.copy()
        env["RUN_DIR"] = str(run_dir)
        env["UNDERLYING"] = underlying
        env["TRADE_DATE"] = trade_date.isoformat()
        env["CLEAN_INPUTS"] = "0"
        subprocess.run(
            [
                str(NODE_EXE),
                str(ROOT / "scripts" / "build_sample_hf_pnl_only_workbook.mjs"),
            ],
            cwd=ROOT,
            env=env,
            check=True,
        )
    finally:
        if created_link:
            remove_junction(node_modules_link)
    workbooks = sorted(run_dir.glob("*_pnl_only.xlsx"))
    if not workbooks:
        raise SystemExit(f"No PnL-only workbook was created in {run_dir}.")
    return workbooks[-1]


def default_run_dir(underlying: str, trade_date) -> Path:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return ROOT / "runs_1s" / f"{underlying.lower()}_{trade_date:%Y%m%d}_pnl_only_{timestamp}"


def ensure_node_modules_link(link_path: Path) -> bool:
    if link_path.exists():
        return False
    if os.name == "nt":
        subprocess.run(
            ["cmd.exe", "/c", "mklink", "/J", str(link_path), str(NODE_MODULES)],
            cwd=ROOT,
            check=True,
            stdout=subprocess.DEVNULL,
        )
    else:
        link_path.symlink_to(NODE_MODULES, target_is_directory=True)
    return True


def remove_junction(link_path: Path) -> None:
    if not link_path.exists():
        return
    if os.name == "nt":
        subprocess.run(["cmd.exe", "/c", "rmdir", str(link_path)], cwd=ROOT, check=False)
    else:
        if link_path.is_symlink():
            link_path.unlink()
        else:
            shutil.rmtree(link_path)


def round_or_none(value) -> float | None:
    return round(float(value), 2) if isinstance(value, (int, float)) else None


def format_number(value) -> str:
    return "--" if value is None else f"{float(value):.2f}"


if __name__ == "__main__":
    main()
