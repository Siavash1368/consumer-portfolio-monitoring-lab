"""Tableau extracts: tidy CSVs with real dates, one file per dashboard view.

Tableau Public cannot connect to a database, so the dashboard reads these
files. They are rebuilt from the mart by:

    python src/cpml/report/tableau_extracts.py

and written to tableau/extracts/. Trend and roll-rate series cover every month
through the default as-of; concentration, limits and triggers are produced for
both the featured month (the 2009 worked example) and the latest month, with
an `asof` column so a single Tableau filter switches between them.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))

import pandas as pd  # noqa: E402

from cpml import config, db  # noqa: E402
from cpml.pack import limits, metrics  # noqa: E402

CFG = config.load()
OUT = os.path.join(config.REPO_ROOT, "tableau", "extracts")
DIMS = ["state", "fico_band", "ltv_band", "purpose", "occupancy",
        "prop_type", "channel", "term_band", "vintage_year"]


def as_date(ym: pd.Series) -> pd.Series:
    return pd.to_datetime(ym.astype(str) + "01", format="%Y%m%d").dt.date


def main() -> None:
    os.makedirs(OUT, exist_ok=True)
    latest = CFG["reporting"]["default_asof"]
    featured = CFG["reporting"]["featured_asof"]
    con = db.mart(read_only=True)

    trend = metrics.add_rates(metrics.portfolio_trend(con, latest))
    trend.insert(0, "month", as_date(trend["ym"]))
    trend["active_upb_bn"] = trend["active_upb"] / 1e9
    trend.to_csv(os.path.join(OUT, "trend.csv"), index=False)

    den = metrics.denominator_check(con, latest)
    den.insert(0, "month", as_date(den["ym"]))
    den.to_csv(os.path.join(OUT, "denominator.csv"), index=False)

    rolls = metrics.headline_rolls(metrics.roll_rates(con, latest))
    rolls.insert(0, "month", as_date(rolls["ym"]))
    rolls.to_csv(os.path.join(OUT, "roll_rates.csv"), index=False)

    vc = metrics.vintage_curves(con, latest)
    vc = vc[vc["observed_share"] >= CFG["reporting"]["vintage_min_observed_share"]]
    vc.to_csv(os.path.join(OUT, "vintage_curves.csv"), index=False)

    conc, lims, trgs = [], [], []
    for asof in (featured, latest):
        for d in DIMS:
            c = metrics.concentration(con, asof, d)
            c.insert(0, "dim_name", d)
            c.insert(0, "asof", as_date(pd.Series([asof] * len(c))))
            conc.append(c)
        lims.append(limits.limit_utilisation(con, asof))
        trgs.append(limits.evaluate_triggers(con, asof))
    pd.concat(conc).to_csv(os.path.join(OUT, "concentration.csv"), index=False)
    l = pd.concat(lims)
    l["asof"] = as_date(l["asof"])
    l.to_csv(os.path.join(OUT, "limits.csv"), index=False)
    t = pd.concat(trgs)
    t["asof"] = as_date(t["asof"])
    t.to_csv(os.path.join(OUT, "triggers.csv"), index=False)
    con.close()
    write_long(trend, rolls, vc, pd.concat(conc), l, t)
    for f in sorted(os.listdir(OUT)):
        n = sum(1 for _ in open(os.path.join(OUT, f), encoding="utf-8")) - 1
        print(f"  {f:22s} {n:>7,} rows")


LABELS = {
    "dpd90p_rate_upb": "90+ DPD rate",
    "dq3089_rate_upb": "30-89 DPD rate",
    "roll_cur_to_30": "Current to 30",
    "roll_30_to_60": "30 to 60",
    "roll_60_to_90": "60 to 90+",
    "cure_30_to_cur": "Cure 30 to current",
    "cure_60_to_cur": "Cure 60 to current",
    "roll_90_to_loss": "90+ to credit event",
}


def write_long(trend, rolls, vc, conc, lim, trg) -> None:
    """One tidy file for the whole dashboard, so Tableau Public needs a single
    upload. Each sheet filters on `view`."""
    frames = []
    t = trend[trend["ym"] >= "200303"].copy()   # the first months hold a handful of loans
    t["dq3089_rate_upb"] = t["dpd30_rate_upb"] + t["dpd60_rate_upb"]
    for col in ("dpd90p_rate_upb", "dq3089_rate_upb"):
        frames.append(pd.DataFrame({"view": "trend", "series": LABELS[col],
                                    "month": t["month"], "value": t[col]}))
    frames.append(pd.DataFrame({"view": "book", "series": "Active balance ($bn)",
                                "month": t["month"], "value": t["active_upb_bn"]}))
    r = rolls[rolls["ym"] >= "200303"]
    frames.append(pd.DataFrame({"view": "rolls", "series": r["metric"].map(LABELS),
                                "month": r["month"], "value": r["rate_cnt"]}))
    frames.append(pd.DataFrame({"view": "vintage", "series": vc["vintage_year"].astype(str),
                                "mob": vc["mob"], "value": vc["cum_ever90_rate"]}))
    frames.append(pd.DataFrame({"view": "concentration", "series": conc["dim_name"],
                                "asof": conc["asof"], "dim_value": conc["dim_value"],
                                "value": conc["share_upb"], "value2": conc["dpd90p_rate_upb"]}))
    frames.append(pd.DataFrame({"view": "limits", "series": lim["limit_id"] + " " + lim["name"],
                                "asof": lim["asof"], "value": lim["measured"],
                                "value2": lim["utilisation"], "threshold": lim["limit"],
                                "status": lim["status"]}))
    frames.append(pd.DataFrame({"view": "triggers", "series": trg["trigger_id"] + " " + trg["name"],
                                "asof": trg["asof"], "value": trg["value"],
                                "threshold": trg["red"], "status": trg["status"],
                                "action": trg["action"]}))
    cols = ["view", "series", "month", "mob", "asof", "dim_value",
            "value", "value2", "threshold", "status", "action"]
    out = pd.concat(frames, ignore_index=True).reindex(columns=cols)
    out.to_csv(os.path.join(OUT, "dashboard_long.csv"), index=False)


if __name__ == "__main__":
    main()
