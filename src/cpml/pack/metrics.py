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

import numpy as np
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
    """Transition rates out of each active state, labelled by the month the
    outcome is observed (the to-month)."""
    return con.sql(f"""
        WITH t AS (
            -- A roll rate for month t is the move from t-1 INTO t: the outcome
            -- is observed at t, so it is known at a month-t close. Labelling by
            -- the from-month (v1.0) used month t+1 data in a month-t pack.
            SELECT STRFTIME(STRPTIME(ym_from, '%Y%m') + INTERVAL 1 MONTH, '%Y%m') AS ym,
                   state_from, state_to, loans, upb_from
            FROM fact_transition
            WHERE STRFTIME(STRPTIME(ym_from, '%Y%m') + INTERVAL 1 MONTH, '%Y%m') <= '{through}'
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
    # execute(...).fetch* works on DuckDB 1.x and 2.x; relation.fetchone()
    # was removed in 2.0, so scalar reads go through execute() everywhere.
    return [r[0] for r in con.execute(
        "SELECT DISTINCT ym FROM fact_cube WHERE ym <= ? ORDER BY ym",
        [through]).fetchall()]


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
    ce = con.execute(f"""
        SELECT COALESCE(SUM(loans), 0) FROM fact_cube
        WHERE dim_name = 'portfolio' AND loan_state = 'CREDIT_EVENT'
          AND ym IN ({inlist})
    """).fetchone()[0]
    dq = con.execute("""
        SELECT COALESCE(SUM(loans), 0) FROM fact_cube
        WHERE dim_name = 'portfolio' AND loan_state = 'DPD90P' AND ym = ?
    """, [base_month]).fetchone()[0]
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
def _ym_index(ym: str) -> int:
    return int(ym[:4]) * 12 + int(ym[4:6])


def vintage_curves(con, asof: str) -> pd.DataFrame:
    """Cumulative ever-90+ and credit-event rates by origination year against
    months on book, using only information available at `asof`.

    Point-in-time rule: a loan contributes to month-on-book m only if it had
    reached age m by the as-of month, and an event counts only if it happened
    by then. The denominator at each m is therefore the set of loans old enough
    to have been observed at m - not the whole vintage - so young cohorts in a
    vintage year do not drag the curve down, and nothing after `asof` leaks in.
    (Version 1.0 of this function had no as-of filter, so a 2009 pack showed
    events through 2025. Fixed 2026-11-22; see docs/CHANGELOG.md.)
    """
    asof_idx = _ym_index(asof)
    age = (f"{asof_idx} - (CAST(SUBSTR(first_ym,1,4) AS INTEGER) * 12"
           f" + CAST(SUBSTR(first_ym,5,2) AS INTEGER))")
    # Aggregate in the database first: 13M loans collapse to a few hundred
    # thousand (vintage, age, event-age) cells, so the host never holds the
    # loan table in memory.
    obs = con.sql(f"""
        SELECT vintage_year, {age} AS a, COUNT(*) AS n
        FROM dim_loan WHERE first_ym <= '{asof}' GROUP BY 1, 2
    """).df()
    if obs.empty:
        return pd.DataFrame(columns=["vintage_year", "mob", "loans_observed",
                                     "orig_loans", "observed_share",
                                     "cum_ever90_rate", "cum_credit_event_rate"])
    events = {
        "cum_ever90_rate": con.sql(f"""
            SELECT vintage_year, {age} AS a, first_90p_age AS e, COUNT(*) AS n
            FROM dim_loan WHERE first_ym <= '{asof}' AND first_90p_age IS NOT NULL
            GROUP BY 1, 2, 3""").df(),
        "cum_credit_event_rate": con.sql(f"""
            SELECT vintage_year, {age} AS a, term_age AS e, COUNT(*) AS n
            FROM dim_loan WHERE first_ym <= '{asof}' AND term_state = 'CREDIT_EVENT'
              AND term_age IS NOT NULL
            GROUP BY 1, 2, 3""").df(),
    }
    out = []
    for vy, g in obs.groupby("vintage_year"):
        max_m = int(g["a"].max())
        m = np.arange(0, max_m + 1)
        cnt = np.zeros(max_m + 2)
        np.add.at(cnt, np.clip(g["a"].to_numpy(), 0, max_m + 1), g["n"].to_numpy())
        observed = cnt[::-1].cumsum()[::-1][: max_m + 1]          # loans with age >= m
        res = {"vintage_year": int(vy), "mob": m, "loans_observed": observed,
               "orig_loans": float(g["n"].sum())}
        for name, ev in events.items():
            e = ev[ev["vintage_year"] == vy]
            hist = np.zeros((max_m + 2, max_m + 2))
            if len(e):
                np.add.at(hist, (np.clip(e["e"].to_numpy(), 0, max_m + 1),
                                 np.clip(e["a"].to_numpy(), 0, max_m + 1)),
                          e["n"].to_numpy())
            # loans whose event age <= m <= age at as-of
            cum_e = hist.cumsum(axis=0)
            tail_a = cum_e[:, ::-1].cumsum(axis=1)[:, ::-1]
            num = tail_a[m, m]
            res[name] = np.divide(num, observed, out=np.zeros(len(m)), where=observed > 0)
        out.append(pd.DataFrame(res))
    df = pd.concat(out, ignore_index=True)
    df["observed_share"] = df["loans_observed"] / df["orig_loans"]
    return df[["vintage_year", "mob", "loans_observed", "orig_loans",
               "observed_share", "cum_ever90_rate", "cum_credit_event_rate"]]


def shift_share(con, asof: str, dim: str, lag: int = 12) -> pd.DataFrame:
    """Decompose the change in the balance-weighted 90+ rate over `lag` months
    into what happened INSIDE segments and what happened to the MIX of segments.

        overall rate R = sum_s share_s * rate_s
        dR = sum share0*drate   (rate effect: credit got worse inside segments)
           + sum dshare*rate0   (mix effect: the book shifted toward riskier segments)
           + sum dshare*drate   (interaction)

    The three columns sum exactly to the change in the headline rate, which the
    tests assert. This is the "why" a committee asks for: deterioration versus
    composition."""
    months = months_available(con, asof)
    if len(months) <= lag:
        return pd.DataFrame()
    m0, m1 = months[-1 - lag], months[-1]

    def seg(m):
        return con.sql(f"""
            SELECT dim_value,
                   SUM(upb) AS upb,
                   SUM(CASE WHEN loan_state='DPD90P' THEN upb ELSE 0 END) AS dq_upb
            FROM fact_cube
            WHERE ym = '{m}' AND dim_name = '{dim}' AND loan_state IN ({ACTIVE_SQL})
            GROUP BY 1
        """).df().set_index("dim_value")

    s0, s1 = seg(m0), seg(m1)
    df = s0.join(s1, how="outer", lsuffix="_0", rsuffix="_1").fillna(0.0)
    df["share_0"] = df["upb_0"] / df["upb_0"].sum()
    df["share_1"] = df["upb_1"] / df["upb_1"].sum()
    df["rate_0"] = np.where(df["upb_0"] > 0, df["dq_upb_0"] / df["upb_0"].where(df["upb_0"] > 0, 1), 0.0)
    df["rate_1"] = np.where(df["upb_1"] > 0, df["dq_upb_1"] / df["upb_1"].where(df["upb_1"] > 0, 1), 0.0)
    d_share = df["share_1"] - df["share_0"]
    d_rate = df["rate_1"] - df["rate_0"]
    df["rate_effect"] = df["share_0"] * d_rate
    df["mix_effect"] = d_share * df["rate_0"]
    df["interaction"] = d_share * d_rate
    df["contribution"] = df["rate_effect"] + df["mix_effect"] + df["interaction"]
    df["ym_from"], df["ym_to"], df["dim_name"] = m0, m1, dim
    return (df.reset_index()
              .sort_values("contribution", ascending=False)
              [["dim_name", "dim_value", "ym_from", "ym_to", "upb_0", "upb_1",
                "share_0", "share_1",
                "rate_0", "rate_1", "rate_effect", "mix_effect", "interaction",
                "contribution"]])


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
