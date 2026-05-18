from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime

import pandas as pd
from kiteconnect import KiteConnect

from fit_sensex.config import ApiConfig, MarketConfig
from fit_sensex.config import StrikeConfig
from fit_sensex.models import ChainRow, OptionQuote


OptionChain = dict[int, ChainRow]
TokenMap = dict[int, tuple[int, str]]


def parse_expiry_date(value: str) -> date:
    value = value.strip()
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    raise ValueError("Use expiry format YYYY-MM-DD, DD-MM-YYYY, or DD/MM/YYYY.")


def fetch_instruments(api: ApiConfig, exchange: str) -> pd.DataFrame:
    kite = KiteConnect(api_key=api.api_key)
    kite.set_access_token(api.access_token)
    return pd.DataFrame(kite.instruments(exchange))


def load_option_chain_for_expiry(
    api: ApiConfig,
    market: MarketConfig,
    strikes: StrikeConfig,
    expiry: date,
) -> tuple[OptionChain, TokenMap, list[int]]:
    df_instruments = fetch_instruments(api, market.exchange)
    filtered = df_instruments[
        (df_instruments["name"] == market.symbol_name)
        & (df_instruments["instrument_type"].isin(["CE", "PE"]))
        & (df_instruments["expiry"] == expiry)
    ]
    if filtered.empty:
        raise ValueError(
            f"No {market.symbol_name} options found on {market.exchange} for {expiry}."
        )

    return build_option_chain(filtered, strikes)


def build_option_chain(
    df_instruments: pd.DataFrame,
    strikes: StrikeConfig,
) -> tuple[OptionChain, TokenMap, list[int]]:
    raw_chain = defaultdict(lambda: {"CE": None, "PE": None})

    for _, row in df_instruments.iterrows():
        strike = int(row["strike"])
        option_type = row["instrument_type"]

        if option_type not in {"CE", "PE"}:
            continue
        if strike < strikes.low or strike > strikes.high:
            continue
        if strike % strikes.gap != 0:
            continue

        raw_chain[strike][option_type] = OptionQuote(
            token=int(row["instrument_token"])
        )

    chain: OptionChain = {
        strike: ChainRow(strike=strike, ce=data["CE"], pe=data["PE"])
        for strike, data in raw_chain.items()
    }

    token_to_strike: TokenMap = {}
    tokens: list[int] = []
    for strike, row in chain.items():
        for option_type, quote in (("CE", row.ce), ("PE", row.pe)):
            if quote is None:
                continue
            token_to_strike[quote.token] = (strike, option_type)
            tokens.append(quote.token)

    return chain, token_to_strike, tokens
