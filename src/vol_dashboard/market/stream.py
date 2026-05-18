from __future__ import annotations

import threading

from kiteconnect import KiteTicker

from vol_dashboard.market.spot import SpotStore
from vol_dashboard.models import ExpirySession


class MultiExpiryKiteStream:
    def __init__(
        self,
        api_key: str,
        access_token: str,
        sessions: list[ExpirySession],
        spot_tokens: dict[str, int] | None = None,
        spot_store: SpotStore | None = None,
    ) -> None:
        self.sessions = sessions
        self.option_tokens = [token for session in sessions for token in session.tokens]
        self.spot_tokens = spot_tokens or {}
        self.spot_store = spot_store
        self.tokens = self.option_tokens + list(self.spot_tokens.values())
        self.store_by_token = {
            token: session.store for session in sessions for token in session.tokens
        }
        self.spot_underlying_by_token = {
            token: underlying for underlying, token in self.spot_tokens.items()
        }
        self.kws = KiteTicker(api_key, access_token)
        self.kws.on_connect = self.on_connect
        self.kws.on_ticks = self.on_ticks
        self.kws.on_error = self.on_error
        self.kws.on_close = self.on_close

    def on_connect(self, ws, response) -> None:
        print(
            f"Connected. Subscribing to {len(self.option_tokens)} option tokens "
            f"and {len(self.spot_tokens)} spot tokens"
        )
        ws.subscribe(self.tokens)
        if self.option_tokens:
            ws.set_mode(ws.MODE_FULL, self.option_tokens)
        if self.spot_tokens:
            ws.set_mode(ws.MODE_LTP, list(self.spot_tokens.values()))

    def on_ticks(self, ws, ticks) -> None:
        for tick in ticks:
            token = tick.get("instrument_token")
            store = self.store_by_token.get(token)
            if store is not None:
                store.update_tick(tick)
                continue

            underlying = self.spot_underlying_by_token.get(token)
            if underlying is not None and self.spot_store is not None:
                self.spot_store.update_tick(underlying, tick)

    @staticmethod
    def on_error(ws, code, reason) -> None:
        print("WebSocket Error:", reason)

    @staticmethod
    def on_close(ws, code, reason) -> None:
        print("WebSocket Closed:", reason)

    def start(self) -> threading.Thread:
        thread = threading.Thread(target=self.kws.connect, daemon=True)
        thread.start()
        return thread
