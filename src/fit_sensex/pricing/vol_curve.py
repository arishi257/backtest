from __future__ import annotations

from scipy.optimize import minimize


class ParametricVolCurve:
    def __init__(self, initial_params: list[float]) -> None:
        self.last_fitted_params = list(initial_params)

    @staticmethod
    def model_slope(
        ns: float,
        a: float,
        b_l: float,
        b_r: float,
        cap_l: float,
        floor_r: float,
    ) -> float:
        if ns < 0:
            return min(a + b_l * ns, cap_l)
        return max(a + b_r * ns, floor_r)

    def build_model_vol_curve(
        self,
        ns_values: list[float],
        atm_vol_percent: float,
        a: float,
        b_l: float,
        b_r: float,
        cap_l: float,
        floor_r: float,
    ) -> list[float]:
        paired = sorted(zip(ns_values, range(len(ns_values))))
        sorted_ns = [x[0] for x in paired]
        sorted_idx = [x[1] for x in paired]
        model_vols = [0.0] * len(ns_values)

        atm_index = min(range(len(sorted_ns)), key=lambda i: abs(sorted_ns[i]))
        original_atm_idx = sorted_idx[atm_index]
        model_vols[original_atm_idx] = atm_vol_percent

        for i in range(atm_index + 1, len(sorted_ns)):
            prev_ns = sorted_ns[i - 1]
            curr_ns = sorted_ns[i]
            prev_idx = sorted_idx[i - 1]
            curr_idx = sorted_idx[i]
            slope = self.model_slope(prev_ns, a, b_l, b_r, cap_l, floor_r)
            model_vols[curr_idx] = model_vols[prev_idx] - slope * (
                (curr_ns - prev_ns) / 0.1
            )

        for i in range(atm_index - 1, -1, -1):
            curr_ns = sorted_ns[i]
            next_ns = sorted_ns[i + 1]
            curr_idx = sorted_idx[i]
            next_idx = sorted_idx[i + 1]
            slope = self.model_slope(curr_ns, a, b_l, b_r, cap_l, floor_r)
            model_vols[curr_idx] = model_vols[next_idx] + slope * (
                (next_ns - curr_ns) / 0.1
            )

        return model_vols

    def fit(
        self,
        ns_values: list[float],
        market_vols: list[float],
        vegas: list[float],
        atm_vol_percent: float,
    ) -> tuple[list[float], list[float], float]:
        initial_guess = self.last_fitted_params

        def objective(params: list[float]) -> float:
            model_vols = self.build_model_vol_curve(
                ns_values, atm_vol_percent, *params
            )
            return sum(
                vega * (market_vol - model_vol) ** 2
                for market_vol, model_vol, vega in zip(market_vols, model_vols, vegas)
            )

        result = minimize(
            objective,
            initial_guess,
            method="Nelder-Mead",
            options={"maxiter": 300, "xatol": 1e-5, "fatol": 1e-5},
        )

        fitted_params = result.x.tolist()
        self.last_fitted_params = fitted_params
        final_model = self.build_model_vol_curve(
            ns_values, atm_vol_percent, *fitted_params
        )
        return fitted_params, final_model, float(result.fun)
