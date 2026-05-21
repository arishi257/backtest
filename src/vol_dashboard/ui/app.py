from __future__ import annotations

import csv
import math
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
import tkinter as tk
from tkinter import ttk

from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure

from vol_dashboard import compat  # noqa: F401
from backtest.config import DEFAULT_PROCESSED_OUTPUT_DIR
from backtest.portfolio import SamplePortfolioRisk, build_sample_portfolio
from backtest.processed_data import ProcessedDataWriter
from fit_sensex.models import AnalyticsResult
from fit_sensex.pricing.black_scholes import (
    black_scholes_delta,
    black_scholes_gamma,
    black_scholes_price,
    black_scholes_theta_price_change,
    black_scholes_vega,
)
from fit_sensex.pricing.vol_curve import ParametricVolCurve
from fit_sensex.services.risk import RiskRow, intrinsic_value_from_synthetic_mid, synthetic_prices
from fit_sensex.ui.app import (
    row_numeric_values as risk_row_numeric_values,
    row_values as risk_row_values,
    total_row_numeric_values as risk_total_row_numeric_values,
    total_row_values as risk_total_row_values,
    weighted_price_total,
)
from vol_dashboard.market.spot import SpotStore
from vol_dashboard.models import ExpirySession
from vol_dashboard.services.snapshots import SnapshotWriter, load_snapshot_rows


SENSEX_BG = "#e6e6e6"
SENSEX_PANEL_BG = "#f2f2f2"
BACKTEST_OUTPUT_DIR = Path(__file__).resolve().parents[3]
SNAPSHOT_DIR = BACKTEST_OUTPUT_DIR / "snapshots"


class VolDashboardApp:
    def __init__(
        self,
        root: tk.Tk,
        sessions: list[ExpirySession],
        spot_store: SpotStore | None = None,
        spot_points: list[tuple[datetime, float]] | None = None,
        spot_source: str | None = None,
        processed_output_dir: Path = DEFAULT_PROCESSED_OUTPUT_DIR,
        before_refresh: Callable[[], bool] | None = None,
        clock: Callable[[], datetime] | None = None,
        auto_schedule: bool = True,
    ) -> None:
        self.root = root
        self.sessions = sessions
        self.spot_store = spot_store
        self.spot_points = spot_points or []
        self.spot_source = spot_source
        self.before_refresh = before_refresh
        self.clock = clock or (lambda: datetime.now(ZoneInfo("Asia/Kolkata")))
        self.auto_schedule = auto_schedule
        self.tabs: list[ExpiryTab] = []
        self.overview_items: dict[str, str] = {}
        self.spot_items: dict[str, str] = {}
        self.latest_results: dict[str, AnalyticsResult] = {}
        self.locked_skews: dict[str, float] = {}
        self.lock_skew_vars: dict[str, tk.StringVar] = {}
        self.current_skew_labels: dict[str, tk.Label] = {}
        self.locked_skew_labels: dict[str, tk.Label] = {}
        self.snapshot_writer = SnapshotWriter(SNAPSHOT_DIR)
        self.processed_writer = ProcessedDataWriter(
            processed_output_dir,
            sessions,
        )
        self.slice_time_var = tk.StringVar(value="Loaded Slice IST: --")
        self.delta_hedge_threshold_var = tk.StringVar(value="1.3")
        self.committed_delta_hedge_threshold = 1.3
        self.delta_hedge_threshold_status_var = tk.StringVar(value="Committed: 1.30")
        self.snapshot_timestamp_var = tk.StringVar()
        self.snapshot_status_var = tk.StringVar(value="Select a snapshot")
        self.loaded_snapshots: dict[str, dict[str, str]] = {}
        self.snapshot_rows_by_tab: dict[str, dict[str, dict[str, str]]] = {}
        self.last_live_timestamp: datetime | None = None
        self.portfolio_risk: SamplePortfolioRisk | None = None

        self.root.title("Multi-Expiry Vol Dashboard")
        self.root.geometry("1320x820")
        self._build_ui()

    def start(self) -> None:
        self.refresh()
        self.root.mainloop()

    def _build_ui(self) -> None:
        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill="both", expand=True)

        overview = tk.Frame(self.notebook)
        self.notebook.add(overview, text="Overview")
        self._build_overview(overview)

        for session in self.sessions:
            frame = tk.Frame(self.notebook)
            self.notebook.add(frame, text=session.spec.tab_name)
            self.tabs.append(ExpiryTab(frame, session, self.locked_skews))

        spot_mid_frame = tk.Frame(self.notebook)
        self.notebook.add(spot_mid_frame, text="Spot vs Universal Mid")
        spot_time_frame = tk.Frame(self.notebook)
        self.notebook.add(spot_time_frame, text="Spot vs Time")
        self.spot_time_tab = SpotTimeTab(
            spot_time_frame,
            spot_mid_frame,
            self.spot_points,
            self.spot_source,
            self.sessions[0].spec.underlying if self.sessions else "NIFTY",
        )
        portfolio_frame = tk.Frame(self.notebook)
        self.notebook.add(portfolio_frame, text="Portfolio Risk")
        frozen_iv_frame = tk.Frame(self.notebook)
        self.notebook.add(frozen_iv_frame, text="Frozen IV Risk")
        hedges_frame = tk.Frame(self.notebook)
        self.notebook.add(hedges_frame, text="Hedges")
        self.hedges_tab = HedgesTab(hedges_frame)
        self.portfolio_tab = PortfolioRiskTab(portfolio_frame, self.hedges_tab)
        self.frozen_iv_tab = FrozenIvPortfolioTab(frozen_iv_frame)

    def _build_overview(self, parent: tk.Frame) -> None:
        tk.Label(
            parent,
            textvariable=self.slice_time_var,
            anchor="w",
            padx=8,
            pady=6,
            font=("Arial", 11, "bold"),
        ).pack(fill="x")
        controls = tk.Frame(parent)
        controls.pack(fill="x", padx=8, pady=(0, 6))
        tk.Label(controls, text="Delta Hedge Threshold Lots").pack(side="left")
        tk.Entry(
            controls,
            textvariable=self.delta_hedge_threshold_var,
            width=10,
            justify="right",
        ).pack(side="left", padx=(6, 0))
        tk.Button(
            controls,
            text="Commit",
            command=self._commit_delta_hedge_threshold,
            width=8,
        ).pack(side="left", padx=(6, 0))
        tk.Label(
            controls,
            textvariable=self.delta_hedge_threshold_status_var,
            anchor="w",
        ).pack(side="left", padx=(8, 0))

        columns = [
            "Expiry",
            "Underlying",
            "Tokens",
            "Live Strikes",
            "Universal Mid",
            "Synthetic Spread",
            "Roll",
            "ATM Vol",
            "User Locked Skew",
            "ATM Vol Skew",
            "ATM Vol Skew 2",
            "Fit Error",
            "Time",
            "Time Days",
            "Status",
        ]
        self.overview_tree = ttk.Treeview(parent, columns=columns, show="headings")
        self.overview_tree.tag_configure("sensex", background=SENSEX_BG)
        for column in columns:
            self.overview_tree.heading(column, text=column)
            self.overview_tree.column(column, width=145, anchor="center")
        self.overview_tree.pack(fill="both", expand=True, padx=8, pady=8)

        for underlying in overview_underlyings(self.sessions):
            item = self.overview_tree.insert(
                "",
                "end",
                values=(
                    "Spot",
                    f"{underlying.title()} Spot",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "Waiting for spot",
                ),
                tags=("sensex",) if underlying == "SENSEX" else (),
            )
            self.spot_items[underlying] = item

        for session in self.sessions:
            item = self.overview_tree.insert(
                "",
                "end",
                values=(
                    session.spec.expiry.strftime("%d-%b-%y"),
                    session.spec.underlying,
                    len(session.tokens),
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "Waiting for quotes",
                ),
                tags=("sensex",) if session.spec.underlying == "SENSEX" else (),
            )
            self.overview_items[session.spec.tab_name] = item

        self._build_snapshots_panel(parent)
        self._build_skew_lock_table(parent)

    def _build_snapshots_panel(self, parent: tk.Frame) -> None:
        panel = tk.LabelFrame(parent, text="Snapshot Comparison")
        panel.pack(fill="both", expand=False, padx=8, pady=(0, 8))

        controls = tk.Frame(panel)
        controls.pack(fill="x", padx=8, pady=8)

        tk.Label(controls, text="Timestamp").pack(side="left", padx=(0, 4))
        self.snapshot_timestamp_combo = ttk.Combobox(
            controls,
            textvariable=self.snapshot_timestamp_var,
            width=22,
            state="readonly",
        )
        self.snapshot_timestamp_combo.pack(side="left", padx=(0, 10))

        tk.Button(controls, text="Reload", command=self._reload_snapshot_files).pack(
            side="left",
            padx=(0, 6),
        )
        tk.Button(controls, text="Load", command=self._load_selected_snapshot).pack(
            side="left",
            padx=(0, 10),
        )
        tk.Label(controls, textvariable=self.snapshot_status_var, anchor="w").pack(
            side="left",
            fill="x",
            expand=True,
        )

        self.snapshot_columns = [
            "Expiry",
            "Universal Mid Live",
            "Universal Mid Previous",
            "% Chg",
            "ATM Vol Live",
            "ATM Vol Previous",
            "User Skew Live",
            "User Skew Previous",
            "Vol Beta",
            "SSR",
            "IST Live",
            "IST Previous",
        ]
        self.snapshot_grid = tk.Frame(panel)
        self.snapshot_grid.pack(fill="x", padx=8, pady=(0, 8))
        self._render_snapshot_header()

        self._reload_snapshot_files()

    def _render_snapshot_header(self) -> None:
        for col_index, column in enumerate(self.snapshot_columns):
            tk.Label(
                self.snapshot_grid,
                text=column,
                borderwidth=1,
                relief="solid",
                padx=4,
                pady=3,
                width=16,
                anchor="center",
                font=("Arial", 9, "bold"),
                bg="#f0f0f0",
            ).grid(row=0, column=col_index, sticky="nsew")

    def _reload_snapshot_files(self) -> None:
        SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
        self.snapshot_rows_by_tab = {}
        timestamp_sets = []
        for session in self.sessions:
            rows = load_snapshot_rows(self.snapshot_writer.path_for(session))
            rows_by_timestamp = {
                row.get("timestamp", ""): row for row in rows if row.get("timestamp")
            }
            self.snapshot_rows_by_tab[session.spec.tab_name] = rows_by_timestamp
            if rows_by_timestamp:
                timestamp_sets.append(set(rows_by_timestamp.keys()))

        common_timestamps = (
            sorted(set.intersection(*timestamp_sets)) if len(timestamp_sets) == len(self.sessions) else []
        )
        self.snapshot_timestamp_combo["values"] = common_timestamps
        if common_timestamps:
            if self.snapshot_timestamp_var.get() not in common_timestamps:
                self.snapshot_timestamp_var.set(common_timestamps[-1])
            self.snapshot_status_var.set(
                f"{len(common_timestamps)} common snapshots available"
            )
        else:
            self.snapshot_timestamp_var.set("")
            self.loaded_snapshots = {}
            self.snapshot_status_var.set("No timestamp is available for all expiries")
            self._render_snapshot_comparison()

    def _load_selected_snapshot(self) -> None:
        timestamp = self.snapshot_timestamp_var.get()
        loaded = {}
        for session in self.sessions:
            row = self.snapshot_rows_by_tab.get(session.spec.tab_name, {}).get(timestamp)
            if row is None:
                self.snapshot_status_var.set(
                    f"Snapshot {timestamp} is not available for all expiries"
                )
                return
            loaded[session.spec.tab_name] = row
        self.loaded_snapshots = loaded
        if not loaded:
            self.snapshot_status_var.set("Could not load selected snapshot")
            return
        self.snapshot_status_var.set(f"Loaded {timestamp}")
        self._render_snapshot_comparison()

    def _render_snapshot_comparison(self) -> None:
        if not hasattr(self, "snapshot_grid"):
            return
        for widget in self.snapshot_grid.grid_slaves():
            if int(widget.grid_info()["row"]) > 0:
                widget.destroy()
        if not self.loaded_snapshots:
            return

        for row_index, session in enumerate(self.sessions, start=1):
            snapshot = self.loaded_snapshots.get(session.spec.tab_name)
            result = self.latest_results.get(session.spec.tab_name)
            snapshot_mid = parse_optional_float(
                snapshot.get("universal_mid") if snapshot else None
            )
            snapshot_atm_vol = parse_optional_float(
                snapshot.get("atm_vol") if snapshot else None
            )
            snapshot_locked_skew = parse_optional_float(
                snapshot.get("user_locked_skew") if snapshot else None
            )
            snapshot_timestamp = snapshot.get("timestamp", "") if snapshot else ""
            live_mid = result.universal_mid if result is not None else None
            live_atm_vol = result.atm_vol if result is not None else None
            live_locked_skew = self.locked_skews.get(session.spec.tab_name)
            pct_change = percent_change(live_mid, snapshot_mid)
            vol_beta = calculate_snapshot_vol_beta(
                snapshot_mid,
                snapshot_atm_vol,
                result,
            )
            ssr = calculate_snapshot_ssr(vol_beta, snapshot_locked_skew)
            self._render_snapshot_row(
                row_index,
                session,
                (
                    session.spec.tab_name,
                    format_cash(live_mid),
                    format_cash(snapshot_mid),
                    format_percent(pct_change),
                    format_vol(live_atm_vol),
                    format_vol(snapshot_atm_vol),
                    format_optional_number(live_locked_skew),
                    format_optional_number(snapshot_locked_skew),
                    format_optional_number(vol_beta),
                    format_optional_number(ssr),
                    format_ist_timestamp(self.last_live_timestamp),
                    snapshot_timestamp,
                ),
                pct_change,
            )

    def _render_snapshot_row(
        self,
        row_index: int,
        session: ExpirySession,
        values: tuple,
        pct_change: float | None,
    ) -> None:
        background = SENSEX_BG if session.spec.underlying == "SENSEX" else "white"
        for col_index, value in enumerate(values):
            foreground = "red" if col_index == 3 and is_negative(pct_change) else "black"
            tk.Label(
                self.snapshot_grid,
                text=value,
                borderwidth=1,
                relief="solid",
                padx=4,
                pady=3,
                width=16,
                anchor="center",
                bg=background,
                fg=foreground,
                font=("Arial", 9),
            ).grid(row=row_index, column=col_index, sticky="nsew")

    def refresh(self) -> None:
        if self.before_refresh is not None and not self.before_refresh():
            self.snapshot_status_var.set("Replay complete")
            return

        snapshot_timestamp = self.clock().replace(
            second=0,
            microsecond=0,
        )
        self.last_live_timestamp = snapshot_timestamp
        self.slice_time_var.set(
            f"Loaded Slice IST: {snapshot_timestamp.strftime('%Y-%m-%d %H:%M:%S')}"
        )
        for tab in self.tabs:
            result = tab.refresh()
            if result is not None:
                self.latest_results[tab.session.spec.tab_name] = result
            self.snapshot_writer.maybe_write(
                snapshot_timestamp,
                tab.session,
                result,
                self.locked_skews.get(tab.session.spec.tab_name),
            )

        spot_by_underlying = (
            self.spot_store.snapshot() if self.spot_store is not None else {}
        )
        roll_by_tab = self._roll_by_tab(spot_by_underlying)

        for tab in self.tabs:
            self._render_overview(
                tab.session,
                self.latest_results.get(tab.session.spec.tab_name),
                roll_by_tab.get(tab.session.spec.tab_name),
            )

        nearest_session = self._nearest_session()
        nearest_result = (
            self.latest_results.get(nearest_session.spec.tab_name)
            if nearest_session is not None
            else None
        )
        universal_mid = nearest_result.universal_mid if nearest_result is not None else None
        atm_vol = nearest_result.atm_vol if nearest_result is not None else None
        calendar_days = (
            nearest_session.config.market.calendar_days
            if nearest_session is not None
            else None
        )
        intraday_var = (
            nearest_session.config.market.intraday_var
            if nearest_session is not None
            else None
        )
        self.spot_time_tab.record_universal_mid(snapshot_timestamp, universal_mid)
        full_day_spot_vol, full_day_mid_vol = self.spot_time_tab.full_day_vols(
            snapshot_timestamp,
            calendar_days,
            intraday_var,
        )
        self._refresh_portfolio_risk(full_day_spot_vol, full_day_mid_vol)
        if nearest_session is not None and nearest_result is not None:
            self.processed_writer.write(
                snapshot_timestamp,
                nearest_session,
                nearest_result,
                self.spot_points,
                self.spot_time_tab.universal_mid_points,
                self.portfolio_tab.last_total_pnl,
                self.portfolio_tab.last_gamma_l,
                self.frozen_iv_tab.last_total_pnl,
            )
        self.spot_time_tab.render(
            snapshot_timestamp,
            universal_mid,
            calendar_days,
            intraday_var,
            atm_vol,
            self.portfolio_tab.last_total_pnl,
            self.portfolio_tab.last_gamma_l,
            self.frozen_iv_tab.last_total_pnl,
        )
        self._render_spot_rows(spot_by_underlying)
        self._render_skew_lock_table()
        self._render_snapshot_comparison()

        if self.auto_schedule:
            refresh_ms = min(session.config.market.refresh_ms for session in self.sessions)
            self.root.after(refresh_ms, self.refresh)

    def _refresh_portfolio_risk(
        self,
        full_day_spot_vol: float | None,
        full_day_mid_vol: float | None,
    ) -> None:
        if not self.sessions:
            return

        nearest_session = sorted(self.sessions, key=lambda item: item.spec.expiry)[0]
        result = self.latest_results.get(nearest_session.spec.tab_name)
        if result is None:
            self.portfolio_tab.set_status("Waiting for nearest-expiry analytics")
            return

        if self.portfolio_risk is None:
            if (
                nearest_session.spec.underlying == "SENSEX"
                and self.last_live_timestamp is not None
                and not is_hedge_start_time(self.last_live_timestamp)
            ):
                self.portfolio_tab.set_status(
                    "Waiting until 09:20 to create SENSEX portfolio"
                )
                self.frozen_iv_tab.set_status(
                    "Waiting until 09:20 to freeze SENSEX portfolio"
                )
                return
            self.portfolio_risk = build_sample_portfolio(nearest_session, result)
            if self.portfolio_risk is None:
                self.portfolio_tab.set_status("Waiting for enough strikes to build portfolio")
                return

        risk_rows = self.portfolio_risk.calculate(result)
        self.portfolio_tab.render(
            result,
            risk_rows,
            self.portfolio_risk.positions,
            nearest_session.spec.tab_name,
            self.last_live_timestamp,
            nearest_session.config.market.risk_free_rate,
            nearest_session.config.market.funding_rate,
            nearest_session.config.market.brokerage_rate,
            self._delta_hedge_threshold(),
            full_day_spot_vol,
            full_day_mid_vol,
        )
        self.frozen_iv_tab.render(
            nearest_session,
            result,
            self.last_live_timestamp,
            nearest_session.config.market.funding_rate,
            nearest_session.config.market.brokerage_rate,
            self._delta_hedge_threshold(),
        )

    def _nearest_session(self) -> ExpirySession | None:
        if not self.sessions:
            return None
        return sorted(self.sessions, key=lambda item: item.spec.expiry)[0]

    def _delta_hedge_threshold(self) -> float:
        return self.committed_delta_hedge_threshold

    def _commit_delta_hedge_threshold(self) -> None:
        try:
            threshold = abs(float(self.delta_hedge_threshold_var.get().strip()))
        except ValueError:
            self.delta_hedge_threshold_status_var.set("Invalid threshold")
            return
        self.committed_delta_hedge_threshold = threshold
        self.delta_hedge_threshold_var.set(format_number(threshold, 2))
        self.delta_hedge_threshold_status_var.set(
            f"Committed: {format_number(threshold, 2)}"
        )

    def _build_skew_lock_table(self, parent: tk.Frame) -> None:
        frame = tk.LabelFrame(parent, text="User Locked Skew")
        frame.pack(fill="x", padx=8, pady=(0, 8))

        headers = ("Expiry", "Current ATM Skew", "Pending", "Locked", "", "", "")
        for col_index, header in enumerate(headers):
            tk.Label(frame, text=header, font=("Arial", 9, "bold"), padx=6).grid(
                row=0,
                column=col_index,
                sticky="ew",
                padx=2,
                pady=2,
            )

        for row_index, session in enumerate(self.sessions, start=1):
            tab_name = session.spec.tab_name
            bg = SENSEX_BG if session.spec.underlying == "SENSEX" else None
            label_style = {"bg": bg} if bg is not None else {}
            tk.Label(frame, text=tab_name, anchor="w", width=18, **label_style).grid(
                row=row_index,
                column=0,
                sticky="ew",
                padx=2,
                pady=2,
            )

            current_label = tk.Label(frame, text="", width=12, **label_style)
            current_label.grid(row=row_index, column=1, sticky="ew", padx=2, pady=2)
            self.current_skew_labels[tab_name] = current_label

            var = tk.StringVar()
            self.lock_skew_vars[tab_name] = var
            tk.Entry(frame, textvariable=var, width=12, justify="right").grid(
                row=row_index,
                column=2,
                sticky="ew",
                padx=2,
                pady=2,
            )

            locked_label = tk.Label(frame, text="", width=12, **label_style)
            locked_label.grid(row=row_index, column=3, sticky="ew", padx=2, pady=2)
            self.locked_skew_labels[tab_name] = locked_label

            tk.Button(
                frame,
                text="Mark",
                command=lambda name=tab_name: self._mark_atm_skew(name),
                width=8,
            ).grid(row=row_index, column=4, padx=2, pady=2)
            tk.Button(
                frame,
                text="Commit",
                command=lambda name=tab_name: self._commit_locked_skew(name),
                width=8,
            ).grid(row=row_index, column=5, padx=2, pady=2)
            tk.Button(
                frame,
                text="Clear",
                command=lambda name=tab_name: self._clear_locked_skew(name),
                width=8,
            ).grid(row=row_index, column=6, padx=2, pady=2)

    def _render_skew_lock_table(self) -> None:
        for session in self.sessions:
            tab_name = session.spec.tab_name
            result = self.latest_results.get(tab_name)
            current_skew = calculate_atm_vol_skew(result) if result is not None else None
            locked_skew = self.locked_skews.get(tab_name)
            self.current_skew_labels[tab_name].config(
                text=format_optional_number(current_skew, 2)
            )
            self.locked_skew_labels[tab_name].config(
                text=format_optional_number(locked_skew, 2)
            )

    def _mark_atm_skew(self, tab_name: str) -> None:
        result = self.latest_results.get(tab_name)
        if result is None:
            return
        current_skew = calculate_atm_vol_skew(result)
        if current_skew is None:
            return
        self.lock_skew_vars[tab_name].set(format_number(current_skew, 2))

    def _commit_locked_skew(self, tab_name: str) -> None:
        try:
            locked_skew = round(float(self.lock_skew_vars[tab_name].get().strip()), 2)
        except ValueError:
            return
        self.locked_skews[tab_name] = locked_skew
        self.lock_skew_vars[tab_name].set(format_number(locked_skew, 2))
        self._render_skew_lock_table()

    def _clear_locked_skew(self, tab_name: str) -> None:
        self.locked_skews.pop(tab_name, None)
        self.lock_skew_vars[tab_name].set("")
        self._render_skew_lock_table()

    def _render_spot_rows(self, spot_by_underlying: dict[str, float]) -> None:
        for underlying, item in self.spot_items.items():
            spot = spot_by_underlying.get(underlying)
            self.overview_tree.item(
                item,
                values=(
                    "Spot",
                    f"{underlying.title()} Spot",
                    "",
                    "",
                    format_cash(spot),
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "Live spot" if spot is not None else "Waiting for spot",
                ),
            )

    def _roll_by_tab(self, spot_by_underlying: dict[str, float]) -> dict[str, float]:
        rolls: dict[str, float] = {}
        for underlying in overview_underlyings(self.sessions):
            underlying_sessions = sorted(
                (
                    session
                    for session in self.sessions
                    if session.spec.underlying == underlying
                ),
                key=lambda session: session.spec.expiry,
            )
            previous_value = spot_by_underlying.get(underlying)
            previous_expiry = None
            for session in underlying_sessions:
                result = self.latest_results.get(session.spec.tab_name)
                if result is None:
                    continue
                if previous_value is not None:
                    roll = result.universal_mid - previous_value
                    if previous_expiry is not None:
                        gap_days = (session.spec.expiry - previous_expiry).days
                        if gap_days > 7:
                            roll = roll / (gap_days / 7)
                    rolls[session.spec.tab_name] = roll
                previous_value = result.universal_mid
                previous_expiry = session.spec.expiry
        return rolls

    def _render_overview(
        self,
        session: ExpirySession,
        result: AnalyticsResult | None,
        roll: float | None,
    ) -> None:
        item = self.overview_items[session.spec.tab_name]
        if result is None:
            self.overview_tree.item(
                item,
                values=(
                    session.spec.expiry.strftime("%d-%b-%y"),
                    session.spec.underlying,
                    len(session.tokens),
                    len(session.store.snapshot()),
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "Waiting for quotes",
                ),
            )
            return

        synthetic_spread = result.best_ask - result.best_bid
        time_days = result.time * session.config.market.calendar_days
        atm_vol_skew = calculate_atm_vol_skew(result)
        locked_skew = self.locked_skews.get(session.spec.tab_name)
        atm_vol_skew_2 = calculate_atm_vol_skew_2(result)

        self.overview_tree.item(
            item,
            values=(
                session.spec.expiry.strftime("%d-%b-%y"),
                session.spec.underlying,
                len(session.tokens),
                len(result.rows),
                format_cash(result.universal_mid),
                format_cash(synthetic_spread),
                format_cash(roll),
                format_number(result.atm_vol * 100, 2),
                format_optional_number(locked_skew),
                format_optional_number(atm_vol_skew),
                format_optional_number(atm_vol_skew_2),
                format_number(result.fit_error, 0, use_commas=True),
                format_number(result.time, 6),
                format_number(time_days, 2),
                "Live",
            ),
        )


class ExpiryTab:
    def __init__(
        self,
        parent: tk.Frame,
        session: ExpirySession,
        locked_skews: dict[str, float],
    ) -> None:
        self.parent = parent
        self.session = session
        self.locked_skews = locked_skews
        self.latest_result: AnalyticsResult | None = None
        self.bg = SENSEX_BG if session.spec.underlying == "SENSEX" else None
        self.panel_bg = SENSEX_PANEL_BG if session.spec.underlying == "SENSEX" else None
        self._build_ui()

    def _build_ui(self) -> None:
        if self.bg is not None:
            self.parent.configure(bg=self.bg)
        frame_style = {"bg": self.bg} if self.bg is not None else {}
        notebook = ttk.Notebook(self.parent)
        notebook.pack(fill="both", expand=True)

        table_tab = tk.Frame(notebook, **frame_style)
        slope_tab = tk.Frame(notebook, **frame_style)
        iv_tab = tk.Frame(notebook, **frame_style)
        error_tab = tk.Frame(notebook, **frame_style)
        cash_vol_tab = tk.Frame(notebook, **frame_style)

        notebook.add(table_tab, text="Option Chain")
        notebook.add(slope_tab, text="Slope Plot")
        notebook.add(iv_tab, text="Normal Vol Surface")
        notebook.add(error_tab, text="Error Surface")
        notebook.add(cash_vol_tab, text="Cash Vol Surface")

        self._build_table_tab(table_tab)
        self.fig, self.ax, self.canvas = build_plot(slope_tab, self.panel_bg)
        self.iv_fig, self.iv_ax, self.iv_canvas = build_plot(iv_tab, self.panel_bg)
        self.error_fig, self.error_ax, self.error_canvas = build_plot(
            error_tab,
            self.panel_bg,
        )
        self.cash_fig, self.cash_ax, self.cash_canvas = build_plot(
            cash_vol_tab,
            self.panel_bg,
        )

    def _build_table_tab(self, parent: tk.Frame) -> None:
        frame_style = {"bg": self.bg} if self.bg is not None else {}
        top = tk.Frame(parent, **frame_style)
        top.pack(fill="x", pady=10)
        self.user_tree, self.user_items = build_summary_table(
            top,
            "User Inputs",
            [
                "Funding Rate",
                "Brokerage Rate",
                "Low Strike",
                "High Strike",
                "Strike Gap",
                "User Value",
                "Time",
                "Full Days",
                "Fraction Days",
                "Intraday",
                "Intraday Var",
            ],
            side="left",
            padx=10,
            bg=self.bg,
        )
        self.market_tree, self.market_items = build_summary_table(
            top,
            "Market Variables",
            [
                "Best Synthetic Bid",
                "Best Synthetic Ask",
                "Universal Synthetic Mid",
                "Universal Spot",
                "ATM Vol",
                "ATM Vol Skew",
                "ATM Vol Skew 2",
                "Param a",
                "Param bL",
                "Param bR",
                "Param CapL",
                "Param FloorR",
                "Fit Error",
            ],
            side="left",
            padx=30,
            bg=self.bg,
        )

        columns = [
            "Strike",
            "Norm Strike",
            "Market IV Mid",
            "Model IV Mid",
            "Market - Model",
            "Slope",
            "Local Vol Skew",
            "Surf Delta",
            "ATM Skew Delta",
            "Skew Delta",
        ]
        self.tree = ttk.Treeview(parent, columns=columns, show="headings")
        self.tree.tag_configure("sensex", background=SENSEX_PANEL_BG)
        for column in columns:
            self.tree.heading(column, text=column)
            self.tree.column(column, width=120, anchor="center")
        self.tree.pack(fill="both", expand=True)

        self.status_label = tk.Label(
            parent,
            text="Waiting for complete bid/ask quotes",
            anchor="w",
            padx=8,
            pady=4,
            **frame_style,
        )
        self.status_label.pack(fill="x")

    def refresh(self) -> AnalyticsResult | None:
        result = self.session.analytics.calculate(self.session.store.snapshot())
        if result is None:
            self.status_label.config(
                text=f"Waiting for complete bid/ask quotes. Live strikes: "
                f"{len(self.session.store.snapshot())}"
            )
            return None

        self.latest_result = result
        self._render(result)
        return result

    def _render(self, result: AnalyticsResult) -> None:
        self._render_rows(result)
        self._render_summary(result)
        self._render_slope_plot(result)
        self._render_iv_smile(result)
        self._render_error_plot(result)
        self._render_cash_vol_curve(result)
        self.status_label.config(
            text=f"Live strikes: {len(result.rows)} | "
            f"Last universal mid: {format_cash(result.universal_mid)}"
        )

    def _render_rows(self, result: AnalyticsResult) -> None:
        for item in self.tree.get_children():
            self.tree.delete(item)
        atm_vol_skew = self.locked_skews.get(
            self.session.spec.tab_name,
            calculate_atm_vol_skew(result),
        )
        for row in sorted(result.rows, key=lambda item: item.strike):
            local_vol_skew = calculate_local_vol_skew(result, row.normalized_strike)
            surf_delta = calculate_surf_delta(
                result,
                row.strike,
                row.model_iv,
                local_vol_skew,
                self.session.config.market.risk_free_rate,
            )
            atm_skew_delta = calculate_surf_delta(
                result,
                row.strike,
                row.model_iv,
                atm_vol_skew,
                self.session.config.market.risk_free_rate,
            )
            skew_delta = difference_or_none(surf_delta, atm_skew_delta)
            self.tree.insert(
                "",
                "end",
                values=(
                    row.strike,
                    row.normalized_strike,
                    row.iv_mid,
                    format_optional_number(row.model_iv),
                    format_iv_diff(row.iv_mid, row.model_iv),
                    row.slope,
                    format_optional_number(local_vol_skew),
                    format_optional_number(surf_delta, 0),
                    format_optional_number(atm_skew_delta, 0),
                    format_optional_number(skew_delta, 0),
                ),
                tags=("sensex",) if self.session.spec.underlying == "SENSEX" else (),
            )

    def _render_summary(self, result: AnalyticsResult) -> None:
        market = self.session.config.market
        strikes = self.session.config.strikes
        a_fit, bl_fit, br_fit, capl_fit, floorr_fit = result.fitted_params
        atm_vol_skew = calculate_atm_vol_skew(result)
        atm_vol_skew_2 = calculate_atm_vol_skew_2(result)

        self._set_user("Funding Rate", f"{round(market.funding_rate * 100, 4)}%")
        self._set_user("Brokerage Rate", f"{round(market.brokerage_rate * 100, 4)}%")
        self._set_user("Low Strike", strikes.low)
        self._set_user("High Strike", strikes.high)
        self._set_user("Strike Gap", strikes.gap)
        self._set_user("User Value", result.user_value)
        self._set_user("Time", f"{round(result.time * 100, 4)}%")
        self._set_user("Full Days", result.full_days)
        self._set_user("Fraction Days", round(result.fraction_days, 4))
        self._set_user("Intraday", round(result.intraday, 4))
        self._set_user("Intraday Var", result.intraday_var)

        self._set_market("Best Synthetic Bid", format_cash(result.best_bid))
        self._set_market("Best Synthetic Ask", format_cash(result.best_ask))
        self._set_market("Universal Synthetic Mid", format_cash(result.universal_mid))
        self._set_market("Universal Spot", format_cash(result.universal_spot))
        self._set_market("ATM Vol", round(result.atm_vol * 100, 2))
        self._set_market("ATM Vol Skew", format_optional_number(atm_vol_skew))
        self._set_market("ATM Vol Skew 2", format_optional_number(atm_vol_skew_2))
        self._set_market("Param a", f"{round(a_fit, 4)}%")
        self._set_market("Param bL", f"{round(bl_fit, 4)}%")
        self._set_market("Param bR", f"{round(br_fit, 4)}%")
        self._set_market("Param CapL", f"{round(capl_fit, 4)}%")
        self._set_market("Param FloorR", f"{round(floorr_fit, 4)}%")
        self._set_market("Fit Error", round(result.fit_error, 4))

    def _set_user(self, name: str, value) -> None:
        self.user_tree.item(self.user_items[name], values=[name, value])

    def _set_market(self, name: str, value) -> None:
        self.market_tree.item(self.market_items[name], values=[name, value])

    def _render_slope_plot(self, result: AnalyticsResult) -> None:
        a_fit, bl_fit, br_fit, capl_fit, floorr_fit = result.fitted_params
        self.ax.clear()
        apply_axis_bg(self.ax, self.panel_bg)
        self.ax.plot(result.slope_x, result.slope_y, marker="o", label="Market Slope")

        model_slope_x = sorted(result.market_ns)
        model_slope_y = [
            ParametricVolCurve.model_slope(
                x, a_fit, bl_fit, br_fit, capl_fit, floorr_fit
            )
            for x in model_slope_x
        ]
        self.ax.plot(model_slope_x, model_slope_y, marker="x", label="Model Slope")
        self.ax.set_xlabel("Normalized Strike")
        self.ax.set_ylabel("Slope")
        self.ax.set_title(f"{self.session.spec.tab_name} Slope vs Normalized Strike")
        self.ax.grid(True)
        self.ax.legend()
        self.canvas.draw()

    def _render_iv_smile(self, result: AnalyticsResult) -> None:
        self.iv_ax.clear()
        apply_axis_bg(self.iv_ax, self.panel_bg)
        self.iv_ax.plot(result.market_ns, result.market_iv, marker="o", label="IV Market")
        if len(result.model_vols) == len(result.market_ns):
            self.iv_ax.plot(result.market_ns, result.model_vols, marker="x", label="IV Model")
        self.iv_ax.set_xlabel("Normalized Strike")
        self.iv_ax.set_ylabel("IV Market / IV Model")
        self.iv_ax.set_title(f"{self.session.spec.tab_name} IV Market vs IV Model")
        self.iv_ax.grid(True)
        self.iv_ax.legend()
        self.iv_canvas.draw()

    def _render_error_plot(self, result: AnalyticsResult) -> None:
        self.error_ax.clear()
        apply_axis_bg(self.error_ax, self.panel_bg)
        error_rows = [
            row
            for row in sorted(result.rows, key=lambda item: item.strike)
            if row.iv_mid != "" and row.model_iv is not None
        ]
        if error_rows:
            self.error_ax.plot(
                [row.strike for row in error_rows],
                [float(row.iv_mid) - float(row.model_iv) for row in error_rows],
                marker="o",
            )
        self.error_ax.axhline(y=0, linestyle="--")
        self.error_ax.set_xlabel("Cash Strike")
        self.error_ax.set_ylabel("IV Market - IV Model")
        self.error_ax.set_title(f"{self.session.spec.tab_name} IV Market - IV Model")
        self.error_ax.grid(True)
        self.error_canvas.draw()

    def _render_cash_vol_curve(self, result: AnalyticsResult) -> None:
        plot_rows = [
            row
            for row in sorted(result.rows, key=lambda item: item.strike)
            if row.iv_bid != "" and row.iv_ask != ""
        ]

        strikes = [row.strike for row in plot_rows]
        bid_iv = [float(row.iv_bid) for row in plot_rows]
        ask_iv = [float(row.iv_ask) for row in plot_rows]
        mid_iv = [(bid + ask) / 2 for bid, ask in zip(bid_iv, ask_iv)]
        lower_error = [mid - bid for mid, bid in zip(mid_iv, bid_iv)]
        upper_error = [ask - mid for ask, mid in zip(ask_iv, mid_iv)]
        model_rows = [row for row in plot_rows if row.model_iv is not None]

        self.cash_ax.clear()
        apply_axis_bg(self.cash_ax, self.panel_bg)
        if strikes:
            self.cash_ax.errorbar(
                strikes,
                mid_iv,
                yerr=[lower_error, upper_error],
                fmt="none",
                color="#0f9d80",
                ecolor="#0f9d80",
                elinewidth=1.3,
                capsize=3,
                markersize=4,
                label="IV Market Bid/Ask",
            )
        if model_rows:
            self.cash_ax.plot(
                [row.strike for row in model_rows],
                [float(row.model_iv) for row in model_rows],
                color="#9c00c8",
                linewidth=1.8,
                label="IV Model Fitted Curve",
            )

        self.cash_ax.set_xlabel("Cash Strike")
        self.cash_ax.set_ylabel("IV Market / IV Model")
        self.cash_ax.set_title(f"{self.session.spec.tab_name} Cash Vol Surface")
        self.cash_ax.grid(True)
        self.cash_ax.legend()
        self.cash_canvas.draw()


class SpotTimeTab:
    def __init__(
        self,
        parent: tk.Frame,
        price_parent: tk.Frame,
        points: list[tuple[datetime, float]],
        source: str | None = None,
        underlying: str = "NIFTY",
    ) -> None:
        self.parent = parent
        self.price_parent = price_parent
        self.points = points
        self.source = source
        self.underlying = underlying
        self.metric_vars = {
            "Replay time": tk.StringVar(value="--"),
            "Spot rows": tk.StringVar(value=str(len(points))),
            "Current close": tk.StringVar(value="--"),
            "Universal mid": tk.StringVar(value="--"),
            "Basis": tk.StringVar(value="--"),
            "ATM Vol": tk.StringVar(value="--"),
            "Vol Spot": tk.StringVar(value="--"),
            "Vol Universal Mid": tk.StringVar(value="--"),
            "Total PnL": tk.StringVar(value="--"),
            "Gamma Diff Total": tk.StringVar(value="--"),
            "Frozen IV Total PnL": tk.StringVar(value="--"),
            "Source": tk.StringVar(value=source or "--"),
        }
        self.universal_mid_points: list[tuple[datetime, float]] = []
        self.atm_vol_points: list[tuple[datetime, float]] = []
        self.vol_mid_points: list[tuple[datetime, float]] = []
        self.total_pnl_points: list[tuple[datetime, float]] = []
        self.gamma_l_points: list[tuple[datetime, float]] = []
        self.vol_fig: Figure
        self.vol_ax = None
        self.pnl_ax = None
        self.price_tab = SpotUniversalMidTab(
            price_parent,
            points,
            self.universal_mid_points,
            underlying,
        )
        self.vol_canvas = None
        self.top_moves_tree = None
        self._build_ui()

    def _build_ui(self) -> None:
        top_row = tk.Frame(self.parent)
        top_row.pack(fill="x", padx=8, pady=6)
        metrics = tk.LabelFrame(top_row, text="Spot vs Time Metrics")
        metrics.pack(side="left", fill="x", expand=True)
        for row_index, (name, value_var) in enumerate(self.metric_vars.items()):
            tk.Label(
                metrics,
                text=name,
                anchor="w",
                width=18,
                padx=6,
                pady=2,
                font=("Arial", 10, "bold"),
            ).grid(row=row_index, column=0, sticky="w")
            tk.Label(
                metrics,
                textvariable=value_var,
                anchor="w",
                padx=6,
                pady=2,
                wraplength=1100,
            ).grid(row=row_index, column=1, sticky="ew")
        metrics.grid_columnconfigure(1, weight=1)
        moves_frame = tk.LabelFrame(top_row, text="Universal Mid Moves > 0.10%")
        moves_frame.pack(side="right", fill="y", padx=(8, 0))
        self._build_top_moves_panel(moves_frame)

        body = tk.Frame(self.parent)
        body.pack(fill="both", expand=True)
        chart_frame = tk.Frame(body)
        chart_frame.pack(fill="both", expand=True)
        self.vol_fig, self.vol_ax, self.vol_canvas = build_plot(chart_frame)
        self.pnl_ax = self.vol_ax.twinx()

    def render(
        self,
        current_timestamp: datetime,
        universal_mid: float | None = None,
        calendar_days: float | None = None,
        intraday_var: float | None = None,
        atm_vol: float | None = None,
        total_pnl: float | None = None,
        gamma_l: float | None = None,
        frozen_iv_total_pnl: float | None = None,
    ) -> None:
        if self.vol_ax is None or self.pnl_ax is None or self.vol_canvas is None:
            return

        self.record_universal_mid(current_timestamp, universal_mid)
        self._record_metric_point(self.atm_vol_points, current_timestamp, atm_vol)
        self._record_metric_point(self.total_pnl_points, current_timestamp, total_pnl)
        self._record_metric_point(self.gamma_l_points, current_timestamp, gamma_l)

        self.vol_ax.clear()
        self.pnl_ax.clear()
        apply_axis_bg(self.vol_ax, None)
        if not self.points and not self.universal_mid_points:
            self.vol_ax.set_title("ATM Vol and Universal Mid Vol vs Time")
            self.vol_ax.set_xlabel("Time IST")
            self.vol_ax.set_ylabel("Vol")
            self.pnl_ax.set_ylabel("Total PnL")
            self.vol_ax.grid(True)
            self._set_metrics(
                current_timestamp=current_timestamp,
                current_close=None,
                current_mid=None,
                basis=None,
                atm_vol=atm_vol,
                vol_spot=None,
                full_day_vol_spot=None,
                vol_mid=None,
                full_day_vol_mid=None,
                total_pnl=total_pnl,
                gamma_diff_total=self.gamma_diff_total(),
                frozen_iv_total_pnl=frozen_iv_total_pnl,
                spot_rows=0,
                source="No spot or universal mid data loaded for this replay date",
            )
            self.price_tab.render(current_timestamp)
            self._render_top_universal_mid_moves()
            self.vol_canvas.draw()
            return

        current_close = self.current_spot_close(current_timestamp)
        current_mid = self.current_universal_mid()
        elapsed_spot_points = self.elapsed_spot_points(current_timestamp)
        vol_spot = annualized_running_vol(
            elapsed_spot_points,
            calendar_days,
            intraday_var,
        )
        vol_mid = annualized_running_vol(
            self.universal_mid_points,
            calendar_days,
            intraday_var,
        )
        full_day_vol_spot, full_day_vol_mid = self.full_day_vols(
            current_timestamp,
            calendar_days,
            intraday_var,
        )
        self._record_metric_point(self.vol_mid_points, current_timestamp, vol_mid)
        basis = (
            universal_mid - current_close
            if universal_mid is not None and current_close is not None
            else None
        )

        self.price_tab.render(current_timestamp)
        self._render_vol_series(current_timestamp)
        self._render_top_universal_mid_moves()
        self._set_metrics(
            current_timestamp=current_timestamp,
            current_close=current_close,
            current_mid=current_mid,
            basis=basis,
            atm_vol=atm_vol,
            vol_spot=vol_spot,
            full_day_vol_spot=full_day_vol_spot,
            vol_mid=vol_mid,
            full_day_vol_mid=full_day_vol_mid,
            total_pnl=total_pnl,
            gamma_diff_total=self.gamma_diff_total(),
            frozen_iv_total_pnl=frozen_iv_total_pnl,
            spot_rows=len(self.points),
            source=self.source,
        )
        self.vol_canvas.draw()

    def _set_metrics(
        self,
        current_timestamp: datetime,
        current_close: float | None,
        current_mid: float | None,
        basis: float | None,
        atm_vol: float | None,
        vol_spot: float | None,
        full_day_vol_spot: float | None,
        vol_mid: float | None,
        full_day_vol_mid: float | None,
        total_pnl: float | None,
        gamma_diff_total: float | None,
        frozen_iv_total_pnl: float | None,
        spot_rows: int,
        source: str | None,
    ) -> None:
        self.metric_vars["Replay time"].set(format_ist_timestamp(current_timestamp))
        self.metric_vars["Spot rows"].set(str(spot_rows))
        self.metric_vars["Current close"].set(
            format_cash(current_close) if current_close is not None else "--"
        )
        self.metric_vars["Universal mid"].set(
            format_cash(current_mid) if current_mid is not None else "--"
        )
        self.metric_vars["Basis"].set(
            format_optional_number(basis) if basis is not None else "--"
        )
        self.metric_vars["ATM Vol"].set(
            format_percent(atm_vol) if atm_vol is not None else "--"
        )
        self.metric_vars["Vol Spot"].set(
            format_vol_pair(vol_spot, full_day_vol_spot)
        )
        self.metric_vars["Vol Universal Mid"].set(
            format_vol_pair(vol_mid, full_day_vol_mid)
        )
        self.metric_vars["Total PnL"].set(
            format_number(total_pnl, 2) if total_pnl is not None else "--"
        )
        self.metric_vars["Gamma Diff Total"].set(
            format_number(gamma_diff_total, 2)
            if gamma_diff_total is not None
            else "--"
        )
        self.metric_vars["Frozen IV Total PnL"].set(
            format_number(frozen_iv_total_pnl, 2)
            if frozen_iv_total_pnl is not None
            else "--"
        )
        self.metric_vars["Source"].set(source or "--")

    def record_universal_mid(
        self,
        current_timestamp: datetime,
        universal_mid: float | None,
    ) -> None:
        if universal_mid is None:
            return
        if (
            not self.universal_mid_points
            or self.universal_mid_points[-1][0] != current_timestamp
        ):
            self.universal_mid_points.append((current_timestamp, universal_mid))

    @staticmethod
    def _record_metric_point(
        points: list[tuple[datetime, float]],
        current_timestamp: datetime,
        value: float | None,
    ) -> None:
        if value is None:
            return
        if not points or points[-1][0] != current_timestamp:
            points.append((current_timestamp, value))

    def full_day_vols(
        self,
        current_timestamp: datetime,
        calendar_days: float | None,
        intraday_var: float | None,
    ) -> tuple[float | None, float | None]:
        return (
            annualized_available_vol(
                self.elapsed_spot_points(current_timestamp),
                calendar_days,
                intraday_var,
            ),
            annualized_available_vol(
                self.universal_mid_points,
                calendar_days,
                intraday_var,
            ),
        )

    def elapsed_spot_points(
        self,
        current_timestamp: datetime,
    ) -> list[tuple[datetime, float]]:
        return [
            (timestamp, close)
            for timestamp, close in self.points
            if timestamp <= current_timestamp
        ]

    def current_spot_close(
        self,
        current_timestamp: datetime,
    ) -> float | None:
        elapsed_points = self.elapsed_spot_points(current_timestamp)
        if not elapsed_points:
            return None
        return elapsed_points[-1][1]

    def current_universal_mid(self) -> float | None:
        if not self.universal_mid_points:
            return None
        return self.universal_mid_points[-1][1]

    def _render_vol_series(self, current_timestamp: datetime) -> None:
        if self.vol_ax is None or self.pnl_ax is None or self.vol_fig is None:
            return

        vol_lines = []
        pnl_lines = []
        if self.atm_vol_points:
            times = [timestamp for timestamp, _ in self.atm_vol_points]
            vols = [vol * 100 for _, vol in self.atm_vol_points]
            line = self.vol_ax.plot(
                times,
                vols,
                color="#2e7d32",
                linewidth=1.8,
                label="ATM vol",
            )[0]
            vol_lines.append(line)
            self.vol_ax.scatter(
                [times[-1]],
                [vols[-1]],
                color="#2e7d32",
                s=24,
                zorder=3,
            )

        if self.vol_mid_points:
            times = [timestamp for timestamp, _ in self.vol_mid_points]
            vols = [vol * 100 for _, vol in self.vol_mid_points]
            line = self.vol_ax.plot(
                times,
                vols,
                color="#6a1b9a",
                linewidth=1.8,
                label="Vol universal mid",
            )[0]
            vol_lines.append(line)
            self.vol_ax.scatter(
                [times[-1]],
                [vols[-1]],
                color="#6a1b9a",
                s=24,
                zorder=3,
            )

        if self.total_pnl_points:
            times = [timestamp for timestamp, _ in self.total_pnl_points]
            pnls = [pnl for _, pnl in self.total_pnl_points]
            line = self.pnl_ax.plot(
                times,
                pnls,
                color="#455a64",
                linewidth=1.7,
                linestyle="-.",
                label="Total PnL",
            )[0]
            pnl_lines.append(line)
            self.pnl_ax.scatter(
                [times[-1]],
                [pnls[-1]],
                color="#455a64",
                s=24,
                zorder=3,
            )
            self.pnl_ax.axhline(0, color="#9e9e9e", linewidth=1)

        self.vol_ax.axvline(current_timestamp, color="#d32f2f", linestyle="--", linewidth=1)
        self.vol_ax.set_title("ATM Vol and Universal Mid Vol vs Time")
        self.vol_ax.set_xlabel("Time IST")
        self.vol_ax.set_ylabel("Vol (%)")
        self.pnl_ax.set_ylabel("Total PnL")
        self.vol_ax.grid(True)
        legend_lines = vol_lines + pnl_lines
        if legend_lines:
            self.vol_ax.legend(
                legend_lines,
                [line.get_label() for line in legend_lines],
                loc="best",
            )
        self.vol_fig.autofmt_xdate()

    def _build_top_moves_panel(self, parent: tk.Frame) -> None:
        columns = (
            "time",
            "um_move_pct",
            "spot_move_pct",
            "pnl_change",
            "gamma_l",
            "gamma_pnl",
            "capped_gamma_pnl",
            "gamma_pnl_diff",
        )
        self.top_moves_tree = ttk.Treeview(
            parent,
            columns=columns,
            show="headings",
            height=7,
        )
        headings = {
            "time": "Time",
            "um_move_pct": "UM Move",
            "spot_move_pct": "Spot Move",
            "pnl_change": "PnL Chg",
            "gamma_l": "Gamma in L",
            "gamma_pnl": "Gamma PnL",
            "capped_gamma_pnl": "Capped Gamma PnL",
            "gamma_pnl_diff": "Diff",
        }
        widths = {
            "time": 105,
            "um_move_pct": 90,
            "spot_move_pct": 90,
            "pnl_change": 95,
            "gamma_l": 95,
            "gamma_pnl": 100,
            "capped_gamma_pnl": 130,
            "gamma_pnl_diff": 90,
        }
        for column in columns:
            self.top_moves_tree.heading(column, text=headings[column])
            self.top_moves_tree.column(column, width=widths[column], anchor="center")
        self.top_moves_tree.pack(fill="y", expand=False, padx=6, pady=6)

    def _render_top_universal_mid_moves(self) -> None:
        if self.top_moves_tree is None:
            return

        for item in self.top_moves_tree.get_children():
            self.top_moves_tree.delete(item)
        for row in self._large_universal_mid_moves():
            self.top_moves_tree.insert(
                "",
                "end",
                values=(
                    row["time"],
                    row["um_move_pct"],
                    row["spot_move_pct"],
                    row["pnl_change"],
                    row["gamma_l"],
                    row["gamma_pnl"],
                    row["capped_gamma_pnl"],
                    row["gamma_pnl_diff"],
                ),
            )

    def _large_universal_mid_moves(self) -> list[dict[str, str]]:
        pnl_by_timestamp = {timestamp: pnl for timestamp, pnl in self.total_pnl_points}
        spot_by_timestamp = {timestamp: close for timestamp, close in self.points}
        gamma_by_timestamp = {timestamp: gamma_l for timestamp, gamma_l in self.gamma_l_points}
        moves = []
        for (previous_time, previous_mid), (timestamp, mid) in zip(
            self.universal_mid_points,
            self.universal_mid_points[1:],
        ):
            if not is_top_move_time(timestamp) or previous_mid <= 0:
                continue
            move_return = mid / previous_mid - 1
            move_pct = move_return * 100
            if abs(move_return) <= 0.001:
                continue
            previous_pnl = pnl_by_timestamp.get(previous_time)
            total_pnl = pnl_by_timestamp.get(timestamp)
            pnl_change = (
                total_pnl - previous_pnl
                if total_pnl is not None and previous_pnl is not None
                else None
            )
            previous_spot = spot_by_timestamp.get(previous_time)
            spot = spot_by_timestamp.get(timestamp)
            spot_move_pct = (
                (spot / previous_spot - 1) * 100
                if spot is not None and previous_spot not in (None, 0)
                else None
            )
            gamma_l = gamma_by_timestamp.get(timestamp)
            gamma_pnl = (
                0.5 * (gamma_l * 100000 * 10) * move_return * move_return * 100 / 1000
                if gamma_l is not None
                else None
            )
            capped_gamma_pnl = (
                capped_gamma_pnl_for_move(gamma_l, move_return)
                if gamma_l is not None
                else None
            )
            gamma_pnl_diff = (
                capped_gamma_pnl - gamma_pnl
                if gamma_pnl is not None and capped_gamma_pnl is not None
                else None
            )
            moves.append(
                {
                    "abs_move_pct": abs(move_pct),
                    "time": f"{previous_time:%H:%M}-{timestamp:%H:%M}",
                    "um_move_pct": f"{move_pct:.3f}%",
                    "spot_move_pct": (
                        f"{spot_move_pct:.3f}%"
                        if spot_move_pct is not None
                        else "--"
                    ),
                    "pnl_change": (
                        format_number(pnl_change, 2)
                        if pnl_change is not None
                        else "--"
                    ),
                    "gamma_l": (
                        format_number(gamma_l, 2)
                        if gamma_l is not None
                        else "--"
                    ),
                    "gamma_pnl": (
                        format_number(gamma_pnl, 2)
                        if gamma_pnl is not None
                        else "--"
                    ),
                    "capped_gamma_pnl": (
                        format_number(capped_gamma_pnl, 2)
                        if capped_gamma_pnl is not None
                        else "--"
                    ),
                    "gamma_pnl_diff": (
                        format_number(gamma_pnl_diff, 2)
                        if gamma_pnl_diff is not None
                        else "--"
                    ),
                    "gamma_pnl_diff_value": gamma_pnl_diff,
                }
            )
        moves.sort(key=lambda row: row["abs_move_pct"], reverse=True)
        large_moves = moves
        diff_total = sum(
            row["gamma_pnl_diff_value"]
            for row in large_moves
            if isinstance(row["gamma_pnl_diff_value"], (int, float))
        )
        if large_moves:
            large_moves.append(
                {
                    "abs_move_pct": -1,
                    "time": "Total",
                    "um_move_pct": "",
                    "spot_move_pct": "",
                    "pnl_change": "",
                    "gamma_l": "",
                    "gamma_pnl": "",
                    "capped_gamma_pnl": "",
                    "gamma_pnl_diff": format_number(diff_total, 2),
                    "gamma_pnl_diff_value": diff_total,
                }
            )
        return large_moves

    def gamma_diff_total(self) -> float | None:
        rows = self._large_universal_mid_moves()
        for row in reversed(rows):
            if row["time"] == "Total":
                value = row["gamma_pnl_diff_value"]
                return value if isinstance(value, (int, float)) else None
        return None


class SpotUniversalMidTab:
    def __init__(
        self,
        parent: tk.Frame,
        points: list[tuple[datetime, float]],
        universal_mid_points: list[tuple[datetime, float]],
        underlying: str = "NIFTY",
    ) -> None:
        self.parent = parent
        self.points = points
        self.universal_mid_points = universal_mid_points
        self.underlying = underlying
        self.fig: Figure
        self.ax = None
        self.canvas = None
        self.fig, self.ax, self.canvas = build_plot(self.parent)

    def render(self, current_timestamp: datetime) -> None:
        if self.ax is None or self.canvas is None:
            return

        self.ax.clear()
        apply_axis_bg(self.ax, None)
        self._render_spot_series(current_timestamp)
        self._render_universal_mid_series()
        self.ax.axvline(current_timestamp, color="#d32f2f", linestyle="--", linewidth=1)
        self.ax.set_title(f"{self.underlying} Spot Close and Universal Mid vs Time")
        self.ax.set_xlabel("Time IST")
        self.ax.set_ylabel("Price")
        self.ax.grid(True)
        if self.points or self.universal_mid_points:
            self.ax.legend()
        self.fig.autofmt_xdate()
        self.canvas.draw()

    def _render_spot_series(self, current_timestamp: datetime) -> None:
        if not self.points:
            return

        times = [timestamp for timestamp, _ in self.points]
        closes = [close for _, close in self.points]
        elapsed_points = [
            (timestamp, close)
            for timestamp, close in self.points
            if timestamp <= current_timestamp
        ]
        self.ax.plot(times, closes, color="#b8b8b8", linewidth=1.1, label="Spot full day")
        if not elapsed_points:
            return

        elapsed_times = [timestamp for timestamp, _ in elapsed_points]
        elapsed_closes = [close for _, close in elapsed_points]
        self.ax.plot(
            elapsed_times,
            elapsed_closes,
            color="#1769aa",
            linewidth=1.8,
            label="Spot close",
        )
        self.ax.scatter(
            [elapsed_times[-1]],
            [elapsed_closes[-1]],
            color="#1769aa",
            s=24,
            zorder=3,
        )

    def _render_universal_mid_series(self) -> None:
        if not self.universal_mid_points:
            return

        times = [timestamp for timestamp, _ in self.universal_mid_points]
        mids = [mid for _, mid in self.universal_mid_points]
        self.ax.plot(
            times,
            mids,
            color="#d32f2f",
            linewidth=1.8,
            label="Universal mid",
        )
        self.ax.scatter(
            [times[-1]],
            [mids[-1]],
            color="#d32f2f",
            s=24,
            zorder=3,
        )


class PortfolioRiskTab:
    def __init__(self, parent: tk.Frame, hedges_tab: "HedgesTab | None" = None) -> None:
        self.parent = parent
        self.hedges_tab = hedges_tab
        self.status_var = tk.StringVar(value="Waiting for portfolio")
        self.options_pnl_var = tk.StringVar(value="Options PnL: ")
        self.hedge_pnl_var = tk.StringVar(value="Delta Hedge PnL: ")
        self.total_pnl_var = tk.StringVar(value="Total PnL: ")
        self.final_delta_var = tk.StringVar(value="Final BS Delta: ")
        self.hedge_status_var = tk.StringVar(value="Hedge starts at 09:20")
        self.full_day_vol_var = tk.StringVar(
            value="Full-Day Vol Spot: -- | Full-Day Vol Universal Mid: --"
        )
        self.cell_labels: dict[tuple[int, int], tk.Label] = {}
        self.section_labels: dict[int, tk.Label] = {}
        self.rendered_body_rows = 0
        self.options_pv_snapshot: float | None = None
        self.hedge_strike: int | None = None
        self.hedge_trades: list[dict[str, float | str]] = []
        self.cumulative_hedge_lots = 0.0
        self.current_positions: list = []
        self.last_total_pnl: float | None = None
        self.last_gamma_l: float | None = None
        self.data_writer = PortfolioDataWriter(BACKTEST_OUTPUT_DIR)
        self.columns = [
            "Lots",
            "Book",
            "Underlying",
            "Maturity",
            "Strike",
            "Type",
            "Qty",
            "Mult",
            "Time Value",
            "Price Fit",
            "Bid Mkt",
            "Ask Mkt",
            "Mid Mkt",
            "Universal Spot",
            "Universal Synthetic Mid",
            "IV Model Used",
            "Time",
            "BS % Delta",
            "BS Delta (L)",
            "BS Delta Lots",
            "Skew Delta Lots",
            "Net Delta",
            "Gamma (L)",
            "Gamma Lots",
            "Vega",
            "BS Theta",
            "Std 1w Vega",
        ]
        self.column_widths = [
            7,
            8,
            10,
            10,
            8,
            5,
            8,
            6,
            9,
            9,
            8,
            8,
            8,
            11,
            14,
            9,
            8,
            9,
            10,
            10,
            12,
            10,
            10,
            10,
            10,
            10,
            10,
        ]
        self._build_ui()

    def _build_ui(self) -> None:
        tk.Label(
            self.parent,
            textvariable=self.status_var,
            anchor="w",
            padx=8,
            pady=6,
            font=("Arial", 10, "bold"),
        ).pack(fill="x")
        pnl_frame = tk.Frame(self.parent)
        pnl_frame.pack(fill="x", padx=8, pady=(0, 6))
        for var in (
            self.options_pnl_var,
            self.hedge_pnl_var,
            self.total_pnl_var,
            self.final_delta_var,
            self.hedge_status_var,
            self.full_day_vol_var,
        ):
            tk.Label(
                pnl_frame,
                textvariable=var,
                anchor="w",
                padx=8,
                font=("Arial", 10),
            ).pack(side="left")

        container = tk.Frame(self.parent)
        container.pack(fill="both", expand=True)

        self.canvas = tk.Canvas(container, highlightthickness=0)
        y_scroll = ttk.Scrollbar(container, orient="vertical", command=self.canvas.yview)
        x_scroll = ttk.Scrollbar(container, orient="horizontal", command=self.canvas.xview)
        self.canvas.configure(
            yscrollcommand=y_scroll.set,
            xscrollcommand=x_scroll.set,
        )

        self.grid = tk.Frame(self.canvas)
        self.canvas.create_window((0, 0), window=self.grid, anchor="nw")
        self.grid.bind(
            "<Configure>",
            lambda event: self.canvas.configure(scrollregion=self.canvas.bbox("all")),
        )

        self.canvas.grid(row=0, column=0, sticky="nsew")
        y_scroll.grid(row=0, column=1, sticky="ns")
        x_scroll.grid(row=1, column=0, sticky="ew")
        container.grid_rowconfigure(0, weight=1)
        container.grid_columnconfigure(0, weight=1)
        self._render_header()

    def set_status(self, status: str) -> None:
        self.status_var.set(status)

    def render(
        self,
        result: AnalyticsResult,
        risk_rows: list,
        positions: list,
        expiry_name: str,
        timestamp: datetime | None,
        risk_free_rate: float,
        funding_rate: float,
        brokerage_rate: float,
        hedge_threshold: float,
        full_day_spot_vol: float | None,
        full_day_mid_vol: float | None,
    ) -> None:
        self.current_positions = positions
        time_text = format_ist_timestamp(timestamp)
        self.status_var.set(
            f"{expiry_name} | Slice IST: {time_text} | "
            f"Universal Mid: {format_number(result.universal_mid, 2)} | "
            f"Positions: {len(positions)}"
        )
        self.full_day_vol_var.set(
            f"Full-Day Vol Spot: {format_percent_or_dash(full_day_spot_vol)} | "
            f"Full-Day Vol Universal Mid: {format_percent_or_dash(full_day_mid_vol)}"
        )
        required_body_rows = len(risk_rows) + 2
        self._ensure_body_rows(required_body_rows)
        row_index = 1
        self._set_section_title(row_index, "Options Book")
        row_index += 1
        rows_by_strike = {row.strike: row for row in result.rows}
        numeric_rows = []
        for row in risk_rows:
            values = self._row_values(row, result, rows_by_strike, risk_free_rate)
            numeric_values = self._row_numeric_values(
                row,
                result,
                rows_by_strike,
                risk_free_rate,
            )
            numeric_rows.append(numeric_values)
            self._set_row(
                row_index,
                values,
                numeric_values,
            )
            row_index += 1

        totals = ()
        options_pv = weighted_price_total(risk_rows, "mid_mkt")
        self.last_gamma_l = None
        if risk_rows:
            totals = self._total_row_numeric_values(risk_rows, result, numeric_rows)
            self.last_gamma_l = (
                totals[22] if isinstance(totals[22], (int, float)) else None
            )
            self._set_row(
                row_index,
                self._total_row_values(risk_rows, result, numeric_rows),
                totals,
                background="#cfe8ff",
                bold=True,
            )
            row_index += 1
        self._hide_unused_rows(row_index)
        self._update_pnl(
            result,
            rows_by_strike,
            timestamp,
            options_pv,
            totals[19] if totals else None,
            funding_rate,
            brokerage_rate,
            hedge_threshold,
        )

    def _render_header(self) -> None:
        for col_index, col in enumerate(self.columns):
            tk.Label(
                self.grid,
                text=col,
                borderwidth=1,
                relief="solid",
                padx=6,
                pady=4,
                width=self.column_widths[col_index],
                anchor="center",
                font=("Arial", 9, "bold"),
                bg="#f0f0f0",
            ).grid(row=0, column=col_index, sticky="nsew")

    def _render_section_title(self, row_index: int, title: str) -> None:
        tk.Label(
            self.grid,
            text=title,
            borderwidth=1,
            relief="solid",
            padx=6,
            pady=4,
            anchor="w",
            bg="#f7f7f7",
            font=("Arial", 9, "bold"),
        ).grid(
            row=row_index,
            column=0,
            columnspan=len(self.columns),
            sticky="nsew",
        )

    def _ensure_body_rows(self, required_body_rows: int) -> None:
        if required_body_rows <= self.rendered_body_rows:
            return

        for row_index in range(self.rendered_body_rows + 1, required_body_rows + 1):
            for col_index in range(len(self.columns)):
                label = tk.Label(
                    self.grid,
                    text="",
                    borderwidth=1,
                    relief="solid",
                    padx=3,
                    pady=3,
                    width=self.column_widths[col_index],
                    anchor="center"
                    if col_index in (0, 1, 2, 3, 4, 5, 6, 7)
                    else "e",
                    bg="white",
                    font=("Arial", 9),
                )
                label.grid(row=row_index, column=col_index, sticky="nsew")
                self.cell_labels[(row_index, col_index)] = label
        self.rendered_body_rows = required_body_rows

    def _set_section_title(self, row_index: int, title: str) -> None:
        label = self.section_labels.get(row_index)
        if label is None:
            label = tk.Label(
                self.grid,
                text=title,
                borderwidth=1,
                relief="solid",
                padx=6,
                pady=4,
                anchor="w",
                bg="#f7f7f7",
                font=("Arial", 9, "bold"),
            )
            self.section_labels[row_index] = label
        label.config(text=title)
        label.grid(
            row=row_index,
            column=0,
            columnspan=len(self.columns),
            sticky="nsew",
        )
        for col_index in range(len(self.columns)):
            cell = self.cell_labels.get((row_index, col_index))
            if cell is not None:
                cell.grid_remove()

    def _set_row(
        self,
        row_index: int,
        values: tuple,
        numeric_values: tuple,
        background: str = "white",
        bold: bool = False,
    ) -> None:
        section_label = self.section_labels.get(row_index)
        if section_label is not None:
            section_label.grid_remove()

        for col_index, value in enumerate(values):
            label = self.cell_labels[(row_index, col_index)]
            label.config(
                text=value,
                fg="red" if is_negative(numeric_values[col_index]) else "black",
                bg=background,
                font=("Arial", 9, "bold") if bold else ("Arial", 9),
            )
            label.grid(row=row_index, column=col_index, sticky="nsew")

    def _hide_unused_rows(self, first_unused_row: int) -> None:
        for row_index in range(first_unused_row, self.rendered_body_rows + 1):
            section_label = self.section_labels.get(row_index)
            if section_label is not None:
                section_label.grid_remove()
            for col_index in range(len(self.columns)):
                label = self.cell_labels.get((row_index, col_index))
                if label is not None:
                    label.grid_remove()

    def _row_values(
        self,
        row,
        result: AnalyticsResult,
        rows_by_strike: dict,
        risk_free_rate: float,
    ) -> tuple:
        base_values = risk_row_values(row, result)
        skew_delta_lots = self._skew_delta_lots(
            row,
            result,
            rows_by_strike,
            risk_free_rate,
        )
        net_delta = difference_or_none(row.bs_delta_lots, skew_delta_lots)
        return insert_columns(
            base_values,
            20,
            (
                format_optional_number(skew_delta_lots),
                format_optional_number(net_delta),
            ),
        )

    def _row_numeric_values(
        self,
        row,
        result: AnalyticsResult,
        rows_by_strike: dict,
        risk_free_rate: float,
    ) -> tuple:
        base_values = risk_row_numeric_values(row, result)
        skew_delta_lots = self._skew_delta_lots(
            row,
            result,
            rows_by_strike,
            risk_free_rate,
        )
        net_delta = difference_or_none(row.bs_delta_lots, skew_delta_lots)
        return insert_columns(
            base_values,
            20,
            (
                "" if skew_delta_lots is None else skew_delta_lots,
                "" if net_delta is None else net_delta,
            ),
        )

    def _total_row_values(
        self,
        risk_rows: list,
        result: AnalyticsResult,
        numeric_rows: list[tuple],
    ) -> tuple:
        base_values = risk_total_row_values(risk_rows, result, label="Master Total")
        totals = self._total_row_numeric_values(risk_rows, result, numeric_rows)
        return insert_columns(
            base_values,
            20,
            (
                format_optional_number(totals[20]),
                format_optional_number(totals[21]),
            ),
        )

    def _total_row_numeric_values(
        self,
        risk_rows: list,
        result: AnalyticsResult,
        numeric_rows: list[tuple],
    ) -> tuple:
        base_values = risk_total_row_numeric_values(risk_rows, result)
        skew_total = sum_numeric_column(numeric_rows, 20)
        net_total = sum_numeric_column(numeric_rows, 21)
        return insert_columns(base_values, 20, (skew_total, net_total))

    def _skew_delta_lots(
        self,
        row,
        result: AnalyticsResult,
        rows_by_strike: dict,
        risk_free_rate: float,
    ):
        market_row = rows_by_strike.get(row.strike)
        if market_row is None or market_row.model_iv is None:
            return None

        local_vol_skew = calculate_local_vol_skew(result, market_row.normalized_strike)
        atm_vol_skew = calculate_atm_vol_skew(result)
        surf_delta = calculate_surf_delta(
            result,
            row.strike,
            market_row.model_iv,
            local_vol_skew,
            risk_free_rate,
        )
        atm_skew_delta = calculate_surf_delta(
            result,
            row.strike,
            market_row.model_iv,
            atm_vol_skew,
            risk_free_rate,
        )
        skew_delta = difference_or_none(surf_delta, atm_skew_delta)
        if skew_delta is None or not row.mult or not result.universal_spot:
            return None
        return skew_delta * row.qty / (result.universal_spot * row.mult)

    def _update_pnl(
        self,
        result: AnalyticsResult,
        rows_by_strike: dict,
        timestamp: datetime | None,
        options_pv: float,
        options_bs_delta_lots: float | str | None,
        funding_rate: float,
        brokerage_rate: float,
        hedge_threshold: float,
    ) -> None:
        if timestamp is None or not isinstance(options_bs_delta_lots, (int, float)):
            self._render_pnl_values(None, None)
            return

        if self.options_pv_snapshot is None and is_hedge_start_time(timestamp):
            self.options_pv_snapshot = options_pv
            self.hedge_strike = nearest_result_strike(result, result.universal_mid)
            self._add_hedge_trade(
                round(-options_bs_delta_lots),
                result,
                rows_by_strike,
                timestamp,
                funding_rate,
                brokerage_rate,
                reason="Initial hedge",
            )
        elif self.options_pv_snapshot is not None and self.hedge_strike is not None:
            combined_delta = options_bs_delta_lots + self.cumulative_hedge_lots
            if abs(combined_delta) > hedge_threshold:
                self._add_hedge_trade(
                    round(-combined_delta),
                    result,
                    rows_by_strike,
                    timestamp,
                    funding_rate,
                    brokerage_rate,
                    reason="Threshold hedge",
                )

        options_pnl = (
            options_pv - self.options_pv_snapshot
            if self.options_pv_snapshot is not None
            else None
        )
        self.final_delta_var.set(
            "Final BS Delta: "
            f"{format_number(options_bs_delta_lots + self.cumulative_hedge_lots, 2)}"
        )
        hedge_pnl = self._hedge_pnl(result, rows_by_strike, funding_rate, brokerage_rate)
        self._render_pnl_values(options_pnl, hedge_pnl)
        hedge_rows = self._hedge_trade_rows(
            result,
            rows_by_strike,
            funding_rate,
            brokerage_rate,
        )
        self.data_writer.write_cycle(
            timestamp,
            result,
            options_pv,
            self.options_pv_snapshot,
            options_pnl,
            options_bs_delta_lots,
            self.cumulative_hedge_lots,
            options_bs_delta_lots + self.cumulative_hedge_lots,
            self.hedge_strike,
            hedge_pnl,
            len(self.hedge_trades),
        )
        self.data_writer.write_hedge_trades(hedge_rows)
        if self.hedges_tab is not None:
            self.hedges_tab.render(hedge_rows, hedge_pnl)
        if self.hedge_strike is None:
            self.hedge_status_var.set("Hedge starts at 09:20")
        else:
            self.hedge_status_var.set(
                f"Hedge Strike: {self.hedge_strike} | "
                f"Hedge Lots: {format_number(self.cumulative_hedge_lots, 2)} | "
                f"Trades: {len(self.hedge_trades)}"
            )

    def _add_hedge_trade(
        self,
        lots_change: float,
        result: AnalyticsResult,
        rows_by_strike: dict,
        timestamp: datetime,
        funding_rate: float,
        brokerage_rate: float,
        reason: str,
    ) -> None:
        if self.hedge_strike is None or abs(lots_change) < 1e-9:
            return

        prices = hedge_prices(
            self.hedge_strike,
            result,
            rows_by_strike,
            funding_rate,
            brokerage_rate,
        )
        if prices is None:
            return
        synth_bid, synth_ask, _ = prices
        trade_price = synth_ask if lots_change > 0 else synth_bid
        self.hedge_trades.append(
            {
                "id": len(self.hedge_trades) + 1,
                "timestamp": format_ist_timestamp(timestamp),
                "strike": self.hedge_strike,
                "lots_change": lots_change,
                "trade_price": trade_price,
                "reason": reason,
            }
        )
        self.cumulative_hedge_lots += lots_change

    def _hedge_pnl(
        self,
        result: AnalyticsResult,
        rows_by_strike: dict,
        funding_rate: float,
        brokerage_rate: float,
    ) -> float | None:
        if self.hedge_strike is None or not self.hedge_trades:
            return None
        prices = hedge_prices(
            self.hedge_strike,
            result,
            rows_by_strike,
            funding_rate,
            brokerage_rate,
        )
        if prices is None:
            return None
        _, _, synth_mid = prices
        multiplier = hedge_multiplier(self.current_positions)
        pnl = 0.0
        for trade in self.hedge_trades:
            pnl += float(trade["lots_change"]) * (
                synth_mid - float(trade["trade_price"])
            ) * multiplier / 1000
        return pnl

    def _hedge_trade_rows(
        self,
        result: AnalyticsResult,
        rows_by_strike: dict,
        funding_rate: float,
        brokerage_rate: float,
    ) -> list[dict[str, float | str]]:
        if self.hedge_strike is None:
            return []
        prices = hedge_prices(
            self.hedge_strike,
            result,
            rows_by_strike,
            funding_rate,
            brokerage_rate,
        )
        if prices is None:
            return []

        _, _, synth_mid = prices
        rows = []
        for trade in self.hedge_trades:
            lots_change = float(trade["lots_change"])
            trade_price = float(trade["trade_price"])
            pnl = (
                lots_change
                * (synth_mid - trade_price)
                * hedge_multiplier(self.current_positions)
                / 1000
            )
            rows.append(
                {
                    "timestamp": trade["timestamp"],
                    "id": trade["id"],
                    "reason": trade["reason"],
                    "strike": trade["strike"],
                    "side": "BUY" if lots_change > 0 else "SELL",
                    "lots_change": lots_change,
                    "trade_price": trade_price,
                    "live_synthetic_mid": synth_mid,
                    "pnl": pnl,
                }
            )
        return rows

    def _render_pnl_values(
        self,
        options_pnl: float | None,
        hedge_pnl: float | None,
    ) -> None:
        total = (options_pnl or 0.0) + (hedge_pnl or 0.0)
        self.last_total_pnl = total
        self.options_pnl_var.set(f"Options PnL: {format_optional_number(options_pnl)}")
        self.hedge_pnl_var.set(f"Delta Hedge PnL: {format_optional_number(hedge_pnl)}")
        self.total_pnl_var.set(f"Total PnL: {format_number(total, 2)}")


class FrozenIvPortfolioTab(PortfolioRiskTab):
    def __init__(self, parent: tk.Frame) -> None:
        super().__init__(parent, hedges_tab=None)
        self.frozen_iv: float | None = None
        self.frozen_timestamp: datetime | None = None
        self.frozen_positions: list | None = None
        self.data_writer = NullPortfolioDataWriter()
        self.status_var.set("Waiting for 09:20 frozen-IV snapshot")
        self.full_day_vol_var.set("Frozen IV: --")

    def render(
        self,
        session: ExpirySession,
        result: AnalyticsResult,
        timestamp: datetime | None,
        funding_rate: float,
        brokerage_rate: float,
        hedge_threshold: float,
    ) -> None:
        if timestamp is None:
            self.status_var.set("Waiting for replay timestamp")
            return
        if self.frozen_iv is None:
            if not is_hedge_start_time(timestamp):
                self.status_var.set("Waiting for 09:20 frozen-IV snapshot")
                return
            self.frozen_iv = result.atm_vol
            self.frozen_timestamp = timestamp
            portfolio = build_sample_portfolio(session, result)
            if portfolio is None:
                self.status_var.set("Waiting for enough strikes to freeze portfolio")
                self.frozen_iv = None
                self.frozen_timestamp = None
                return
            self.frozen_positions = portfolio.positions

        positions = self.frozen_positions or []
        risk_rows = self._frozen_risk_rows(positions, result, funding_rate)
        super().render(
            result,
            risk_rows,
            positions,
            f"{session.spec.tab_name} Frozen IV",
            timestamp,
            result.time,
            funding_rate,
            brokerage_rate,
            hedge_threshold,
            None,
            None,
        )
        self.full_day_vol_var.set(
            f"Frozen IV: {format_percent_or_dash(self.frozen_iv)} | "
            f"Frozen at: {format_ist_timestamp(self.frozen_timestamp)}"
        )

    def _row_values(
        self,
        row,
        result: AnalyticsResult,
        rows_by_strike: dict,
        risk_free_rate: float,
    ) -> tuple:
        return insert_columns(risk_row_values(row, result), 20, ("", ""))

    def _row_numeric_values(
        self,
        row,
        result: AnalyticsResult,
        rows_by_strike: dict,
        risk_free_rate: float,
    ) -> tuple:
        return insert_columns(risk_row_numeric_values(row, result), 20, ("", ""))

    def _update_pnl(
        self,
        result: AnalyticsResult,
        rows_by_strike: dict,
        timestamp: datetime | None,
        options_pv: float,
        options_bs_delta_lots: float | str | None,
        funding_rate: float,
        brokerage_rate: float,
        hedge_threshold: float,
    ) -> None:
        if timestamp is None or not isinstance(options_bs_delta_lots, (int, float)):
            self._render_pnl_values(None, None)
            return

        if self.options_pv_snapshot is None:
            self.options_pv_snapshot = options_pv
            self.hedge_strike = nearest_result_strike(result, result.universal_mid)
            self._add_hedge_trade(
                round(-options_bs_delta_lots),
                result,
                rows_by_strike,
                timestamp,
                funding_rate,
                brokerage_rate,
                reason="Initial frozen-IV hedge",
            )
        elif self.hedge_strike is not None:
            combined_delta = options_bs_delta_lots + self.cumulative_hedge_lots
            if abs(combined_delta) > hedge_threshold:
                self._add_hedge_trade(
                    round(-combined_delta),
                    result,
                    rows_by_strike,
                    timestamp,
                    funding_rate,
                    brokerage_rate,
                    reason="Frozen-IV threshold hedge",
                )

        options_pnl = (
            options_pv - self.options_pv_snapshot
            if self.options_pv_snapshot is not None
            else None
        )
        self.final_delta_var.set(
            "Final BS Delta: "
            f"{format_number(options_bs_delta_lots + self.cumulative_hedge_lots, 2)}"
        )
        hedge_pnl = self._hedge_pnl(result, rows_by_strike, funding_rate, brokerage_rate)
        self._render_pnl_values(options_pnl, hedge_pnl)
        if self.hedge_strike is None:
            self.hedge_status_var.set("Hedge starts at 09:20")
        else:
            self.hedge_status_var.set(
                f"Hedge Strike: {self.hedge_strike} | "
                f"Hedge Lots: {format_number(self.cumulative_hedge_lots, 2)} | "
                f"Trades: {len(self.hedge_trades)}"
            )

    def _frozen_risk_rows(
        self,
        positions: list,
        result: AnalyticsResult,
        funding_rate: float,
    ) -> list[RiskRow]:
        if self.frozen_iv is None:
            return []
        rows_by_strike = {row.strike: row for row in result.rows}
        risk_rows = []
        for position in positions:
            market_row = rows_by_strike.get(position.strike)
            if market_row is None:
                risk_rows.append(self._blank_frozen_row(position))
                continue
            risk_rows.append(self._frozen_position_row(position, result, funding_rate))
        return risk_rows

    def _frozen_position_row(
        self,
        position,
        result: AnalyticsResult,
        funding_rate: float,
    ) -> RiskRow:
        frozen_iv = self.frozen_iv or 0.0
        vol_pct = frozen_iv * 100
        rate = funding_rate
        spot = result.universal_spot
        time = result.time
        qty = position.qty
        mult = position.mult

        price_fit = black_scholes_price(
            spot,
            position.strike,
            time,
            rate,
            frozen_iv,
            position.option_type,
        )
        intrinsic_value = intrinsic_value_from_synthetic_mid(
            position.option_type,
            result.universal_mid,
            position.strike,
        )
        time_value = price_fit - intrinsic_value
        delta = black_scholes_delta(
            spot,
            position.strike,
            time,
            rate,
            frozen_iv,
            position.option_type,
        )
        gamma = black_scholes_gamma(spot, position.strike, time, rate, frozen_iv)
        vega = black_scholes_vega(spot, position.strike, time, rate, frozen_iv)
        time_unit = (15 / 375) * 0.4 / 255.5
        theta_price_change = black_scholes_theta_price_change(
            spot,
            position.strike,
            time,
            time_unit,
            rate,
            frozen_iv,
            position.option_type,
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
            option_type=position.option_type,
            qty=qty,
            mult=mult,
            time_value=time_value,
            price_fit=price_fit,
            bid_mkt=price_fit,
            ask_mkt=price_fit,
            mid_mkt=price_fit,
            model_iv=vol_pct,
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
    def _blank_frozen_row(position) -> RiskRow:
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


class NullPortfolioDataWriter:
    def write_cycle(self, *args, **kwargs) -> None:
        return

    def write_hedge_trades(self, rows: list[dict[str, float | str]]) -> None:
        return


class HedgesTab:
    def __init__(self, parent: tk.Frame) -> None:
        self.parent = parent
        self.status_var = tk.StringVar(value="No hedge trades yet")
        self.columns = [
            "Timestamp",
            "Reason",
            "Strike",
            "Side",
            "Lots",
            "Trade Price",
            "Live Synthetic Mid",
            "PnL",
        ]
        self._build_ui()

    def _build_ui(self) -> None:
        tk.Label(
            self.parent,
            textvariable=self.status_var,
            anchor="w",
            padx=8,
            pady=6,
            font=("Arial", 10, "bold"),
        ).pack(fill="x")

        self.tree = ttk.Treeview(self.parent, columns=self.columns, show="headings")
        for column in self.columns:
            self.tree.heading(column, text=column)
            width = 160 if column in {"Timestamp", "Reason"} else 130
            self.tree.column(column, width=width, anchor="center")
        self.tree.pack(fill="both", expand=True, padx=8, pady=(0, 8))

    def render(
        self,
        rows: list[dict[str, float | str]],
        hedge_pnl: float | None,
    ) -> None:
        for item in self.tree.get_children():
            self.tree.delete(item)

        if not rows:
            self.status_var.set("No hedge trades yet")
            return

        total_pnl = sum(float(row["pnl"]) for row in rows)
        self.status_var.set(
            f"Hedge trades: {len(rows)} | "
            f"Total Hedge PnL: {format_optional_number(hedge_pnl)}"
        )
        for row in rows:
            self.tree.insert(
                "",
                "end",
                values=(
                    row["timestamp"],
                    row["reason"],
                    row["strike"],
                    row["side"],
                    format_number(float(row["lots_change"]), 0),
                    format_number(float(row["trade_price"]), 2),
                    format_number(float(row["live_synthetic_mid"]), 2),
                    format_number(float(row["pnl"]), 2),
                ),
            )
        self.tree.insert(
            "",
            "end",
            values=(
                "Total",
                "",
                "",
                "",
                "",
                "",
                "",
                format_number(total_pnl, 2),
            ),
        )


class PortfolioDataWriter:
    def __init__(self, output_dir: Path) -> None:
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.options_path = self.output_dir / "portfolio_options_pv.csv"
        self.hedges_path = self.output_dir / "portfolio_hedges.csv"
        for path in (self.options_path, self.hedges_path):
            if path.exists():
                path.unlink()
        self.last_cycle_timestamp: str | None = None
        self.written_trade_ids: set[int] = set()

    def write_cycle(
        self,
        timestamp: datetime,
        result: AnalyticsResult,
        options_pv: float,
        options_pv_snapshot: float | None,
        options_pnl: float | None,
        options_bs_delta_lots: float,
        cumulative_hedge_lots: float,
        final_bs_delta_lots: float,
        hedge_strike: int | None,
        hedge_pnl: float | None,
        hedge_trade_count: int,
    ) -> None:
        timestamp_text = format_ist_timestamp(timestamp)
        if self.last_cycle_timestamp == timestamp_text:
            return

        append_csv_row(
            self.options_path,
            [
                "timestamp",
                "universal_mid",
                "universal_spot",
                "atm_vol",
                "options_pv",
                "options_pv_snapshot",
                "options_pnl",
                "options_bs_delta_lots",
                "cumulative_hedge_lots",
                "final_bs_delta_lots",
                "hedge_strike",
                "hedge_pnl",
                "total_pnl",
                "hedge_trade_count",
            ],
            {
                "timestamp": timestamp_text,
                "universal_mid": result.universal_mid,
                "universal_spot": result.universal_spot,
                "atm_vol": result.atm_vol,
                "options_pv": options_pv,
                "options_pv_snapshot": "" if options_pv_snapshot is None else options_pv_snapshot,
                "options_pnl": "" if options_pnl is None else options_pnl,
                "options_bs_delta_lots": options_bs_delta_lots,
                "cumulative_hedge_lots": cumulative_hedge_lots,
                "final_bs_delta_lots": final_bs_delta_lots,
                "hedge_strike": "" if hedge_strike is None else hedge_strike,
                "hedge_pnl": "" if hedge_pnl is None else hedge_pnl,
                "total_pnl": (options_pnl or 0.0) + (hedge_pnl or 0.0),
                "hedge_trade_count": hedge_trade_count,
            },
        )
        self.last_cycle_timestamp = timestamp_text

    def write_hedge_trades(self, rows: list[dict[str, float | str]]) -> None:
        for row in rows:
            trade_id = int(row["id"])
            if trade_id in self.written_trade_ids:
                continue
            append_csv_row(
                self.hedges_path,
                [
                    "id",
                    "timestamp",
                    "reason",
                    "strike",
                    "side",
                    "lots_change",
                    "trade_price",
                    "live_synthetic_mid",
                    "pnl",
                ],
                row,
            )
            self.written_trade_ids.add(trade_id)


def append_csv_row(path: Path, fieldnames: list[str], row: dict) -> None:
    write_header = not path.exists() or path.stat().st_size == 0
    with path.open("a", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames, extrasaction="ignore")
        if write_header:
            writer.writeheader()
        writer.writerow(row)


def build_summary_table(parent, title, parameters, side, padx, bg=None):
    frame_style = {"bg": bg} if bg is not None else {}
    frame = tk.Frame(parent, **frame_style)
    frame.pack(side=side, padx=padx)
    tk.Label(frame, text=title, font=("Arial", 11, "bold"), **frame_style).pack()

    tree = ttk.Treeview(frame, columns=["Parameter", "Value"], show="headings", height=12)
    for column in ["Parameter", "Value"]:
        tree.heading(column, text=column)
        tree.column(column, width=180, anchor="center")
    tree.pack()

    items = {}
    for name in parameters:
        items[name] = tree.insert("", "end", values=[name, ""])
    return tree, items


def overview_underlyings(sessions: list[ExpirySession]) -> list[str]:
    seen = {session.spec.underlying for session in sessions}
    ordered = [underlying for underlying in ("NIFTY", "SENSEX") if underlying in seen]
    ordered.extend(sorted(seen - set(ordered)))
    return ordered


def build_plot(parent, bg=None):
    fig = Figure(figsize=(8, 6), dpi=100, facecolor=bg or "white")
    ax = fig.add_subplot(111)
    apply_axis_bg(ax, bg)
    canvas = FigureCanvasTkAgg(fig, master=parent)
    canvas.get_tk_widget().pack(fill="both", expand=True)
    return fig, ax, canvas


def apply_axis_bg(ax, bg) -> None:
    if bg is not None:
        ax.set_facecolor(bg)


def insert_columns(values: tuple, index: int, additions: tuple) -> tuple:
    return values[:index] + additions + values[index:]


def sum_numeric_column(rows: list[tuple], col_index: int) -> float | str:
    values = [row[col_index] for row in rows if isinstance(row[col_index], (int, float))]
    return sum(values) if values else ""


def is_hedge_start_time(timestamp: datetime) -> bool:
    return (timestamp.hour, timestamp.minute) >= (9, 20)


def is_top_move_time(timestamp: datetime) -> bool:
    return (9, 20) <= (timestamp.hour, timestamp.minute) <= (15, 24)


def capped_gamma_pnl_for_move(gamma_l: float, move_return: float) -> float:
    remaining_move = abs(move_return)
    capped_pnl = 0.0
    max_chunk = 0.001
    while remaining_move > 0:
        chunk = min(max_chunk, remaining_move)
        capped_pnl += 0.5 * (gamma_l * 100000 * 10) * chunk * chunk * 100 / 1000
        remaining_move -= chunk
    return capped_pnl


def annualized_running_vol(
    points: list[tuple[datetime, float]],
    calendar_days: float | None,
    intraday_var: float | None,
    window: int = 10,
) -> float | None:
    return annualized_vol(points, calendar_days, intraday_var, window)


def annualized_available_vol(
    points: list[tuple[datetime, float]],
    calendar_days: float | None,
    intraday_var: float | None,
) -> float | None:
    return annualized_vol(points, calendar_days, intraday_var, None)


def annualized_vol(
    points: list[tuple[datetime, float]],
    calendar_days: float | None,
    intraday_var: float | None,
    window: int | None,
) -> float | None:
    if calendar_days is None or intraday_var in (None, 0):
        return None

    variances = one_minute_variances(points)
    if window is not None:
        if len(variances) < window:
            return None
        variances = variances[-window:]
    if not variances:
        return None

    average_variance = sum(variances) / len(variances)
    annual_variance = average_variance * 375 * calendar_days / intraday_var
    if annual_variance < 0:
        return None
    return math.sqrt(annual_variance)


def one_minute_variances(points: list[tuple[datetime, float]]) -> list[float]:
    variances = []
    for (previous_timestamp, previous_value), (timestamp, value) in zip(
        points,
        points[1:],
    ):
        if (timestamp - previous_timestamp).total_seconds() != 60:
            continue
        if previous_value <= 0 or value <= 0:
            continue
        log_return = math.log(value / previous_value)
        variances.append(log_return * log_return)
    return variances


def nearest_result_strike(result: AnalyticsResult, value: float) -> int:
    strikes = [row.strike for row in result.rows]
    return min(strikes, key=lambda strike: (abs(strike - value), strike))


def hedge_prices(
    strike: int,
    result: AnalyticsResult,
    rows_by_strike: dict,
    funding_rate: float,
    brokerage_rate: float,
) -> tuple[float, float, float] | None:
    market_row = rows_by_strike.get(strike)
    if market_row is None:
        return None
    synth_bid, synth_ask = synthetic_prices(
        strike=strike,
        ce_bid=market_row.ce_bid,
        ce_ask=market_row.ce_ask,
        pe_bid=market_row.pe_bid,
        pe_ask=market_row.pe_ask,
        funding_rate=funding_rate,
        brokerage_rate=brokerage_rate,
        time=result.time,
    )
    return synth_bid, synth_ask, (synth_bid + synth_ask) / 2


def hedge_multiplier(positions: list[PortfolioPosition]) -> float:
    for position in positions:
        if position.mult:
            return float(position.mult)
    return 65.0


def calculate_atm_vol_skew(result: AnalyticsResult) -> float | None:
    if result.time <= 0 or result.atm_vol <= 0:
        return None
    if len(result.fitted_params) != 5:
        return None

    delta_ns_1pct = abs(log_1pct_spot() / ((result.time ** 0.5) * result.atm_vol))
    half_width = min(0.25, max(0.05, delta_ns_1pct))
    left_vol = model_vol_at_ns(result, -half_width)
    right_vol = model_vol_at_ns(result, half_width)
    if left_vol is None or right_vol is None:
        return None

    slope_per_ns = (right_vol - left_vol) / (2 * half_width)
    return slope_per_ns * delta_ns_1pct


def calculate_atm_vol_skew_2(result: AnalyticsResult) -> float | None:
    if result.time <= 0 or result.atm_vol <= 0:
        return None
    if len(result.fitted_params) != 5:
        return None

    a_fit = result.fitted_params[0]
    delta_ns_1pct = abs(log_1pct_spot() / ((result.time ** 0.5) * result.atm_vol))
    return -a_fit * (delta_ns_1pct / 0.1)


def calculate_local_vol_skew(
    result: AnalyticsResult,
    normalized_strike: float | None,
) -> float | None:
    if normalized_strike is None or result.time <= 0 or result.atm_vol <= 0:
        return None
    if len(result.fitted_params) != 5:
        return None

    a_fit, bl_fit, br_fit, capl_fit, floorr_fit = result.fitted_params
    model_slope = ParametricVolCurve.model_slope(
        normalized_strike,
        a_fit,
        bl_fit,
        br_fit,
        capl_fit,
        floorr_fit,
    )
    delta_ns_1pct = abs(log_1pct_spot() / ((result.time ** 0.5) * result.atm_vol))
    return -model_slope * (delta_ns_1pct / 0.1)


def calculate_surf_delta(
    result: AnalyticsResult,
    strike: int,
    model_iv: float | None,
    vol_skew: float | None,
    risk_free_rate: float,
) -> float | None:
    if model_iv is None or vol_skew is None:
        return None
    if result.time <= 0:
        return None

    vega = black_scholes_vega(
        result.universal_spot,
        strike,
        result.time,
        risk_free_rate,
        model_iv / 100,
    )
    return vol_skew * vega


def difference_or_none(left: float | None, right: float | None) -> float | None:
    if left is None or right is None:
        return None
    return left - right


def is_negative(value) -> bool:
    return isinstance(value, (int, float)) and value < 0


def calculate_snapshot_vol_beta(
    snapshot_mid: float | None,
    snapshot_atm_vol: float | None,
    live_result: AnalyticsResult | None,
) -> float | None:
    if snapshot_mid in (None, 0) or snapshot_atm_vol is None or live_result is None:
        return None
    spot_move_1pct_units = (live_result.universal_mid - snapshot_mid) / (
        snapshot_mid * 0.01
    )
    if spot_move_1pct_units == 0:
        return None
    vol_change_points = (live_result.atm_vol - snapshot_atm_vol) * 100
    return vol_change_points / spot_move_1pct_units


def calculate_snapshot_ssr(
    vol_beta: float | None,
    user_locked_skew: float | None,
) -> float | None:
    if vol_beta is None or user_locked_skew in (None, 0):
        return None
    return vol_beta / user_locked_skew


def percent_change(live_value: float | None, previous_value: float | None) -> float | None:
    if live_value is None or previous_value in (None, 0):
        return None
    return (live_value / previous_value) - 1


def vol_point_change(
    live_atm_vol: float | None,
    snapshot_atm_vol: float | None,
) -> float | None:
    if live_atm_vol is None or snapshot_atm_vol is None:
        return None
    return (live_atm_vol - snapshot_atm_vol) * 100


def parse_optional_float(value) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def format_ist_timestamp(value: datetime | None) -> str:
    if value is None:
        return ""
    return value.strftime("%Y-%m-%d %H:%M:%S")


def model_vol_at_ns(result: AnalyticsResult, ns: float) -> float | None:
    if result.atm_vol <= 0 or len(result.fitted_params) != 5:
        return None

    curve = ParametricVolCurve(result.fitted_params)
    return curve.build_model_vol_curve(
        [0.0, ns],
        result.atm_vol * 100,
        *result.fitted_params,
    )[1]


def log_1pct_spot() -> float:
    return 0.009950330853168092


def format_optional_number(value, decimals: int = 2) -> str:
    if value is None or value == "":
        return ""
    return format_number(value, decimals)


def format_cash(value) -> str:
    if value is None or value == "":
        return ""
    return format_number(value, 0, use_commas=True)


def format_percent(value) -> str:
    if value is None or value == "":
        return ""
    return f"{value * 100:.2f}%"


def format_percent_or_dash(value) -> str:
    if value is None or value == "":
        return "--"
    return format_percent(value)


def format_vol_pair(running_vol, full_day_vol) -> str:
    return f"{format_percent_or_dash(running_vol)}    ||    {format_percent_or_dash(full_day_vol)}"


def format_vol(value) -> str:
    if value is None or value == "":
        return ""
    return format_number(value * 100, 2)


def format_iv_diff(market_iv, model_iv) -> str:
    if market_iv == "" or model_iv is None:
        return ""
    return format_number(float(market_iv) - float(model_iv), 2)


def format_number(value, decimals: int = 2, use_commas: bool = False) -> str:
    if value == "":
        return ""
    if isinstance(value, int):
        if decimals > 0:
            return format(value, f',.{decimals}f' if use_commas else f'.{decimals}f')
        return format(value, ",d" if use_commas else "d")
    if isinstance(value, float):
        return format(value, f',.{decimals}f' if use_commas else f'.{decimals}f')
    return str(value)
