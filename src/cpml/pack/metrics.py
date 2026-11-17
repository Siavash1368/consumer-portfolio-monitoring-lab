"""Portfolio monitoring metrics, all read from the mart.

Measurement conventions, stated once and applied everywhere
-----------------------------------------------------------
* STOCK measures (balances outstanding, delinquency rates) count only ACTIVE
  states. A loan-month carrying a zero-balance code is a termination EVENT in
  that month, not a balance, and its reported balance is zero by construction;
  the balance it removed is carried separately as upb_removed.
* RATES are reported balance-weighted by default and count-weighted alongside.
  Balance-weighted is the committee number; the count version is what makes a
  mix shift visible.
* ROLL RATES exclude REPORTING_GAP and CENSORED from the denominator, because
  neither is a credit outcome. The share excluded is published next to every
  roll rate rather than hidden - if it moves, the roll rates are not comparable
  month to month.
"""
from __future__ import annotations

import pandas as pd

from .. import config, db

ACTIVE = config.ACTIVE_STATES
ACTIVE_SQL = ", ".join(f"'{s}'" for s in ACTIVE)
NON_OUTCOME = ("REPORTING_GAP", "CENSORED")


def _con():
    return db.mart(read_only=True)


# ---------------------------------------------------------------- stock ----
def portfolio_trend(con, through: str) -> pd.DataFrame:
    """Month by month book size and delinquency, up to and including `through`."""
    return con.sql(f"""
        WITH s AS (
            SELECT ym, loan_state, loans, upb, upb_removed
            FROM fact_cube
            WHERE dim_name = 'portfolio' AND ym <= '{through}'
        )
        SELECT ym,
               SUM(CASE WHEN loan_state IN ({ACTIVE_SQL}) THEN loans END)        AS active_loans,
               SUM(CASE WHEN loan_state IN ({ACTIVE_SQL}) THEN upb   END)        AS active_upb,
               SUM(CASE WHEN loan_state = 'DPD30'  THEN loans END)               AS dpd30_loans,
               SUM(CASE WHEN loan_state = 'DPD60'  THEN loans END)               AS dpd60_loans,
               SUM(CASE WHEN loan_state = 'DPD90P' THEN loans END)               AS dpd90p_loans,
               SUM(CASE WHEN loan_state = 'DPD30'  THEN upb   END)               AS dpd30_upb,
               SUM(CASE WHEN loan_state = 'DPD60'  THEN upb   END)               AS dpd60_upb,
               SUM(CASE WHEN loan_state = 'DPD90P' THEN upb   END)               AS dpd90p_upb,
               SUM(CASE WHEN loan_state = 'PREPAID'      THEN loans END)         AS prepaid_loans,
               SUM(CASE WHEN loan_state = 'PREPAID'      THEN upb_removed END)   AS prepaid_upb,
               SUM(CASE WHEN loan_state = 'CREDIT_EVENT' THEN loans END)         AS credit_event_loans,
               SUM(CASE WHEN loan_state = 'CREDIT_EVENT' THEN upb_removed END)   AS credit_event_upb
        FROM s GROUP BY ym ORDER BY ym
    """).df().fillna(0)


def add_rates(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    for lvl in ("dpd30", "dpd60", "dpd90p"):
        out[f"{lvl}_rate_upb"] = out[f"{lvl}_upb"] / out["active_upb"].replace(0, pd.NA)
        out[f"{lvl}_rate_cnt"] = out[f"{lvl}_loans"] / out["active_loans"].replace(0, pd.NA)
    out["dq_total_rate_upb"] = (
        out[["dpd30_upb", "dpd60_upb", "dpd90p_upb"]].sum(axis=1)
        / out["active_upb"].replace(0, pd.NA)
    )
    # 3-month change in the headline rate - the input to trigger EW-01
    out["dpd90p_rate_upb_chg_3m"] = out["dpd90p_rate_upb"].diff(3)
    return out


# ----------------------------------------------------------- roll rates ----
def roll_rates(con, through: str) -> pd.DataFrame:
    """Transition rates out of each active state, by month."""
    return con.sql(f"""
        WITH t AS (
            SELECT ym_from AS ym, state_from, state_to, loans, upb_from
            FROM fact_transition WHERE ym_from <= '{through}'
        ),
        d AS (
            SELECT ym, state_from,
                   SUM(CASE WHEN state_to NOT IN ('REPORTING_GAP','CENSORED')
                            THEN loans END) AS denom_loans,
                   SUM(CASE WHEN state_to NOT IN ('REPORTING_GAP','CENSORED')
                            THEN upb_from END) AS denom_upb,
                   SUM(loans) AS all_loans,
                   SUM(CASE WHEN state_to = 'REPORTING_GAP' THEN loans END) AS gap_loans
            FROM t GROUP BY 1,2
        )
        SELECT t.ym, t.state_from, t.state_to,
               t.loans, t.upb_from,
               d.denom_loans, d.denom_upb,
               t.loans    / NULLIF(d.denom_loans,0) AS rate_cnt,
               t.upb_from / NULLIF(d.denom_upb,0)   AS rate_upb,
               COALESCE(d.gap_loans,0) / NULLIF(d.all_loans,0) AS gap_share
        FROM t JOIN d USING (ym, state_from)
        WHERE t.state_to NOT IN ('REPORTING_GAP','CENSORED')
        ORDER BY t.ym, t.state_from, t.state_to
    """).df()


HEADLINE_ROLLS = {
    "roll_cur_to_30":   ("CUR", "DPD30"),
    "roll_30_to_60":    ("DPD30", "DPD60"),
    "roll_60_to_90":    ("DPD60", "DPD90P"),
    "cure_30_to_cur":   ("DPD30", "CUR"),
    "cure_60_to_cur":   ("DPD60", "CUR"),
    "roll_90_to_loss":  ("DPD90P", "CREDIT_EVENT"),
}


def headline_rolls(rr: pd.DataFrame) -> pd.DataFrame:
    """Pivot the roll matrix down to the handful of series a committee reads."""
    frames = []
    for name, (fr, to) in HEADLINE_ROLLS.items():
        sub = rr[(rr.state_from == fr) & (rr.state_to == to)][
            ["ym", "rate_cnt", "rate_upb", "gap_share"]
        ].copy()
        sub["metric"] = name
        frames.append(sub)
    out = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    return out.sort_values(["metric", "ym"]).reset_index(drop=True)


def months_available(con, through: str) -> list[str]:
    return [r[0] for r in con.sql(
        "SELECT DISTINCT ym FROM fact_cube WHERE ym <= ? ORDER BY ym",
        params=[through]).fetchall()]


def flow_to_loss_12m(con, asof: str) -> float:
    """Credit-event terminations over the last 12 months against the 90+ stock
    at the start of that window.

    The delinquency definition (90+ days past due) and the loss definition
    (a credit-event termination) are different populations by construction.
    This ratio is the bridge between them: of the loans sitting at 90+ a year
    ago, how much actually turned into a disposition. It is the number that
    stops a committee reading a 90+ rate as if it were a loss rate.
    """
    months = months_available(con, asof)
    if len(months) < 13:
        return float("nan")
    window, base_month = months[-12:], months[-13]
    inlist = ", ".join(f"'{m}'" for m in window)
    ce = con.sql(f"""
        SELECT COALESCE(SUM(loans), 0) FROM fact_cube
        WHERE dim_name = 'portfolio' AND loan_state = 'CREDIT_EVENT'
          AND ym IN ({inlist})
    """).fetchone()[0]
    dq = con.sql("""
        SELECT COALESCE(SUM(loans), 0) FROM fact_cube
        WHERE dim_name = 'portfolio' AND loan_state = 'DPD90P' AND ym = ?
    """, params=[base_month]).fetchone()[0]
    return float(ce) / float(dq) if dq else float("nan")


# -------------------------------------------------------- concentration ----
def concentration(con, asof: str, dim: str) -> pd.DataFrame:
    return con.sql(f"""
        WITH s AS (
            SELECT dim_value, loan_state, loans, upb
            FROM fact_cube
            WHERE ym = '{asof}' AND dim_name = '{dim}'
              AND loan_state IN ({ACTIVE_SQL})
        ), a AS (
            SELECT dim_value,
                   SUM(loans) AS loans, SUM(upb) AS upb,
                   SUM(CASE WHEN loan_state='DPD90P' THEN upb END) AS dpd90p_upb,
                   SUM(CASE WHEN loan_state='DPD90P' THEN loans END) AS dpd90p_loans
            FROM s GROUP BY 1
        )
        SELECT dim_value,
               loans, upb,
               loans / SUM(loans) OVER () AS share_loans,
               upb   / SUM(upb)   OVER () AS share_upb,
               COALESCE(dpd90p_upb,0)   / NULLIF(upb,0)   AS dpd90p_rate_upb,
               COALESCE(dpd90p_loans,0) / NULLIF(loans,0) AS dpd90p_rate_cnt
        FROM a ORDER BY upb DESC
    """).df()


# ------------------------------------------------------ vintage curves -----
def vintage_curves(con) -> pd.DataFrame:
    """Cumulative ever-90+ and credit-event rates by origination year against
    months on book. Built from loan-level first-event ages, so a loan is
    counted once, at the age it first reached the state."""
    return con.sql("""
        WITH base AS (
            SELECT vintage_year, COUNT(*) AS orig_loans, SUM(original_upb) AS orig_upb
            FROM dim_loan GROUP BY 1
        ),
        ev AS (
            SELECT vintage_year, first_90p_age AS mob, COUNT(*) AS n90,
                   SUM(original_upb) AS upb90
            FROM dim_loan WHERE first_90p_age IS NOT NULL GROUP BY 1,2
        ),
        ce AS (
            SELECT vintage_year, term_age AS mob, COUNT(*) AS nce
            FROM dim_loan WHERE term_state='CREDIT_EVENT' AND term_age IS NOT NULL
            GROUP BY 1,2
        ),
        grid AS (
            SELECT b.vintage_year, m.mob, b.orig_loans, b.orig_upb
            FROM base b CROSS JOIN (SELECT UNNEST(GENERATE_SERIES(0, 275)) AS mob) m
        )
        SELECT g.vintage_year, g.mob, g.orig_loans,
               SUM(COALESCE(ev.n90,0))  OVER (PARTITION BY g.vintage_year
                                              ORDER BY g.mob) / g.orig_loans AS cum_ever90_rate,
               SUM(COALESCE(ce.nce,0))  OVER (PARTITION BY g.vintage_year
                                              ORDER BY g.mob) / g.orig_loans AS cum_credit_event_rate
        FROM grid g
        LEFT JOIN ev USING (vintage_year, mob)
        LEFT JOIN ce USING (vintage_year, mob)
        ORDER BY g.vintage_year, g.mob
    """).df()


# --------------------------------------------- denominator effect check ----
def denominator_check(con, through: str) -> pd.DataFrame:
    """The same numerator over two denominators.

    A delinquency rate measured on CURRENT balance falls whenever the book
    grows, and rises whenever it runs off, with no change in credit at all.
    Measuring the same 90+ balance against the balance 12 months earlier
    separates the credit signal from the denominator. On a runoff book the two
    diverge sharply, and reporting only the current-balance rate would overstate
    deterioration."""
    return con.sql(f"""
        WITH s AS (
            SELECT ym,
                   SUM(CASE WHEN loan_state IN ({ACTIVE_SQL}) THEN upb END) AS active_upb,
                   SUM(CASE WHEN loan_state = 'DPD90P' THEN upb END)        AS dpd90p_upb
            FROM fact_cube WHERE dim_name='portfolio' AND ym <= '{through}'
            GROUP BY 1
        )
        SELECT ym, active_upb, dpd90p_upb,
               dpd90p_upb / NULLIF(active_upb,0) AS rate_on_current,
               dpd90p_upb / NULLIF(LAG(active_upb,12) OVER (ORDER BY ym),0) AS rate_on_lagged_12m,
               active_upb / NULLIF(LAG(active_upb,12) OVER (ORDER BY ym),0) - 1 AS book_growth_12m
        FROM s ORDER BY ym
    """).df()
