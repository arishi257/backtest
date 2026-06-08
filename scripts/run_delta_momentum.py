from __future__ import annotations

import argparse
import csv
import math
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
SCRIPTS = ROOT / "scripts"
for path in (SRC, SCRIPTS):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from backtest.config import DEFAULT_WORKBOOK, normalize_underlying
from backtest.portfolio import multiplier_for
from backtest.sessions import build_backtest_sessions
from backtest_sample_hf.__main__ import (
    cache_file_path,
    load_sample_hf_option_dataset,
    parse_date_key,
)
from run_sample_hf_pnl_only import DirectPriceBook, round_or_none


DEFAULT_DATA_DIR = Path(r"C:\options data\1s data")
DEFAULT_START_TIME = "09:20"
DEFAULT_END_TIME = "15:15:00"
DEFAULT_GAMMA_LOTS_PER_10BPS = 60.0
DEFAULT_OFFSET_DELAY_SECONDS = 1
DEFAULT_UM_STRIKE_COUNT = 6
DEFAULT_BROKERAGE_RATE = 0.0024
DEFAULT_HEDGE_THRESHOLD_LOTS = 40


@dataclass
class ScheduledOffset:
    hedge_timestamp: datetime
    due_timestamp: datetime
    lots_change: int
    hedge_trade_price: float


@dataclass
class Trade:
    timestamp: datetime
    portfolio: str
    lots_change: int
    trade_price: float
    bought_premium_per_lot: float
    sold_premium_per_lot: float
    reason: str
    due_timestamp: datetime | None = None
    source_timestamp: datetime | None = None


@dataclass(frozen=True)
class SyntheticLegPrices:
    strike: int
    call_price: float
    put_price: float


@dataclass(frozen=True)
class DeltaMomentumSnapshot:
    timestamp: datetime
    time: float
    funding_factor: float
    discount_factor: float
    universal_mid: float
    universal_spot: float
    user_value: int
    bid_leg: SyntheticLegPrices
    ask_leg: SyntheticLegPrices


@dataclass
class DeltaMomentumResult:
    rows: list[dict[str, object]]
    trades: list[Trade]
    summary: dict[str, object]
    cycles: int
    analytics: int


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the delta_momentum virtual short-gamma hedge/offset strategy."
    )
    parser.add_argument("--date", required=True, help="Trading date, e.g. 26052026.")
    parser.add_argument(
        "--underlying",
        required=True,
        type=normalize_underlying,
        choices=("NIFTY", "SENSEX"),
    )
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument("--workbook", type=Path, default=DEFAULT_WORKBOOK)
    parser.add_argument("--start-time", default=DEFAULT_START_TIME)
    parser.add_argument("--end-time", default=DEFAULT_END_TIME)
    parser.add_argument(
        "--gamma-lots-per-10bps",
        type=float,
        default=DEFAULT_GAMMA_LOTS_PER_10BPS,
        help="Short-gamma lots generated per +0.1%% UM move from reference UM.",
    )
    parser.add_argument(
        "--offset-delay-seconds",
        type=int,
        default=DEFAULT_OFFSET_DELAY_SECONDS,
    )
    parser.add_argument(
        "--um-strike-count",
        type=int,
        default=DEFAULT_UM_STRIKE_COUNT,
        help="Number of complete strikes nearest to ATM/user value used for UM.",
    )
    parser.add_argument(
        "--brokerage-rate",
        type=float,
        default=DEFAULT_BROKERAGE_RATE,
        help="Brokerage rate applied to average bought/sold premium. Default 0.0024 = 0.24%%.",
    )
    parser.add_argument(
        "--hedge-threshold-lots",
        type=int,
        default=DEFAULT_HEDGE_THRESHOLD_LOTS,
        help="Rebalance to flat only when options + hedge delta breaches this lot threshold.",
    )
    parser.add_argument("--run-dir", type=Path, default=None)
    args = parser.parse_args()

    trade_date = parse_date_key(args.date)
    run_dir = args.run_dir or default_run_dir(args.underlying, trade_date)
    run_dir.mkdir(parents=True, exist_ok=True)

    cache_path = cache_file_path(args.data_dir, trade_date, args.underlying)
    cache_was_present = cache_path.exists()
    dataset = load_sample_hf_option_dataset(args.data_dir, trade_date, args.underlying)

    result = run_delta_momentum(
        dataset=dataset,
        workbook=args.workbook,
        start_time=args.start_time,
        end_time=args.end_time,
        gamma_lots_per_10bps=args.gamma_lots_per_10bps,
        offset_delay_seconds=args.offset_delay_seconds,
        um_strike_count=args.um_strike_count,
        brokerage_rate=args.brokerage_rate,
        hedge_threshold_lots=args.hedge_threshold_lots,
    )
    if not result.rows:
        raise SystemExit("No delta_momentum rows were produced.")

    timeseries_csv = run_dir / "delta_momentum_timeseries.csv"
    trades_csv = run_dir / "delta_momentum_trades.csv"
    summary_csv = run_dir / "summary_metrics.csv"
    write_csv(timeseries_csv, result.rows)
    write_trades_csv(trades_csv, result.trades)
    write_summary_csv(summary_csv, result.summary)

    print(f"Completed {result.cycles} 1-second cycles. Analytics rows: {result.analytics}.")
    print(f"Raw parsed-data cache: {cache_path}")
    print(f"Cache used at start: {'yes' if cache_was_present else 'no; created during this run'}")
    print(f"Run dir: {run_dir}")
    print(f"Timeseries: {timeseries_csv}")
    print(f"Trades: {trades_csv}")
    print("Final metrics:")
    for key in (
        "underlying",
        "trade_date",
        "start_time",
        "end_time",
        "um_strike_count",
        "brokerage_rate",
        "hedge_threshold_lots",
        "ref_timestamp",
        "ref_universal_mid",
        "final_universal_mid",
        "final_pnl",
        "final_pnl_after_brokerage",
        "brokerage_cost",
        "max_running_pnl",
        "min_running_pnl",
        "hedge_trade_count",
        "offset_trade_count",
        "delta_hedge_lots_traded",
        "delta_offset_lots_traded",
        "pending_offset_count",
    ):
        print(f"  {key}: {result.summary.get(key)}")


def run_delta_momentum(
    dataset,
    workbook: Path,
    start_time: str,
    end_time: str,
    gamma_lots_per_10bps: float,
    offset_delay_seconds: int,
    um_strike_count: int = DEFAULT_UM_STRIKE_COUNT,
    brokerage_rate: float = DEFAULT_BROKERAGE_RATE,
    hedge_threshold_lots: int = DEFAULT_HEDGE_THRESHOLD_LOTS,
) -> DeltaMomentumResult:
    current_time = datetime.now
    sessions = build_backtest_sessions(dataset, workbook, current_time, refresh_ms=0)
    if len(sessions) != 1:
        raise ValueError("delta_momentum expects one expiry session.")

    session = sessions[0]
    surface = dataset.option_surface(session.spec.expiry)
    price_book = DirectPriceBook(dataset, surface, session)
    lot_size = multiplier_for(dataset.underlying)

    start_clock = normalize_clock(start_time)
    end_clock = normalize_clock(end_time)
    offset_delay = timedelta(seconds=offset_delay_seconds)

    ref_timestamp: datetime | None = None
    ref_um: float | None = None
    current_user_value = session.config.market.user_value
    hedge_position = 0
    offset_position = 0
    scheduled_offsets: list[ScheduledOffset] = []
    trades: list[Trade] = []
    rows: list[dict[str, object]] = []
    cycles = 0
    analytics = 0

    for timestamp_value in dataset.timestamps:
        cycles += 1
        timestamp = timestamp_value.to_pydatetime()
        clock = timestamp.strftime("%H:%M:%S")
        if clock < start_clock:
            continue
        is_after_end = clock > end_clock
        if is_after_end and not scheduled_offsets:
            continue

        row = surface.loc[timestamp_value]
        snapshot = nearest_strike_um_snapshot(
            price_book,
            row,
            timestamp,
            current_user_value,
            um_strike_count,
        )
        if snapshot is None or not is_finite(snapshot.universal_mid):
            continue
        analytics += 1
        current_user_value = snapshot.user_value

        if ref_um is None:
            ref_timestamp = timestamp
            ref_um = snapshot.universal_mid

        executed_offsets = execute_due_offsets(
            scheduled_offsets,
            trades,
            timestamp,
            snapshot,
        )
        offset_position += sum(trade.lots_change for trade in executed_offsets)

        option_delta = option_delta_lots(
            snapshot.universal_mid,
            ref_um,
            gamma_lots_per_10bps,
        )
        rounded_option_delta = round_half_away_from_zero(option_delta)
        net_delta_before_hedge = rounded_option_delta + hedge_position
        hedge_trade_lots = (
            -net_delta_before_hedge
            if abs(net_delta_before_hedge) >= hedge_threshold_lots
            else 0
        )
        if is_after_end:
            hedge_trade_lots = 0
        if abs(hedge_trade_lots) >= 1:
            trade = Trade(
                timestamp=timestamp,
                portfolio="delta_hedge",
                lots_change=hedge_trade_lots,
                trade_price=snapshot.universal_mid,
                bought_premium_per_lot=synthetic_bought_premium_per_lot(
                    hedge_trade_lots,
                    snapshot,
                ),
                sold_premium_per_lot=synthetic_sold_premium_per_lot(
                    hedge_trade_lots,
                    snapshot,
                ),
                reason="rebalance_to_flat",
            )
            trades.append(trade)
            hedge_position += hedge_trade_lots
            scheduled_offsets.append(
                ScheduledOffset(
                    hedge_timestamp=timestamp,
                    due_timestamp=timestamp + offset_delay,
                    lots_change=-hedge_trade_lots,
                    hedge_trade_price=snapshot.universal_mid,
                )
            )

        net_delta_after_hedge = rounded_option_delta + hedge_position
        running_pnl = portfolio_pnl(trades, snapshot.universal_mid, lot_size)
        premium_bought, premium_sold = traded_premiums(trades, lot_size)
        brokerage_cost = brokerage_from_premiums(
            premium_bought,
            premium_sold,
            brokerage_rate,
        )
        running_pnl_after_brokerage = running_pnl - brokerage_cost
        rows.append(
            {
                "timestamp": timestamp.strftime("%Y-%m-%d %H:%M:%S"),
                "universal_mid": round_or_none(snapshot.universal_mid),
                "ref_universal_mid": round_or_none(ref_um),
                "move_pct_from_ref": round_or_none((snapshot.universal_mid / ref_um - 1.0) * 100),
                "raw_option_delta_lots": round_or_none(option_delta),
                "option_delta_lots": rounded_option_delta,
                "net_delta_before_hedge_lots": net_delta_before_hedge,
                "net_delta_after_hedge_lots": net_delta_after_hedge,
                "hedge_threshold_lots": hedge_threshold_lots,
                "delta_hedge_position_lots": hedge_position,
                "delta_offset_position_lots": offset_position,
                "combined_delta_position_lots": hedge_position + offset_position,
                "hedge_trade_lots": hedge_trade_lots,
                "offset_trade_lots": sum(trade.lots_change for trade in executed_offsets),
                "is_after_end_time": is_after_end,
                "pending_offset_count": len(scheduled_offsets),
                "total_premium_bought": round_or_none(premium_bought),
                "total_premium_sold": round_or_none(premium_sold),
                "brokerage_cost": round_or_none(brokerage_cost),
                "running_pnl_before_brokerage": round_or_none(running_pnl),
                "running_pnl_after_brokerage": round_or_none(running_pnl_after_brokerage),
            }
        )

    if ref_um is None or ref_timestamp is None:
        raise ValueError(f"No good UM timestamp found at or after {start_time}.")

    pnl_values = [
        float(row["running_pnl_before_brokerage"])
        for row in rows
        if isinstance(row.get("running_pnl_before_brokerage"), (int, float))
    ]
    summary = {
        "strategy": "delta_momentum",
        "underlying": dataset.underlying,
        "trade_date": dataset.trade_date.isoformat(),
        "start_time": start_time,
        "end_time": end_time,
        "gamma_lots_per_10bps": gamma_lots_per_10bps,
        "offset_delay_seconds": offset_delay_seconds,
        "um_strike_count": um_strike_count,
        "brokerage_rate": brokerage_rate,
        "hedge_threshold_lots": hedge_threshold_lots,
        "lot_size": lot_size,
        "ref_timestamp": ref_timestamp.strftime("%Y-%m-%d %H:%M:%S"),
        "ref_universal_mid": round_or_none(ref_um),
        "final_universal_mid": rows[-1]["universal_mid"] if rows else None,
        "final_pnl": rows[-1]["running_pnl_before_brokerage"] if rows else None,
        "final_pnl_after_brokerage": rows[-1]["running_pnl_after_brokerage"] if rows else None,
        "total_premium_bought": rows[-1]["total_premium_bought"] if rows else None,
        "total_premium_sold": rows[-1]["total_premium_sold"] if rows else None,
        "brokerage_cost": rows[-1]["brokerage_cost"] if rows else None,
        "max_running_pnl": max(pnl_values) if pnl_values else None,
        "min_running_pnl": min(pnl_values) if pnl_values else None,
        "hedge_trade_count": sum(1 for trade in trades if trade.portfolio == "delta_hedge"),
        "offset_trade_count": sum(1 for trade in trades if trade.portfolio == "delta_offset"),
        "delta_hedge_lots_traded": sum(
            abs(trade.lots_change) for trade in trades if trade.portfolio == "delta_hedge"
        ),
        "delta_offset_lots_traded": sum(
            abs(trade.lots_change) for trade in trades if trade.portfolio == "delta_offset"
        ),
        "pending_offset_count": len(scheduled_offsets),
        "final_delta_hedge_position_lots": hedge_position,
        "final_delta_offset_position_lots": offset_position,
        "final_combined_delta_position_lots": hedge_position + offset_position,
    }
    return DeltaMomentumResult(rows, trades, summary, cycles, analytics)


def nearest_strike_um_snapshot(
    price_book: DirectPriceBook,
    row,
    timestamp: datetime,
    user_value: int,
    strike_count: int,
) -> DeltaMomentumSnapshot | None:
    if strike_count <= 0:
        raise ValueError("--um-strike-count must be greater than zero.")

    intraday = price_book.session.analytics._intraday_remaining(timestamp)
    fraction_days = intraday * price_book.market.intraday_var
    time_value = (
        price_book.market.full_days + fraction_days
    ) / price_book.market.calendar_days
    funding_factor = math.exp(time_value * price_book.market.funding_rate)
    discount_factor = math.exp(price_book.market.risk_free_rate * time_value)

    candidates = []
    for strike in price_book.strikes:
        ce = price_book.option_price(row, strike, "CE")
        pe = price_book.option_price(row, strike, "PE")
        if ce is None or pe is None:
            continue
        candidates.append((abs(strike - user_value), strike, ce, pe))
    if not candidates:
        return None

    nearest = sorted(candidates, key=lambda item: (item[0], item[1]))[:strike_count]
    best_bid = None
    best_ask = None
    bid_leg = None
    ask_leg = None
    for _, strike, ce, pe in nearest:
        synth_bid = (
            strike
            + (ce - pe) * funding_factor
            - price_book.market.brokerage_rate * (ce + pe)
        )
        synth_ask = (
            strike
            + (ce - pe) * funding_factor
            + price_book.market.brokerage_rate * (ce + pe)
        )
        if best_bid is None or synth_bid > best_bid:
            best_bid = synth_bid
            bid_leg = SyntheticLegPrices(strike=strike, call_price=ce, put_price=pe)
        if best_ask is None or synth_ask < best_ask:
            best_ask = synth_ask
            ask_leg = SyntheticLegPrices(strike=strike, call_price=ce, put_price=pe)

    if best_bid is None or best_ask is None or bid_leg is None or ask_leg is None:
        return None
    universal_mid = (best_bid + best_ask) / 2
    next_user_value = (
        round(universal_mid / price_book.market.strike_round_base)
        * price_book.market.strike_round_base
    )
    return DeltaMomentumSnapshot(
        timestamp=timestamp,
        time=time_value,
        funding_factor=funding_factor,
        discount_factor=discount_factor,
        universal_mid=universal_mid,
        universal_spot=universal_mid / discount_factor,
        user_value=next_user_value,
        bid_leg=bid_leg,
        ask_leg=ask_leg,
    )


def execute_due_offsets(
    scheduled_offsets: list[ScheduledOffset],
    trades: list[Trade],
    timestamp: datetime,
    snapshot: DeltaMomentumSnapshot,
) -> list[Trade]:
    due = [offset for offset in scheduled_offsets if offset.due_timestamp <= timestamp]
    if not due:
        return []
    scheduled_offsets[:] = [
        offset for offset in scheduled_offsets if offset.due_timestamp > timestamp
    ]
    executed = []
    for offset in due:
        trade = Trade(
            timestamp=timestamp,
            portfolio="delta_offset",
            lots_change=offset.lots_change,
            trade_price=snapshot.universal_mid,
            bought_premium_per_lot=synthetic_bought_premium_per_lot(
                offset.lots_change,
                snapshot,
            ),
            sold_premium_per_lot=synthetic_sold_premium_per_lot(
                offset.lots_change,
                snapshot,
            ),
            reason="delayed_offset",
            due_timestamp=offset.due_timestamp,
            source_timestamp=offset.hedge_timestamp,
        )
        trades.append(trade)
        executed.append(trade)
    return executed


def option_delta_lots(
    universal_mid: float,
    ref_universal_mid: float,
    gamma_lots_per_10bps: float,
) -> float:
    move_10bps_units = (universal_mid / ref_universal_mid - 1.0) / 0.001
    return -gamma_lots_per_10bps * move_10bps_units


def round_half_away_from_zero(value: float) -> int:
    if value == 0 or not math.isfinite(value):
        return 0
    sign = 1 if value > 0 else -1
    return sign * int(math.floor(abs(value) + 0.5))


def portfolio_pnl(trades: list[Trade], universal_mid: float, lot_size: int) -> float:
    return sum(
        trade.lots_change * (universal_mid - trade.trade_price) * lot_size
        for trade in trades
    )


def traded_premiums(trades: list[Trade], lot_size: int) -> tuple[float, float]:
    bought = 0.0
    sold = 0.0
    for trade in trades:
        bought += abs(trade.lots_change) * trade.bought_premium_per_lot * lot_size
        sold += abs(trade.lots_change) * trade.sold_premium_per_lot * lot_size
    return bought, sold


def synthetic_bought_premium_per_lot(
    lots_change: int,
    snapshot: DeltaMomentumSnapshot,
) -> float:
    if lots_change > 0:
        return snapshot.ask_leg.call_price
    if lots_change < 0:
        return snapshot.bid_leg.put_price
    return 0.0


def synthetic_sold_premium_per_lot(
    lots_change: int,
    snapshot: DeltaMomentumSnapshot,
) -> float:
    if lots_change > 0:
        return snapshot.ask_leg.put_price
    if lots_change < 0:
        return snapshot.bid_leg.call_price
    return 0.0


def brokerage_from_premiums(
    total_premium_bought: float,
    total_premium_sold: float,
    brokerage_rate: float,
) -> float:
    return brokerage_rate * ((total_premium_bought + total_premium_sold) / 2.0)


def normalize_clock(value: str) -> str:
    text = value.strip()
    parts = text.split(":")
    if len(parts) == 2:
        return f"{int(parts[0]):02d}:{int(parts[1]):02d}:00"
    if len(parts) == 3:
        return f"{int(parts[0]):02d}:{int(parts[1]):02d}:{int(parts[2]):02d}"
    raise argparse.ArgumentTypeError(f"Expected time as HH:MM or HH:MM:SS, got {value!r}.")


def is_finite(value: object) -> bool:
    return isinstance(value, (int, float)) and math.isfinite(float(value))


def write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        return
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def write_trades_csv(path: Path, trades: list[Trade]) -> None:
    rows = [
        {
            "timestamp": trade.timestamp.strftime("%Y-%m-%d %H:%M:%S"),
            "portfolio": trade.portfolio,
            "lots_change": trade.lots_change,
            "trade_price": round_or_none(trade.trade_price),
            "bought_premium_per_lot": round_or_none(trade.bought_premium_per_lot),
            "sold_premium_per_lot": round_or_none(trade.sold_premium_per_lot),
            "reason": trade.reason,
            "due_timestamp": trade.due_timestamp.strftime("%Y-%m-%d %H:%M:%S")
            if trade.due_timestamp
            else "",
            "source_timestamp": trade.source_timestamp.strftime("%Y-%m-%d %H:%M:%S")
            if trade.source_timestamp
            else "",
        }
        for trade in trades
    ]
    write_csv(path, rows)


def write_summary_csv(path: Path, metrics: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["metric", "value"])
        writer.writeheader()
        for key, value in metrics.items():
            writer.writerow({"metric": key, "value": value})


def default_run_dir(underlying: str, trade_date) -> Path:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return ROOT / "runs_1s" / f"{underlying.lower()}_{trade_date:%Y%m%d}_delta_momentum_{timestamp}"


if __name__ == "__main__":
    main()
