from __future__ import annotations

import tkinter as tk

from vol_dashboard.config import DEFAULT_WORKBOOK
from vol_dashboard.market.spot import SpotStore, build_spot_tokens
from vol_dashboard.market.stream import MultiExpiryKiteStream
from vol_dashboard.services.sessions import build_sessions
from vol_dashboard.ui.app import VolDashboardApp
from vol_dashboard.workbook import load_expiry_specs


def main() -> None:
    specs = load_expiry_specs(DEFAULT_WORKBOOK)
    print("Loaded expiries:")
    for spec in specs:
        print(f"  {spec.underlying} {spec.expiry:%d-%b-%y}")

    sessions = build_sessions(specs, DEFAULT_WORKBOOK)
    total_tokens = sum(len(session.tokens) for session in sessions)
    print(f"Prepared {len(sessions)} expiry sessions with {total_tokens} option tokens")
    for session in sessions:
        print(
            f"  {session.spec.tab_name}: "
            f"{len(session.chain)} strikes, {len(session.tokens)} tokens"
        )

    api = sessions[0].config.api
    spot_store = SpotStore()
    spot_tokens = build_spot_tokens(
        api,
        [session.spec.underlying for session in sessions],
    )
    for underlying, token in spot_tokens.items():
        print(f"  {underlying} spot token: {token}")

    stream = MultiExpiryKiteStream(
        api.api_key,
        api.access_token,
        sessions,
        spot_tokens=spot_tokens,
        spot_store=spot_store,
    )
    stream.start()

    root = tk.Tk()
    app = VolDashboardApp(root, sessions, spot_store=spot_store)
    app.start()


if __name__ == "__main__":
    main()
