from __future__ import annotations

import csv
import tkinter as tk
from tkinter import ttk

from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure

from fit_sensex.config import AppConfig
from fit_sensex.market.instruments import OptionChain
from fit_sensex.market.kite_stream import MarketDataStore
from fit_sensex.models import AnalyticsResult
from fit_sensex.pricing.vol_curve import ParametricVolCurve
from fit_sensex.services.analytics import AnalyticsEngine
from fit_sensex.services.risk import RiskEngine


class FitSensexApp:
    def __init__(
        self,
        root: tk.Tk,
        config: AppConfig,
        chain: OptionChain,
        store: MarketDataStore,
        analytics: AnalyticsEngine,
    ) -> None:
        self.root = root
        self.config = config
        self.chain = chain
        self.store = store
        self.analytics = analytics
        self.risk_engine = RiskEngine(config.market)
        self.options_pv_snapshot: float | None = None
        self.options_pv_snapshot_label = "Not snapped"
        self.latest_risk_rows: list = []
        self.latest_result: AnalyticsResult | None = None

        self.root.title(
            f"{self.config.market.symbol_name} Dynamic Synthetic Futures + "
            "IV Market / IV Model Parametric Fit"
        )
        self._build_ui()

    def start(self) -> None:
        self.refresh()
        self.root.mainloop()

    def _build_ui(self) -> None:
        notebook = ttk.Notebook(self.root)
        notebook.pack(fill="both", expand=True)

        table_tab = tk.Frame(notebook)
        plot_tab = tk.Frame(notebook)
        iv_plot_tab = tk.Frame(notebook)
        error_tab = tk.Frame(notebook)
        cash_vol_tab = tk.Frame(notebook)
        risk_tab = tk.Frame(notebook)
        trades_tab = tk.Frame(notebook)
        pnl_tab = tk.Frame(notebook)

        notebook.add(table_tab, text="Option Chain")
        notebook.add(plot_tab, text="Slope Plot")
        notebook.add(iv_plot_tab, text="Normal Vol Surface")
        notebook.add(error_tab, text="Error Surface")
        notebook.add(cash_vol_tab, text="Cash Vol Surface")
        notebook.add(risk_tab, text="Risk")
        notebook.add(trades_tab, text="Hedge Trades")
        notebook.add(pnl_tab, text="PnL")

        top_tables_frame = tk.Frame(table_tab)
        top_tables_frame.pack(fill="x", pady=10)
        self.user_tree, self.user_items = self._build_summary_table(
            top_tables_frame,
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
        )
        self.market_tree, self.market_items = self._build_summary_table(
            top_tables_frame,
            "Market Variables",
            [
                "Best Synthetic Bid",
                "Best Synthetic Ask",
                "Universal Synthetic Mid",
                "Universal Spot",
                "ATM Vol",
                "Param a",
                "Param bL",
                "Param bR",
                "Param CapL",
                "Param FloorR",
                "Fit Error",
            ],
            side="left",
            padx=30,
        )

        self.tree = ttk.Treeview(
            table_tab,
            columns=[
                "Strike",
                "Norm Strike",
                "Market IV Mid",
                "Model IV Mid",
                "Market - Model",
                "Slope",
            ],
            show="headings",
        )
        for col in [
            "Strike",
            "Norm Strike",
            "Market IV Mid",
            "Model IV Mid",
            "Market - Model",
            "Slope",
        ]:
            self.tree.heading(col, text=col)
            self.tree.column(col, width=120, anchor="center")
        self.tree.pack(fill="both", expand=True)

        self.strike_to_item = {}
        for strike in sorted(self.chain.keys()):
            item = self.tree.insert("", "end", values=[strike, "", "", "", "", ""])
            self.strike_to_item[strike] = item

        self.fig, self.ax, self.canvas = self._build_plot(plot_tab)
        self.iv_fig, self.iv_ax, self.iv_canvas = self._build_plot(iv_plot_tab)
        self.error_fig, self.error_ax, self.error_canvas = self._build_plot(error_tab)
        self.cash_vol_fig, self.cash_vol_ax, self.cash_vol_canvas = self._build_plot(
            cash_vol_tab
        )
        self._build_risk_table(risk_tab)
        self._build_trades_table(trades_tab)
        self._build_pnl_tab(pnl_tab)

    @staticmethod
    def _build_summary_table(parent, title, parameters, side, padx):
        frame = tk.Frame(parent)
        frame.pack(side=side, padx=padx)
        tk.Label(frame, text=title, font=("Arial", 11, "bold")).pack()

        tree = ttk.Treeview(
            frame,
            columns=["Parameter", "Value"],
            show="headings",
            height=12,
        )
        for col in ["Parameter", "Value"]:
            tree.heading(col, text=col)
            tree.column(col, width=180, anchor="center")
        tree.pack()

        items = {}
        for name in parameters:
            items[name] = tree.insert("", "end", values=[name, ""])
        return tree, items

    @staticmethod
    def _build_plot(parent):
        fig = Figure(figsize=(8, 6), dpi=100)
        ax = fig.add_subplot(111)
        canvas = FigureCanvasTkAgg(fig, master=parent)
        canvas.get_tk_widget().pack(fill="both", expand=True)
        return fig, ax, canvas

    def _build_risk_table(self, parent) -> None:
        self.risk_columns = [
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
            "Gamma (L)",
            "Gamma Lots",
            "Vega",
            "BS Theta",
            "Std 1w Vega",
        ]
        self.risk_column_widths = [
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
            10,
            10,
            10,
            10,
            10,
        ]
        container = tk.Frame(parent)
        container.pack(fill="both", expand=True)

        self.risk_canvas = tk.Canvas(container, highlightthickness=0)
        y_scroll = ttk.Scrollbar(container, orient="vertical", command=self.risk_canvas.yview)
        x_scroll = ttk.Scrollbar(container, orient="horizontal", command=self.risk_canvas.xview)
        self.risk_canvas.configure(
            yscrollcommand=y_scroll.set,
            xscrollcommand=x_scroll.set,
        )

        self.risk_grid = tk.Frame(self.risk_canvas)
        self.risk_canvas_window = self.risk_canvas.create_window(
            (0, 0),
            window=self.risk_grid,
            anchor="nw",
        )
        self.risk_grid.bind(
            "<Configure>",
            lambda event: self.risk_canvas.configure(
                scrollregion=self.risk_canvas.bbox("all")
            ),
        )

        self.risk_canvas.grid(row=0, column=0, sticky="nsew")
        y_scroll.grid(row=0, column=1, sticky="ns")
        x_scroll.grid(row=1, column=0, sticky="ew")
        container.grid_rowconfigure(0, weight=1)
        container.grid_columnconfigure(0, weight=1)

        self._render_risk_header()

    def _build_trades_table(self, parent) -> None:
        self.trades_columns = [
            "Timestamp",
            "Underlying",
            "Maturity",
            "Strike",
            "Side",
            "Lots Change",
            "Synthetic Before",
            "Synthetic After",
            "Trade Price",
            "Live PnL",
            "Combined Delta Lots",
            "Threshold Lots",
            "Universal Mid",
            "Universal Spot",
        ]
        self.trades_column_widths = [18, 10, 10, 8, 7, 9, 11, 11, 9, 10, 12, 10, 9, 9]

        container = tk.Frame(parent)
        container.pack(fill="both", expand=True)

        self.trades_canvas = tk.Canvas(container, highlightthickness=0)
        y_scroll = ttk.Scrollbar(container, orient="vertical", command=self.trades_canvas.yview)
        x_scroll = ttk.Scrollbar(container, orient="horizontal", command=self.trades_canvas.xview)
        self.trades_canvas.configure(
            yscrollcommand=y_scroll.set,
            xscrollcommand=x_scroll.set,
        )

        self.trades_grid = tk.Frame(self.trades_canvas)
        self.trades_canvas.create_window((0, 0), window=self.trades_grid, anchor="nw")
        self.trades_grid.bind(
            "<Configure>",
            lambda event: self.trades_canvas.configure(
                scrollregion=self.trades_canvas.bbox("all")
            ),
        )

        self.trades_canvas.grid(row=0, column=0, sticky="nsew")
        y_scroll.grid(row=0, column=1, sticky="ns")
        x_scroll.grid(row=1, column=0, sticky="ew")
        container.grid_rowconfigure(0, weight=1)
        container.grid_columnconfigure(0, weight=1)
        self._render_trades_header()

    def _build_pnl_tab(self, parent) -> None:
        control_frame = tk.Frame(parent)
        control_frame.pack(fill="x", padx=12, pady=(12, 8))

        snap_button = tk.Button(
            control_frame,
            text="Snap Options PV",
            command=self._snap_options_pv,
            padx=10,
            pady=4,
        )
        snap_button.pack(side="left")

        clear_button = tk.Button(
            control_frame,
            text="Clear Hedge Trades",
            command=self._clear_hedge_trades,
            padx=10,
            pady=4,
        )
        clear_button.pack(side="left", padx=(8, 0))

        reload_button = tk.Button(
            control_frame,
            text="Reload Portfolio",
            command=self._reload_portfolio,
            padx=10,
            pady=4,
        )
        reload_button.pack(side="left", padx=(8, 0))

        self.pnl_snapshot_label = tk.Label(
            control_frame,
            text=f"Snapshot: {self.options_pv_snapshot_label}",
            padx=12,
            anchor="w",
            font=("Arial", 10),
        )
        self.pnl_snapshot_label.pack(side="left")

        self.pnl_grid = tk.Frame(parent)
        self.pnl_grid.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        self.pnl_column_widths = [20, 12]
        self.pnl_rows = [
            ("universal_mid", "Universal Mid"),
            ("atm_vol", "ATM Vol"),
            ("options_snapshot", "Options Snapshot PV"),
            ("options_live", "Options Live Mid PV"),
            ("options_pnl", "Options Live PnL"),
            ("hedge_pnl", "Hedge Trades Live PnL"),
            ("total_pnl", "Total PnL"),
        ]
        self._render_pnl_header()

    def _render_trades(self, result: AnalyticsResult | None = None) -> None:
        for widget in self.trades_grid.grid_slaves():
            if int(widget.grid_info()["row"]) > 0:
                widget.destroy()

        trades = list(reversed(load_trade_log(self.risk_engine.trade_log_path)))
        trade_mults = load_trade_mult_lookup(self.config.market.portfolio_file)
        live_universal_mid = result.universal_mid if result is not None else None
        total_live_pnl = sum(
            trade_live_pnl(trade, live_universal_mid, trade_mults) or 0.0
            for trade in trades
        )

        self._render_trades_row(
            1,
            (
                "Total",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                format_number(total_live_pnl, 2, use_commas=True),
                "",
                "",
                format_optional_number(live_universal_mid),
                "",
            ),
            (
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                total_live_pnl,
                "",
                "",
                live_universal_mid if live_universal_mid is not None else "",
                "",
            ),
            background="#cfe8ff",
            bold=True,
        )

        for row_index, trade in enumerate(trades, start=2):
            live_pnl = trade_live_pnl(trade, live_universal_mid, trade_mults)
            self._render_trades_row(
                row_index,
                (
                    trade.get("timestamp", ""),
                    trade.get("underlying", ""),
                    trade.get("maturity", ""),
                    trade.get("strike", ""),
                    trade.get("side", ""),
                    format_csv_number(trade.get("lots_change"), 0),
                    format_csv_number(trade.get("synthetic_lots_before"), 0),
                    format_csv_number(trade.get("synthetic_lots_after"), 0),
                    format_csv_number(trade.get("trade_price"), 2),
                    format_optional_number(live_pnl),
                    format_csv_number(trade.get("combined_delta_lots"), 2),
                    format_csv_number(trade.get("threshold_lots"), 2),
                    format_csv_number(trade.get("universal_mid"), 2),
                    format_csv_number(trade.get("universal_spot"), 2),
                ),
                (
                    "",
                    "",
                    "",
                    csv_numeric(trade.get("strike")),
                    "",
                    csv_numeric(trade.get("lots_change")),
                    csv_numeric(trade.get("synthetic_lots_before")),
                    csv_numeric(trade.get("synthetic_lots_after")),
                    csv_numeric(trade.get("trade_price")),
                    live_pnl if live_pnl is not None else "",
                    csv_numeric(trade.get("combined_delta_lots")),
                    csv_numeric(trade.get("threshold_lots")),
                    csv_numeric(trade.get("universal_mid")),
                    csv_numeric(trade.get("universal_spot")),
                ),
            )

    def refresh(self) -> None:
        result = self.analytics.calculate(self.store.snapshot())
        if result is not None:
            self.latest_result = result
            self.latest_risk_rows = self.risk_engine.calculate(result)
            self._render(result)
        else:
            self._render_trades()
            self._render_pnl(None, self.latest_risk_rows)

        self.root.after(self.config.market.refresh_ms, self.refresh)

    def _render(self, result: AnalyticsResult) -> None:
        self._render_rows(result)
        self._render_summary(result)
        self._render_slope_plot(result)
        self._render_iv_smile(result)
        self._render_error_plot(result)
        self._render_cash_vol_curve(result)
        self._render_risk(result, self.latest_risk_rows)
        self._render_trades(result)
        self._render_pnl(result, self.latest_risk_rows)

    def _render_rows(self, result: AnalyticsResult) -> None:
        for row in result.rows:
            self.tree.item(
                self.strike_to_item[row.strike],
                values=(
                    row.strike,
                    row.normalized_strike,
                    row.iv_mid,
                    format_optional_number(row.model_iv),
                    format_iv_diff(row.iv_mid, row.model_iv),
                    row.slope,
                ),
            )

    def _render_summary(self, result: AnalyticsResult) -> None:
        market = self.config.market
        strikes = self.config.strikes
        a_fit, bl_fit, br_fit, capl_fit, floorr_fit = result.fitted_params

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

        self._set_market("Best Synthetic Bid", round(result.best_bid, 2))
        self._set_market("Best Synthetic Ask", round(result.best_ask, 2))
        self._set_market("Universal Synthetic Mid", round(result.universal_mid, 2))
        self._set_market("Universal Spot", round(result.universal_spot, 2))
        self._set_market("ATM Vol", round(result.atm_vol * 100, 2))
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
        self.ax.set_title("Slope vs Normalized Strike")
        self.ax.grid(True)
        self.ax.legend()
        self.canvas.draw()

    def _render_iv_smile(self, result: AnalyticsResult) -> None:
        self.iv_ax.clear()
        self.iv_ax.plot(
            result.market_ns,
            result.market_iv,
            marker="o",
            label="IV Market",
        )
        if len(result.model_vols) == len(result.market_ns):
            self.iv_ax.plot(
                result.market_ns,
                result.model_vols,
                marker="x",
                label="IV Model",
            )
        self.iv_ax.set_xlabel("Normalized Strike")
        self.iv_ax.set_ylabel("IV Market / IV Model")
        self.iv_ax.set_title("IV Market vs IV Model")
        self.iv_ax.grid(True)
        self.iv_ax.legend()
        self.iv_canvas.draw()

    def _render_error_plot(self, result: AnalyticsResult) -> None:
        self.error_ax.clear()
        error_rows = [
            row
            for row in sorted(result.rows, key=lambda item: item.strike)
            if (
                row.iv_bid != ""
                and row.iv_ask != ""
                and row.iv_mid != ""
                and row.model_iv is not None
            )
        ]
        if error_rows:
            strikes = [row.strike for row in error_rows]
            mid_errors = [
                float(row.iv_mid) - float(row.model_iv) for row in error_rows
            ]
            bid_errors = [
                float(row.iv_bid) - float(row.model_iv) for row in error_rows
            ]
            ask_errors = [
                float(row.iv_ask) - float(row.model_iv) for row in error_rows
            ]
            lower_errors = [
                min(bid_error, ask_error)
                for bid_error, ask_error in zip(bid_errors, ask_errors)
            ]
            upper_errors = [
                max(bid_error, ask_error)
                for bid_error, ask_error in zip(bid_errors, ask_errors)
            ]
            self.error_ax.fill_between(
                strikes,
                lower_errors,
                upper_errors,
                color="#a6a6a6",
                alpha=0.75,
                label="Bid/Ask Spread",
            )
            self.error_ax.plot(
                strikes,
                mid_errors,
                marker="o",
                label="Mid Error",
            )
        self.error_ax.axhline(y=0, linestyle="--")
        self.error_ax.set_xlabel("Cash Strike")
        self.error_ax.set_ylabel("IV Market - IV Model")
        self.error_ax.set_title("IV Market - IV Model vs Cash Strike")
        self.error_ax.grid(True)
        if error_rows:
            self.error_ax.legend()
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
        model_strikes = [row.strike for row in model_rows]
        model_iv = [float(row.model_iv) for row in model_rows]

        self.cash_vol_ax.clear()
        if strikes:
            self.cash_vol_ax.errorbar(
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
        if model_strikes:
            self.cash_vol_ax.plot(
                model_strikes,
                model_iv,
                color="#9c00c8",
                linewidth=1.8,
                label="IV Model Fitted Curve",
            )

        self.cash_vol_ax.set_xlabel("Cash Strike")
        self.cash_vol_ax.set_ylabel("IV Market / IV Model")
        self.cash_vol_ax.set_title("IV Model Fitted Curve vs IV Market Bid/Ask")
        self.cash_vol_ax.grid(True)
        self.cash_vol_ax.legend()
        self.cash_vol_canvas.draw()

    def _render_risk(self, result: AnalyticsResult, risk_rows: list) -> None:
        for widget in self.risk_grid.grid_slaves():
            if int(widget.grid_info()["row"]) > 0:
                widget.destroy()

        options_rows = [row for row in risk_rows if row.book == "options"]
        delta_rows = [row for row in risk_rows if row.book == "delta"]

        row_index = 1
        row_index = self._render_risk_book_section(
            row_index,
            "Options Book",
            options_rows,
            result,
        )
        row_index = self._render_risk_book_section(
            row_index,
            "Delta Book",
            delta_rows,
            result,
        )

        if risk_rows:
            self._render_risk_grid_row(
                row_index,
                total_row_values(risk_rows, result, label="Master Total"),
                total_row_numeric_values(risk_rows, result),
                background="#cfe8ff",
                bold=True,
            )

    def _render_risk_book_section(
        self,
        row_index: int,
        title: str,
        rows: list,
        result: AnalyticsResult,
    ) -> int:
        if not rows:
            return row_index

        self._render_risk_section_title(row_index, title)
        row_index += 1

        for row in rows:
            self._render_risk_grid_row(
                row_index,
                row_values(row, result),
                row_numeric_values(row, result),
            )
            row_index += 1

        self._render_risk_grid_row(
            row_index,
            total_row_values(rows, result),
            total_row_numeric_values(rows, result),
            background="#e6e6e6",
            bold=True,
        )
        return row_index + 1

    def _render_risk_section_title(self, row_index: int, title: str) -> None:
        label = tk.Label(
            self.risk_grid,
            text=title,
            borderwidth=1,
            relief="solid",
            padx=6,
            pady=4,
            anchor="w",
            bg="#f7f7f7",
            font=("Arial", 9, "bold"),
        )
        label.grid(
            row=row_index,
            column=0,
            columnspan=len(self.risk_columns),
            sticky="nsew",
        )

    def _snap_options_pv(self) -> None:
        options_rows = [row for row in self.latest_risk_rows if getattr(row, "book", "") == "options"]
        self.options_pv_snapshot = weighted_price_total(options_rows, "mid_mkt")
        self.options_pv_snapshot_label = format_number(
            self.options_pv_snapshot,
            2,
            use_commas=True,
        )
        self.pnl_snapshot_label.config(text=f"Snapshot: {self.options_pv_snapshot_label}")
        self._render_pnl(self.latest_result, self.latest_risk_rows)

    def _clear_hedge_trades(self) -> None:
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
        with self.risk_engine.trade_log_path.open("w", newline="") as file:
            writer = csv.writer(file)
            writer.writerow(headers)
        self._render_trades(self.latest_result)
        self._render_pnl(self.latest_result, self.latest_risk_rows)

    def _reload_portfolio(self) -> None:
        self.options_pv_snapshot = None
        self.options_pv_snapshot_label = "Not snapped"
        self.pnl_snapshot_label.config(text=f"Snapshot: {self.options_pv_snapshot_label}")
        if self.latest_result is not None:
            self.latest_risk_rows = self.risk_engine.calculate(self.latest_result)
            self._render_risk(self.latest_result, self.latest_risk_rows)
            self._render_pnl(self.latest_result, self.latest_risk_rows)

    def _render_pnl(self, result: AnalyticsResult | None, risk_rows: list) -> None:
        options_rows = [row for row in risk_rows if getattr(row, "book", "") == "options"]
        options_live_pv = weighted_price_total(options_rows, "mid_mkt")
        hedge_pnl = 0.0
        live_universal_mid = result.universal_mid if result is not None else None
        live_atm_vol = result.atm_vol * 100 if result is not None else None
        trade_mults = load_trade_mult_lookup(self.config.market.portfolio_file)
        trades = load_trade_log(self.risk_engine.trade_log_path)
        hedge_pnl = sum(
            trade_live_pnl(trade, live_universal_mid, trade_mults) or 0.0
            for trade in trades
        )
        hedge_pnl /= 1000
        options_pnl = (
            options_live_pv - self.options_pv_snapshot
            if self.options_pv_snapshot is not None
            else None
        )
        total_pnl = hedge_pnl + (options_pnl or 0.0)

        self.pnl_snapshot_label.config(text=f"Snapshot: {self.options_pv_snapshot_label}")
        self._set_pnl_row("universal_mid", live_universal_mid)
        self._set_pnl_row("atm_vol", live_atm_vol, suffix="%")
        self._set_pnl_row("options_snapshot", self.options_pv_snapshot)
        self._set_pnl_row("options_live", options_live_pv)
        self._set_pnl_row("options_pnl", options_pnl)
        self._set_pnl_row("hedge_pnl", hedge_pnl)
        self._set_pnl_row("total_pnl", total_pnl, total=True)

    def _set_pnl_row(
        self,
        key: str,
        value: float | None,
        total: bool = False,
        suffix: str = "",
    ) -> None:
        row_index = next(index for index, item in enumerate(self.pnl_rows, start=1) if item[0] == key)
        metric = next(label for item_key, label in self.pnl_rows if item_key == key)
        for widget in self.pnl_grid.grid_slaves(row=row_index):
            widget.destroy()

        background = "#cfe8ff" if total else "white"
        self._render_pnl_cell(row_index, 0, metric, "", background, total)
        display_value = format_optional_number(value) if value is not None else ""
        if display_value and suffix:
            display_value = f"{display_value}{suffix}"
        self._render_pnl_cell(
            row_index,
            1,
            display_value,
            value if value is not None else "",
            background,
            total,
        )

    def _render_risk_grid_row(
        self,
        row_index: int,
        values: tuple,
        numeric_values: tuple,
        background: str = "white",
        bold: bool = False,
    ) -> None:
        for col_index, value in enumerate(values):
            foreground = "red" if is_negative(numeric_values[col_index]) else "black"
            label = tk.Label(
                self.risk_grid,
                text=value,
                borderwidth=1,
                relief="solid",
                padx=3,
                pady=3,
                width=self.risk_column_widths[col_index],
                anchor="center" if col_index in (0, 1, 2, 3, 4, 5, 6, 7) else "e",
                fg=foreground,
                bg=background,
                font=("Arial", 9, "bold") if bold else ("Arial", 9),
            )
            label.grid(row=row_index, column=col_index, sticky="nsew")

    def _render_trades_header(self) -> None:
        for col_index, col in enumerate(self.trades_columns):
            label = tk.Label(
                self.trades_grid,
                text=col,
                borderwidth=1,
                relief="solid",
                padx=6,
                pady=4,
                width=self.trades_column_widths[col_index],
                anchor="center",
                font=("Arial", 9, "bold"),
                bg="#f0f0f0",
            )
            label.grid(row=0, column=col_index, sticky="nsew")

    def _render_trades_row(
        self,
        row_index: int,
        values: tuple,
        numeric_values: tuple,
        background: str = "white",
        bold: bool = False,
    ) -> None:
        centered = {0, 1, 2, 3, 4}
        for col_index, value in enumerate(values):
            label = tk.Label(
                self.trades_grid,
                text=value,
                borderwidth=1,
                relief="solid",
                padx=3,
                pady=3,
                width=self.trades_column_widths[col_index],
                anchor="center" if col_index in centered else "e",
                fg="red" if is_negative(numeric_values[col_index]) else "black",
                bg=background,
                font=("Arial", 9, "bold") if bold else ("Arial", 9),
            )
            label.grid(row=row_index, column=col_index, sticky="nsew")

    def _render_pnl_header(self) -> None:
        headers = ("Metric", "Value")
        for col_index, header in enumerate(headers):
            label = tk.Label(
                self.pnl_grid,
                text=header,
                borderwidth=1,
                relief="solid",
                padx=6,
                pady=4,
                width=self.pnl_column_widths[col_index],
                anchor="center",
                font=("Arial", 9, "bold"),
                bg="#f0f0f0",
            )
            label.grid(row=0, column=col_index, sticky="nsew")

    def _render_pnl_cell(
        self,
        row_index: int,
        col_index: int,
        value,
        numeric_value,
        background: str,
        bold: bool,
    ) -> None:
        label = tk.Label(
            self.pnl_grid,
            text=value,
            borderwidth=1,
            relief="solid",
            padx=6,
            pady=4,
            width=self.pnl_column_widths[col_index],
            anchor="w" if col_index == 0 else "e",
            fg="red" if col_index == 1 and is_negative(numeric_value) else "black",
            bg=background,
            font=("Arial", 9, "bold") if bold else ("Arial", 9),
        )
        label.grid(row=row_index, column=col_index, sticky="nsew")

    def _render_risk_header(self) -> None:
        for col_index, col in enumerate(self.risk_columns):
            label = tk.Label(
                self.risk_grid,
                text=col,
                borderwidth=1,
                relief="solid",
                padx=6,
                pady=4,
                width=self.risk_column_widths[col_index],
                anchor="center",
                font=("Arial", 9, "bold"),
                bg="#f0f0f0",
            )
            label.grid(row=0, column=col_index, sticky="nsew")


def row_values(row, result: AnalyticsResult) -> tuple:
    return (
        format_number(row.lots, 0),
        row.book,
        row.underlying,
        row.maturity,
        row.strike,
        row.option_type,
        format_number(row.qty, 0),
        format_number(row.mult, 0),
        format_number(row.time_value, 2),
        format_number(row.price_fit, 2),
        format_number(row.bid_mkt, 2),
        format_number(row.ask_mkt, 2),
        format_number(row.mid_mkt, 2),
        format_number(result.universal_spot, 2),
        format_number(result.universal_mid, 2),
        format_number((row.model_iv or 0), 2),
        format_number(result.time, 6),
        format_number(row.bs_delta_pct, 2),
        format_number(row.bs_delta_ccy, 2),
        format_number(row.bs_delta_lots, 2),
        format_number(row.gamma_ccy_10bps, 2),
        format_number(row.gamma_lots_10bps, 2),
        format_number(row.vega_ccy_10bps, 0, use_commas=True),
        format_number(row.bs_theta_ccy, 0, use_commas=True),
        format_number(row.std_1w_vega, 0, use_commas=True),
    )


def row_numeric_values(row, result: AnalyticsResult) -> tuple:
    return (
        row.lots,
        "",
        "",
        "",
        row.strike,
        "",
        row.qty,
        row.mult,
        row.time_value,
        row.price_fit,
        row.bid_mkt,
        row.ask_mkt,
        row.mid_mkt,
        result.universal_spot,
        result.universal_mid,
        row.model_iv if row.model_iv is not None else "",
        result.time,
        row.bs_delta_pct,
        row.bs_delta_ccy,
        row.bs_delta_lots,
        row.gamma_ccy_10bps,
        row.gamma_lots_10bps,
        row.vega_ccy_10bps,
        row.bs_theta_ccy,
        row.std_1w_vega,
    )


def total_row_values(rows: list, result: AnalyticsResult, label: str = "Total") -> tuple:
    totals = total_row_numeric_values(rows, result)
    return (
        format_number(totals[0], 0),
        label,
        "",
        "",
        "",
        "",
        format_number(totals[6], 0),
        "",
        format_number(weighted_price_total(rows, "time_value"), 2),
        format_number(weighted_price_total(rows, "price_fit"), 2),
        format_number(weighted_price_total(rows, "bid_mkt"), 2),
        format_number(weighted_price_total(rows, "ask_mkt"), 2),
        format_number(weighted_price_total(rows, "mid_mkt"), 2),
        "",
        "",
        "",
        "",
        format_number(totals[17], 2),
        format_number(totals[18], 2),
        format_number(totals[19], 2),
        format_number(totals[20], 2),
        format_number(totals[21], 2),
        format_number(totals[22], 0, use_commas=True),
        format_number(totals[23], 0, use_commas=True),
        format_number(totals[24], 0, use_commas=True),
    )


def total_row_numeric_values(rows: list, result: AnalyticsResult) -> tuple:
    numeric_rows = [row_numeric_values(row, result) for row in rows]
    totals = []
    for col_index in range(25):
        if col_index in (13, 14, 15, 16):
            totals.append("")
        else:
            totals.append(sum_numeric_column(numeric_rows, col_index))
    return tuple(totals)


def sum_numeric_column(rows: list[tuple], col_index: int) -> float | str:
    values = [row[col_index] for row in rows if isinstance(row[col_index], (int, float))]
    return sum(values) if values else ""


def weighted_price_total(rows: list, field_name: str) -> float:
    total = 0.0
    for row in rows:
        price = getattr(row, field_name, "")
        qty = getattr(row, "qty", 0)
        if isinstance(price, (int, float)) and isinstance(qty, (int, float)):
            total += qty * price / 1000
    return total


def format_optional_number(value, decimals: int = 2) -> str:
    if value is None or value == "":
        return ""
    return format_number(value, decimals)


def load_trade_log(path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="") as file:
        return list(csv.DictReader(file))


def load_trade_mult_lookup(path) -> dict[tuple[str, str, int], float]:
    if not path.exists():
        return {}
    with path.open(newline="") as file:
        rows = list(csv.DictReader(file))

    lookup: dict[tuple[str, str, int], float] = {}
    for row in rows:
        try:
            underlying = str(row.get("Underlying", "")).strip().upper()
            maturity = str(row.get("Maturity", "")).strip()
            strike = int(float(str(row.get("Strike", "")).strip()))
            mult = float(str(row.get("Mult", "")).strip())
        except (TypeError, ValueError):
            continue
        lookup[(underlying, maturity, strike)] = mult
    return lookup


def csv_numeric(value) -> float | str:
    if value in (None, ""):
        return ""
    try:
        return float(value)
    except (TypeError, ValueError):
        return ""


def trade_live_pnl(
    trade: dict[str, str],
    live_universal_mid: float | None,
    trade_mults: dict[tuple[str, str, int], float],
) -> float | None:
    if live_universal_mid is None:
        return None
    try:
        lots_change = float(trade.get("lots_change", ""))
        trade_price = float(trade.get("trade_price", ""))
    except (TypeError, ValueError):
        return None
    try:
        mult = float(trade.get("mult", ""))
    except (TypeError, ValueError):
        try:
            key = (
                str(trade.get("underlying", "")).strip().upper(),
                str(trade.get("maturity", "")).strip(),
                int(float(str(trade.get("strike", "")).strip())),
            )
        except (TypeError, ValueError):
            return None
        mult = trade_mults.get(key)
        if mult is None:
            return None
    return lots_change * (live_universal_mid - trade_price) * mult


def format_csv_number(value, decimals: int = 2) -> str:
    if value in (None, ""):
        return ""
    try:
        return format_number(float(value), decimals)
    except (TypeError, ValueError):
        return str(value)


def format_iv_diff(market_iv, model_iv) -> str:
    if market_iv == "" or model_iv is None:
        return ""
    return format_number(float(market_iv) - float(model_iv), 2)


def format_number(value, decimals: int = 2, use_commas: bool = False) -> str:
    if value == "":
        return ""
    if isinstance(value, int):
        format_spec = f",.{decimals}f" if use_commas else f".{decimals}f"
        return format(value, format_spec) if decimals > 0 else format(value, ",d" if use_commas else "d")
    if isinstance(value, float):
        format_spec = f",.{decimals}f" if use_commas else f".{decimals}f"
        return format(value, format_spec)
    return str(value)


def is_negative(value) -> bool:
    return isinstance(value, (int, float)) and value < 0
