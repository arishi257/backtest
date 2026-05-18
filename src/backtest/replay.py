from __future__ import annotations

from datetime import datetime

import pandas as pd

from backtest.data import OptionDataset
from fit_sensex.models import LatestQuote
from vol_dashboard.models import ExpirySession


class CsvReplayFeed:
    def __init__(self, dataset: OptionDataset, sessions: list[ExpirySession]) -> None:
        self.dataset = dataset
        self.sessions = sessions
        self.surfaces = {
            session.spec.expiry: dataset.option_surface(session.spec.expiry)
            for session in sessions
        }
        self.position = -1
        self.current_timestamp = dataset.timestamps[0].to_pydatetime()

    def now(self) -> datetime:
        return self.current_timestamp

    def advance(self) -> bool:
        next_position = self.position + 1
        if next_position >= len(self.dataset.timestamps):
            return False
        self.position = next_position
        self.current_timestamp = self.dataset.timestamps[self.position].to_pydatetime()
        for session in self.sessions:
            self._feed_session(session, self.dataset.timestamps[self.position])
        return True

    def _feed_session(self, session: ExpirySession, timestamp: pd.Timestamp) -> None:
        surface = self.surfaces[session.spec.expiry]
        row = surface.loc[timestamp]
        latest = {}
        for strike, chain_row in session.chain.items():
            ce_price = price_for_quote(row, chain_row.ce.token if chain_row.ce else None)
            pe_price = price_for_quote(row, chain_row.pe.token if chain_row.pe else None)
            if ce_price is None or pe_price is None:
                continue
            if chain_row.ce is not None:
                chain_row.ce.bid = ce_price
                chain_row.ce.ask = ce_price
            if chain_row.pe is not None:
                chain_row.pe.bid = pe_price
                chain_row.pe.ask = pe_price
            latest[strike] = LatestQuote(
                ce_bid=ce_price,
                ce_ask=ce_price,
                pe_bid=pe_price,
                pe_ask=pe_price,
            )
        with session.store._lock:
            session.store.latest_values = latest


def price_for_quote(row: pd.Series, token: int | None) -> float | None:
    if token is None:
        return None
    ticker = ticker_by_token.get(token)
    if ticker is None or ticker not in row.index:
        return None
    value = row[ticker]
    if pd.isna(value):
        return None
    return float(value)


ticker_by_token: dict[int, str] = {}


def register_token_tickers(sessions: list[ExpirySession], dataset: OptionDataset) -> None:
    ticker_lookup = {
        (row.expiry, int(row.strike), row.option_type): row.ticker
        for row in dataset.frame[
            ["expiry", "strike", "option_type", "ticker"]
        ].drop_duplicates().itertuples(index=False)
    }
    token_lookup = {}
    for session in sessions:
        for token, (strike, option_type) in session.store.token_to_strike.items():
            ticker = ticker_lookup.get((session.spec.expiry, strike, option_type))
            if ticker is not None:
                token_lookup[token] = ticker
    ticker_by_token.clear()
    ticker_by_token.update(token_lookup)


def empty_spot_snapshot() -> dict[str, float]:
    return {}
