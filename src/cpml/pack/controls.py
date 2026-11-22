"""Reconciliation control: the mart against independent source totals.

The mart's portfolio totals are built by joining performance to origination and
deriving loan states. The control totals are built by a different, simpler
query that reads the performance table alone (see build_mart.CONTROL_SQL). If
the two disagree, something in the join or the state logic lost or duplicated
rows.

Every month gets one of three statuses:

  PASS       counts tie exactly and balances tie within the declared tolerance
  EXPLAINED  the difference matches a documented exception in the config
             exactly - a known, reviewed break, not a new one
  FAIL       anything else. A FAIL blocks publication of the pack.

A documented exception only excuses the exact difference it describes. If the
same month breaks by a different amount next time, it is a FAIL again.
"""
from __future__ import annotations

import pandas as pd

from .. import config

ACTIVE_AND_TERMINAL = None  # the cube portfolio rows already carry every state


def reconcile(con, through: str) -> pd.DataFrame:
    cfg = config.load()
    tol_loans = int(cfg["controls"]["reconciliation_tolerance_loans"])
    tol_upb = float(cfg["controls"]["reconciliation_tolerance_upb_pct"])
    exceptions = {
        str(e["ym"]): e for e in cfg["controls"].get("reconciliation_exceptions", [])
    }
    df = con.sql(f"""
        WITH m AS (
            SELECT ym, SUM(loans) AS mart_loans, SUM(upb) AS mart_upb
            FROM fact_cube WHERE dim_name = 'portfolio' AND ym <= '{through}'
            GROUP BY 1
        )
        SELECT c.ym,
               COALESCE(m.mart_loans, 0) AS mart_loans,
               c.loans                   AS source_loans,
               COALESCE(m.mart_upb, 0)   AS mart_upb,
               c.upb                     AS source_upb
        FROM fact_control_totals c
        LEFT JOIN m USING (ym)
        WHERE c.ym <= '{through}'
        ORDER BY c.ym
    """).df()
    df["diff_loans"] = df["source_loans"] - df["mart_loans"]
    df["diff_upb"] = df["source_upb"] - df["mart_upb"]
    df["rel_diff_upb"] = (df["diff_upb"].abs()
                          / df["source_upb"].where(df["source_upb"] != 0, 1))

    def status(r) -> tuple[str, str]:
        if abs(r.diff_loans) <= tol_loans and r.rel_diff_upb <= tol_upb:
            return "PASS", ""
        e = exceptions.get(r.ym)
        if e is not None and int(e["diff_loans"]) == int(r.diff_loans) \
                and abs(float(e["diff_upb"]) - float(r.diff_upb)) < 0.01:
            return "EXPLAINED", e["reason"]
        return "FAIL", ""

    st = df.apply(status, axis=1, result_type="expand")
    df["status"], df["explanation"] = st[0], st[1]
    return df


def summarise(rec: pd.DataFrame) -> dict:
    counts = rec["status"].value_counts().to_dict()
    return {
        "months": int(len(rec)),
        "pass": int(counts.get("PASS", 0)),
        "explained": int(counts.get("EXPLAINED", 0)),
        "fail": int(counts.get("FAIL", 0)),
        "publishable": int(counts.get("FAIL", 0)) == 0,
    }
