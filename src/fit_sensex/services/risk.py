from __future__ import annotations

import csv
import math
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path

from fit_sensex.config import MarketConfig
from fit_sensex.models import AnalyticsResult, AnalyticsRow
from fit_sensex.pricing.black_scholes import (
    black_scholes_delta,
    black_scholes_gamma,
    black_scholes_price,
    black_scholes_theta_price_change,
    black_scholes_vega,
)


@dataclass(frozen=True)
class PortfolioPosition:
    book: str
    lots: float | str
    underlying: str
    maturity: str
    strike: int
    option_type: str
    qty: float
    mult: float


@dataclass(frozen=True)
class RiskRow:
    book: str
    lots: float | str
    underlying: str
    maturity: str
    strike: int
    option_type: str
    qty: float
    mult: float
    time_value: float | str
    price_fit: float | str
    bid_mkt: float | str
    ask_mkt: float | str
    mid_mkt: float | str
    model_iv: float | str
    bs_delta_pct: float | str
    bs_delta_ccy: float | str
    bs_delta_lots: float | str
    gamma_ccy_10bps: float | str
    gamma_lots_10bps: float | str
    vega_ccy_10bps: float | str
    bs_theta_ccy: float | str
    std_1w_vega: float | str


@dataclass(frozen=True)
class HedgeTrade:
    timestamp: str
    underlying: str
    maturity: str
    strike: int
    side: str
    lots_change: float
    synthetic_lots_before: float
    synthetic_lots_after: float
    mult: float
    trade_price: float
    combined_delta_lots: float
    threshold_lots: float
    universal_mid: float
    universal_spot: float


@dataclass(frozen=True)
class HedgeDecision:
    positions: list[PortfolioPosition]
    trades: list[HedgeTrade]


def load_portfolio(path: Path) -> list[PortfolioPosition]:
    if not path.exists():
        return []

    with path.open(newline="") as file:
        reader = csv.DictReader(file)
        return [parse_position(row) for row in reader if row_has_position(row)]


def save_portfolio(path: Path, positions: list[PortfolioPosition]) -> None:
    headers = ["Book", "Lots", "Underlying", "Maturity", "Strike", "Type", "Mult", "Qty"]
    with path.open("w", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=headers)
        writer.writeheader()
        for position in positions:
            writer.writerow(
                {
                    "Book": position.book,
                    "Lots": position.lots,
                    "Underlying": position.underlying,
                    "Maturity": position.maturity,
                    "Strike": position.strike,
                    "Type": position.option_type,
                    "Mult": position.mult,
                    "Qty": position.qty,
                }
            )


def append_trades(path: Path, trades: list[HedgeTrade]) -> None:
    if not trades:
        return

    file_exists = path.exists()
    headers = [
        "timestamp",
        "underlying",
        "maturity",
        "strike",
        "side",
        "lots_change",
        "synthetic_lots_before",
        "synthetic_lots_after",
        "mult",
        "trade_price",
        "combined_delta_lots",
        "threshold_lots",
        "universal_mid",
        "universal_spot",
    ]
    with path.open("a", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=headers)
        if not file_exists:
            writer.writeheader()
        for trade in trades:
            writer.writerow(trade.__dict__)


def row_has_position(row: dict[str, str]) -> bool:
    normalized = normalize_row(row)
    return bool(normalized.get("strike") and normalized.get("option_type"))


def parse_position(row: dict[str, str]) -> PortfolioPosition:
    normalized = normalize_row(row)
    mult = float(normalized["mult"])
    lots_raw = normalized.get("lots", "")
    qty_raw = normalized.get("qty", "")
    qty = float(qty_raw) if qty_raw != "" else float(lots_raw) * mult

    return PortfolioPosition(
        book=(normalized.get("book") or "options").strip().lower(),
        lots=float(lots_raw) if lots_raw != "" else "",
        underlying=normalized.get("underlying", ""),
        maturity=normalized.get("maturity", ""),
        strike=int(float(normalized["strike"])),
        option_type=normalized["option_type"].strip().upper(),
        qty=qty,
        mult=mult,
    )


def normalize_row(row: dict[str, str]) -> dict[str, str]:
    normalized = {
        str(key).strip().lower().replace(" ", "_"): str(value).strip()
        for key, value in row.items()
    }
    if "type" in normalized and "option_type" not in normalized:
        normalized["option_type"] = normalized["type"]
    return normalized


class RiskEngine:
    def __init__(self, market_config: MarketConfig) -> None:
        self.market_config = market_config
        self.trade_log_path = market_config.portfolio_file.with_name(
            f"{market_config.portfolio_file.stem}_trades.csv"
        )

    def calculate(self, result: AnalyticsResult) -> list[RiskRow]:
        raw_positions = [
            position
            for position in load_portfolio(self.market_config.portfolio_file)
            if self._matches_underlying(position)
        ]
        hedged = self._apply_synthetic_hedge(raw_positions, result)
        if hedged.trades:
            save_portfolio(self.market_config.portfolio_file, hedged.positions)
            append_trades(self.trade_log_path, hedged.trades)

        rows_by_strike = {row.strike: row for row in result.rows}
        risk_rows: list[RiskRow] = []
        for position in hedged.positions:
            market_row = rows_by_strike.get(position.strike)
            if market_row is None or market_row.model_iv is None:
                risk_rows.append(self._blank_row(position))
                continue
            risk_rows.append(self._calculate_position(position, market_row, result))
        return risk_rows

    def _matches_underlying(self, position: PortfolioPosition) -> bool:
        if not position.underlying:
            return True
        return position.underlying.strip().upper() == self.market_config.symbol_name.upper()

    def _apply_synthetic_hedge(
        self,
        positions: list[PortfolioPosition],
        result: AnalyticsResult,
    ) -> HedgeDecision:
        option_positions = [position for position in positions if position.book == "options"]
        delta_positions = [position for position in positions if position.book == "delta"]
        if not delta_positions:
            inferred = self._infer_delta_pair(option_positions)
            if inferred is not None:
                delta_positions = [
                    replace(inferred[0], book="delta", lots=inferred[0].qty / inferred[0].mult),
                    replace(inferred[1], book="delta", lots=inferred[1].qty / inferred[1].mult),
                ]
                inferred_set = set(inferred)
                option_positions = [
                    position for position in option_positions if position not in inferred_set
                ]
        if not option_positions or not delta_positions:
            return HedgeDecision(positions=positions, trades=[])

        synthetic_pair = self._delta_pair(delta_positions)
        if synthetic_pair is None:
            return HedgeDecision(positions=positions, trades=[])

        option_rows = self._risk_rows_for_positions(option_positions, result)
        delta_rows = self._risk_rows_for_positions(delta_positions, result)
        options_delta_lots = sum_numeric(option_rows, "bs_delta_lots")
        synthetic_delta_lots = sum_numeric(delta_rows, "bs_delta_lots")
        combined_delta_lots = options_delta_lots + synthetic_delta_lots
        options_gamma_lots = sum_numeric(option_rows, "gamma_lots_10bps")
        threshold_lots = (
            abs(options_gamma_lots) * self.market_config.delta_hedge_threshold_ratio
        )

        if abs(combined_delta_lots) < threshold_lots:
            return HedgeDecision(positions=positions, trades=[])

        current_synth_lots = synthetic_pair[0].qty / synthetic_pair[0].mult
        synthetic_delta_per_lot = self._synthetic_delta_per_lot(synthetic_pair, result)
        if synthetic_delta_per_lot == 0:
            return HedgeDecision(positions=positions, trades=[])
        target_synth_lots = round(
            current_synth_lots - (combined_delta_lots / synthetic_delta_per_lot)
        )
        lots_change = target_synth_lots - current_synth_lots
        if lots_change == 0:
            return HedgeDecision(positions=positions, trades=[])

        trade = self._build_hedge_trade(
            synthetic_pair[0],
            lots_change,
            current_synth_lots,
            target_synth_lots,
            combined_delta_lots,
            threshold_lots,
            result,
        )
        updated_delta_positions = self._update_delta_pair(
            delta_positions,
            synthetic_pair,
            target_synth_lots,
        )
        updated_positions = option_positions + updated_delta_positions
        return HedgeDecision(positions=updated_positions, trades=[trade])

    def _synthetic_delta_per_lot(
        self,
        synthetic_pair: tuple[PortfolioPosition, PortfolioPosition],
        result: AnalyticsResult,
    ) -> float:
        call_position, put_position = synthetic_pair
        rows_by_strike = {row.strike: row for row in result.rows}
        market_row = rows_by_strike.get(call_position.strike)
        if market_row is None or market_row.model_iv is None:
            return 0.0

        vol = market_row.model_iv / 100
        rate = self.market_config.funding_rate
        spot = result.universal_spot
        time = result.time

        call_delta = black_scholes_delta(
            spot, call_position.strike, time, rate, vol, "CE"
        )
        put_delta = black_scholes_delta(
            spot, put_position.strike, time, rate, vol, "PE"
        )
        return call_delta - put_delta

    def _infer_delta_pair(
        self,
        option_positions: list[PortfolioPosition],
    ) -> tuple[PortfolioPosition, PortfolioPosition] | None:
        calls = [position for position in option_positions if position.option_type == "CE"]
        puts = [position for position in option_positions if position.option_type == "PE"]
        matches: list[tuple[PortfolioPosition, PortfolioPosition]] = []
        for call in calls:
            for put in puts:
                if (
                    call.strike == put.strike
                    and call.mult == put.mult
                    and call.maturity == put.maturity
                    and math.isclose(call.qty, -put.qty)
                ):
                    matches.append((call, put))
        return matches[-1] if len(matches) == 1 else None

    def _delta_pair(
        self,
        delta_positions: list[PortfolioPosition],
    ) -> tuple[PortfolioPosition, PortfolioPosition] | None:
        calls = [position for position in delta_positions if position.option_type == "CE"]
        puts = [position for position in delta_positions if position.option_type == "PE"]
        for call in calls:
            for put in puts:
                if (
                    call.strike == put.strike
                    and call.mult == put.mult
                    and call.maturity == put.maturity
                ):
                    return call, put
        return None

    def _build_hedge_trade(
        self,
        call_position: PortfolioPosition,
        lots_change: float,
        current_synth_lots: float,
        target_synth_lots: float,
        combined_delta_lots: float,
        threshold_lots: float,
        result: AnalyticsResult,
    ) -> HedgeTrade:
        market_row = next(row for row in result.rows if row.strike == call_position.strike)
        synth_bid, synth_ask = synthetic_prices(
            strike=call_position.strike,
            ce_bid=market_row.ce_bid,
            ce_ask=market_row.ce_ask,
            pe_bid=market_row.pe_bid,
            pe_ask=market_row.pe_ask,
            funding_rate=self.market_config.funding_rate,
            brokerage_rate=self.market_config.brokerage_rate,
            time=result.time,
        )
        side = "BUY" if lots_change > 0 else "SELL"
        trade_price = synth_ask if side == "BUY" else synth_bid

        return HedgeTrade(
            timestamp=datetime.now().isoformat(timespec="seconds"),
            underlying=self.market_config.symbol_name,
            maturity=call_position.maturity,
            strike=call_position.strike,
            side=side,
            lots_change=lots_change,
            synthetic_lots_before=current_synth_lots,
            synthetic_lots_after=target_synth_lots,
            mult=call_position.mult,
            trade_price=trade_price,
            combined_delta_lots=combined_delta_lots,
            threshold_lots=threshold_lots,
            universal_mid=result.universal_mid,
            universal_spot=result.universal_spot,
        )

    def _update_delta_pair(
        self,
        delta_positions: list[PortfolioPosition],
        synthetic_pair: tuple[PortfolioPosition, PortfolioPosition],
        target_synth_lots: float,
    ) -> list[PortfolioPosition]:
        call_position, put_position = synthetic_pair
        call_qty = target_synth_lots * call_position.mult
        put_qty = -target_synth_lots * put_position.mult

        updated_positions: list[PortfolioPosition] = []
        for position in delta_positions:
            if position == call_position:
                updated_positions.append(
                    replace(position, qty=call_qty, lots=target_synth_lots)
                )
            elif position == put_position:
                updated_positions.append(
                    replace(position, qty=put_qty, lots=-target_synth_lots)
                )
            else:
                updated_positions.append(position)
        return updated_positions

    def _risk_rows_for_positions(
        self,
        positions: list[PortfolioPosition],
        result: AnalyticsResult,
    ) -> list[RiskRow]:
        rows_by_strike = {row.strike: row for row in result.rows}
        risk_rows: list[RiskRow] = []
        for position in positions:
            market_row = rows_by_strike.get(position.strike)
            if market_row is None or market_row.model_iv is None:
                continue
            risk_rows.append(self._calculate_position(position, market_row, result))
        return risk_rows

    def _calculate_position(
        self,
        position: PortfolioPosition,
        market_row: AnalyticsRow,
        result: AnalyticsResult,
    ) -> RiskRow:
        option_type = position.option_type
        bid_mkt, ask_mkt = option_market_prices(market_row, option_type)
        mid_mkt = (bid_mkt + ask_mkt) / 2

        vol = market_row.model_iv / 100
        rate = self.market_config.funding_rate
        spot = result.universal_spot
        time = result.time
        qty = position.qty
        mult = position.mult
        intrinsic_value = intrinsic_value_from_synthetic_mid(
            option_type,
            result.universal_mid,
            position.strike,
        )

        price_fit = black_scholes_price(
            spot, position.strike, time, rate, vol, option_type
        )
        time_value = mid_mkt - intrinsic_value
        delta = black_scholes_delta(spot, position.strike, time, rate, vol, option_type)
        gamma = black_scholes_gamma(spot, position.strike, time, rate, vol)
        vega = black_scholes_vega(spot, position.strike, time, rate, vol)
        time_unit = (15 / 375) * 0.4 / 255.5
        theta_price_change = black_scholes_theta_price_change(
            spot, position.strike, time, time_unit, rate, vol, option_type
        )

        bs_delta_pct = delta
        bs_delta_ccy = bs_delta_pct * spot * qty / 100000
        bs_delta_lots = bs_delta_ccy * 100000 / mult / spot if mult and spot else 0
        gamma_ccy_10bps = gamma * spot * spot * 0.01 * qty / 100000 / 10
        gamma_lots_10bps = (
            gamma_ccy_10bps * 100000 / spot / mult if spot and mult else 0
        )
        vega_ccy_10bps = vega / 100 / 10 * qty
        bs_theta_ccy = theta_price_change * qty
        std_1w_vega = (
            vega_ccy_10bps / math.sqrt(time) * math.sqrt(5 / 248)
            if time > 0
            else 0
        )

        return RiskRow(
            book=position.book,
            lots=position.lots,
            underlying=position.underlying,
            maturity=position.maturity,
            strike=position.strike,
            option_type=option_type,
            qty=qty,
            mult=mult,
            time_value=time_value,
            price_fit=price_fit,
            bid_mkt=bid_mkt,
            ask_mkt=ask_mkt,
            mid_mkt=mid_mkt,
            model_iv=market_row.model_iv,
            bs_delta_pct=bs_delta_pct,
            bs_delta_ccy=bs_delta_ccy,
            bs_delta_lots=bs_delta_lots,
            gamma_ccy_10bps=gamma_ccy_10bps,
            gamma_lots_10bps=gamma_lots_10bps,
            vega_ccy_10bps=vega_ccy_10bps,
            bs_theta_ccy=bs_theta_ccy,
            std_1w_vega=std_1w_vega,
        )

    @staticmethod
    def _blank_row(position: PortfolioPosition) -> RiskRow:
        return RiskRow(
            book=position.book,
            lots=position.lots,
            underlying=position.underlying,
            maturity=position.maturity,
            strike=position.strike,
            option_type=position.option_type,
            qty=position.qty,
            mult=position.mult,
            time_value="",
            price_fit="",
            bid_mkt="",
            ask_mkt="",
            mid_mkt="",
            model_iv="",
            bs_delta_pct="",
            bs_delta_ccy="",
            bs_delta_lots="",
            gamma_ccy_10bps="",
            gamma_lots_10bps="",
            vega_ccy_10bps="",
            bs_theta_ccy="",
            std_1w_vega="",
        )


def option_market_prices(row: AnalyticsRow, option_type: str) -> tuple[float, float]:
    if option_type == "CE":
        return row.ce_bid, row.ce_ask
    if option_type == "PE":
        return row.pe_bid, row.pe_ask
    raise ValueError(f"Unsupported option_type {option_type!r}. Use CE or PE.")


def intrinsic_value_from_synthetic_mid(
    option_type: str,
    synthetic_mid: float,
    strike: int,
) -> float:
    if option_type == "CE":
        return max(synthetic_mid - strike, 0)
    if option_type == "PE":
        return max(strike - synthetic_mid, 0)
    raise ValueError(f"Unsupported option_type {option_type!r}. Use CE or PE.")


def synthetic_prices(
    strike: int,
    ce_bid: float,
    ce_ask: float,
    pe_bid: float,
    pe_ask: float,
    funding_rate: float,
    brokerage_rate: float,
    time: float,
) -> tuple[float, float]:
    funding_factor = math.exp(time * funding_rate)
    synth_bid = (
        strike
        + (ce_bid - pe_ask) * funding_factor
        - brokerage_rate * (ce_bid + pe_ask)
    )
    synth_ask = (
        strike
        + (ce_ask - pe_bid) * funding_factor
        + brokerage_rate * (ce_ask + pe_bid)
    )
    return synth_bid, synth_ask


def sum_numeric(rows: list[RiskRow], field_name: str) -> float:
    total = 0.0
    for row in rows:
        value = getattr(row, field_name, "")
        if isinstance(value, (int, float)):
            total += value
    return total
