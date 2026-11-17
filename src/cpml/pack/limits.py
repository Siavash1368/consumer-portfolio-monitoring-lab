"""Risk-appetite limit utilisation and early-warning triggers.

Both read their definitions from config/monitoring_config.yaml. No threshold
appears in this file. The output of each is a table with the measured value,
the declared limit, the utilisation, the status and - for anything that fires -
the declared action, so the breach log is self-explaining to a reader who has
never seen the code.

The limits are ILLUSTRATIVE, set by the author to exercise the machinery on
public data. They are not any institution's risk appetite.
"""
from __future__ import annotations

import pandas as pd

from .. import config
from . import metrics

CFG = config.load()


def _status(value: float, amber: float, red: float, direction: str) -> str:
    if pd.isna(value):
        return "NO DATA"
    if direction == "max":
        return "RED" if value >= red else ("AMBER" if value >= amber else "GREEN")
    return "RED" if value <= red else ("AMBER" if value <= amber else "GREEN")


def limit_utilisation(con, asof: str) -> pd.DataFrame:
    """Evaluate every declared risk-appetite limit for one as-of month."""
    rows = []
    trend = metrics.add_rates(metrics.portfolio_trend(con, asof))
    cur = trend[trend.ym == asof]
    for lim in CFG["risk_appetite_limits"]:
        dim = lim["dimension"]
        if dim == "portfolio":
            measured = float(cur[lim["metric"]].iloc[0]) if len(cur) else float("nan")
            where = "portfolio"
        else:
            conc = metrics.concentration(con, asof, dim)
            if lim.get("dimension_values"):
                sel = conc[conc.dim_value.isin(lim["dimension_values"])]
                measured = float(sel["share_upb"].sum())
                where = "+".join(lim["dimension_values"])
            else:
                # no values listed means the limit binds on the largest single bucket
                sel = conc.nlargest(1, "share_upb")
                measured = float(sel["share_upb"].iloc[0]) if len(sel) else float("nan")
                where = str(sel["dim_value"].iloc[0]) if len(sel) else "n/a"
        limit = float(lim["limit"])
        util = measured / limit if limit else float("nan")
        rows.append({
            "asof": asof, "limit_id": lim["id"], "name": lim["name"],
            "dimension": dim, "measured_on": where,
            "measured": measured, "limit": limit, "utilisation": util,
            "status": "BREACH" if util > 1.0 else ("WATCH" if util >= 0.9 else "WITHIN"),
            "rationale": lim["rationale"],
        })
    return pd.DataFrame(rows)


def evaluate_triggers(con, asof: str) -> pd.DataFrame:
    """Evaluate every declared early-warning trigger for one as-of month."""
    trend = metrics.add_rates(metrics.portfolio_trend(con, asof))
    rr = metrics.headline_rolls(metrics.roll_rates(con, asof))

    def roll_value(metric: str) -> float:
        sub = rr[(rr.metric == metric) & (rr.ym == asof)]
        return float(sub["rate_cnt"].iloc[0]) if len(sub) else float("nan")

    cur = trend[trend.ym == asof]
    values = {
        "dpd90p_rate_upb": float(cur["dpd90p_rate_upb"].iloc[0]) if len(cur) else float("nan"),
        "dpd90p_rate_upb_chg_3m": float(cur["dpd90p_rate_upb_chg_3m"].iloc[0]) if len(cur) else float("nan"),
        "roll_cur_to_30": roll_value("roll_cur_to_30"),
        "roll_30_to_60": roll_value("roll_30_to_60"),
        "cure_30_to_cur": roll_value("cure_30_to_cur"),
        "flow_to_loss_12m": metrics.flow_to_loss_12m(con, asof),
    }
    rows = []
    for trg in CFG["early_warning_triggers"]:
        v = values.get(trg["metric"], float("nan"))
        st = _status(v, float(trg["amber"]), float(trg["red"]), trg["direction"])
        rows.append({
            "asof": asof, "trigger_id": trg["id"], "name": trg["name"],
            "metric": trg["metric"], "value": v,
            "amber": trg["amber"], "red": trg["red"], "direction": trg["direction"],
            "status": st,
            "action": trg["action"] if st in ("AMBER", "RED") else "",
        })
    return pd.DataFrame(rows)


def breach_log(limits_df: pd.DataFrame, triggers_df: pd.DataFrame) -> pd.DataFrame:
    """Everything that fired this month, in one table, with its action."""
    a = limits_df[limits_df.status != "WITHIN"][
        ["asof", "limit_id", "name", "measured", "limit", "status"]
    ].rename(columns={"limit_id": "id", "measured": "value", "limit": "threshold"})
    a["kind"] = "risk_appetite_limit"
    a["action"] = "Report utilisation to committee; breach requires a documented remediation plan."
    b = triggers_df[triggers_df.status.isin(["AMBER", "RED"])][
        ["asof", "trigger_id", "name", "value", "red", "status", "action"]
    ].rename(columns={"trigger_id": "id", "red": "threshold"})
    b["kind"] = "early_warning_trigger"
    cols = ["asof", "kind", "id", "name", "value", "threshold", "status", "action"]
    return pd.concat([a.reindex(columns=cols), b.reindex(columns=cols)], ignore_index=True)
