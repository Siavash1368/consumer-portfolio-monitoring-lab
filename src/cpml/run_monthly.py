"""The monthly pack. One parameter: the as-of month.

    python src/cpml/run_monthly.py --asof 200906

Running it produces the entire pack for that month into outputs/<asof>/ and
writes a manifest recording the config version, the mart build state and the
row counts behind every figure, so any number in the pack can be traced back to
the run that produced it.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import pandas as pd  # noqa: E402

from cpml import config, db  # noqa: E402
from cpml.pack import controls, limits, metrics  # noqa: E402
from cpml.report import summary  # noqa: E402

CFG = config.load()
DIMS = ["state", "fico_band", "ltv_band", "purpose", "occupancy",
        "prop_type", "channel", "term_band", "vintage_year"]


def _write(df: pd.DataFrame, outdir: str, name: str) -> dict:
    path = os.path.join(outdir, f"{name}.csv")
    df.to_csv(path, index=False)
    print(f"  {name:28s} {len(df):>8,} rows")
    return {"file": f"{name}.csv", "rows": int(len(df))}


def run(asof: str, outroot: str = "outputs") -> dict:
    con = db.mart(read_only=True)
    available = [r[0] for r in con.sql(
        "SELECT DISTINCT ym FROM fact_cube ORDER BY ym").fetchall()]
    if asof not in available:
        raise SystemExit(
            f"as-of month {asof} is not in the mart "
            f"(available {available[0]}..{available[-1]})")

    outdir = os.path.join(config.REPO_ROOT, outroot, asof)
    os.makedirs(outdir, exist_ok=True)
    print(f"\nMonthly pack - as of {asof}  (config {CFG['config_version']})")
    print("-" * 64)

    files = []
    trend = metrics.add_rates(metrics.portfolio_trend(con, asof))
    files.append(_write(trend, outdir, "portfolio_trend"))

    rr = metrics.roll_rates(con, asof)
    files.append(_write(rr, outdir, "roll_rate_matrix"))
    files.append(_write(metrics.headline_rolls(rr), outdir, "roll_rates_headline"))

    files.append(_write(metrics.vintage_curves(con, asof), outdir, "vintage_curves"))
    for dim in ("state", "vintage_year", "fico_band", "ltv_band"):
        ss = metrics.shift_share(con, asof, dim)
        if len(ss):
            files.append(_write(ss, outdir, f"attribution_12m_{dim}"))
    files.append(_write(metrics.denominator_check(con, asof), outdir, "denominator_check"))

    conc = pd.concat(
        [metrics.concentration(con, asof, d).assign(dim_name=d) for d in DIMS],
        ignore_index=True)
    files.append(_write(conc, outdir, "concentration"))

    lim = limits.limit_utilisation(con, asof)
    trg = limits.evaluate_triggers(con, asof)
    files.append(_write(lim, outdir, "risk_appetite_limits"))
    files.append(_write(trg, outdir, "early_warning_triggers"))
    files.append(_write(limits.breach_log(lim, trg), outdir, "breach_log"))

    rec = controls.reconcile(con, asof)
    files.append(_write(rec, outdir, "reconciliation"))
    rec_summary = controls.summarise(rec)

    manifest = {
        "asof": asof,
        "config_version": CFG["config_version"],
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "portfolio": CFG["source"]["portfolio_name"],
        "mart_months": {"min": available[0], "max": available[-1], "n": len(available)},
        "mart_rows": {
            t: int(con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0])
            for t in ("fact_cube", "fact_transition", "dim_loan", "fact_control_totals")
        },
        "headline": {
            "active_loans": int(trend[trend.ym == asof]["active_loans"].iloc[0]),
            "active_upb": float(trend[trend.ym == asof]["active_upb"].iloc[0]),
            "dpd90p_rate_upb": float(trend[trend.ym == asof]["dpd90p_rate_upb"].iloc[0]),
        },
        "reconciliation": rec_summary,
        "limits_breached": lim[lim.status == "BREACH"]["limit_id"].tolist(),
        "triggers_fired": trg[trg.status.isin(["AMBER", "RED"])]["trigger_id"].tolist(),
        "files": files,
    }
    with open(os.path.join(outdir, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2)

    h = manifest["headline"]
    print("-" * 64)
    print(f"  active book      {h['active_loans']:>12,} loans   "
          f"${h['active_upb']/1e9:,.1f}bn")
    print(f"  90+ rate (bal)   {h['dpd90p_rate_upb']*100:>12.2f}%")
    print(f"  limits breached  {manifest['limits_breached'] or 'none'}")
    print(f"  triggers fired   {manifest['triggers_fired'] or 'none'}")
    print(f"  reconciliation   {rec_summary['pass']} pass, {rec_summary['explained']} explained, "
          f"{rec_summary['fail']} fail")
    con.close()
    if not rec_summary["publishable"]:
        # declared control behaviour: a failed reconciliation blocks publication
        print("  CONTROL FAILURE - pack written for investigation but NOT publishable")
        raise SystemExit(2)
    if len(available) >= 13 and available.index(asof) >= 12:
        print(f"  committee page   {os.path.relpath(summary.write(asof, outroot), config.REPO_ROOT)}")
    print(f"  written to       {os.path.relpath(outdir, config.REPO_ROOT)}\n")
    return manifest


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Run the monthly monitoring pack.")
    ap.add_argument("--asof", default=CFG["reporting"]["default_asof"],
                    help="reporting month as YYYYMM")
    a = ap.parse_args()
    run(a.asof)
