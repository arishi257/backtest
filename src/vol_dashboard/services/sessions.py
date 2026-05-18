from __future__ import annotations

from collections import defaultdict
from pathlib import Path

from vol_dashboard import compat  # noqa: F401
from fit_sensex.market.instruments import build_option_chain, fetch_instruments
from fit_sensex.market.kite_stream import MarketDataStore
from fit_sensex.services.analytics import AnalyticsEngine
from vol_dashboard.config import build_expiry_config
from vol_dashboard.models import ExpirySession, ExpirySpec


def build_sessions(specs: list[ExpirySpec], workbook_path: Path) -> list[ExpirySession]:
    configs = [
        (spec, build_expiry_config(spec.underlying, spec.expiry, workbook_path))
        for spec in specs
    ]
    if not configs:
        return []

    api = configs[0][1].api
    api.validate()

    instruments_by_exchange = {}
    for _, config in configs:
        exchange = config.market.exchange
        if exchange not in instruments_by_exchange:
            instruments_by_exchange[exchange] = fetch_instruments(api, exchange)

    sessions: list[ExpirySession] = []
    for spec, config in configs:
        df_instruments = instruments_by_exchange[config.market.exchange]
        filtered = df_instruments[
            (df_instruments["name"] == config.market.symbol_name)
            & (df_instruments["instrument_type"].isin(["CE", "PE"]))
            & (df_instruments["expiry"] == spec.expiry)
        ]
        if filtered.empty:
            raise ValueError(
                f"No {config.market.symbol_name} options found on "
                f"{config.market.exchange} for {spec.expiry}."
            )

        chain, token_to_strike, tokens = build_option_chain(filtered, config.strikes)
        store = MarketDataStore(chain, token_to_strike)
        sessions.append(
            ExpirySession(
                spec=spec,
                config=config,
                chain=chain,
                store=store,
                analytics=AnalyticsEngine(config.market, config.model),
                tokens=tokens,
            )
        )

    _validate_unique_tokens(sessions)
    return sessions


def _validate_unique_tokens(sessions: list[ExpirySession]) -> None:
    seen: dict[int, str] = {}
    duplicates: dict[int, list[str]] = defaultdict(list)
    for session in sessions:
        for token in session.tokens:
            label = session.spec.tab_name
            if token in seen:
                duplicates[token].extend([seen[token], label])
            else:
                seen[token] = label
    if duplicates:
        sample = next(iter(duplicates.items()))
        raise ValueError(
            f"Duplicate instrument token {sample[0]} found across "
            f"{', '.join(sorted(set(sample[1])))}."
        )

