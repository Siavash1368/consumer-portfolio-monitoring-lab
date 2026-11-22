"""Build the monitoring mart from the Freddie Mac loan-level source.

Why a mart exists at all
------------------------
The source performance table is 933,443,821 loan-months in a 64 GB database on
a 3 GB / 2-core host. A single unindexed month filter over it takes ~115
seconds, which makes an interactive monthly pack impossible. This step reduces
it, once, to a set of small aggregate cubes (~10^5 rows) plus one loan-level
outcome table (~13 M rows). Every figure the pack publishes is then a
sub-second query, and the reduction itself is reproducible from this file.

How it stays tractable
----------------------
Freddie loan sequence numbers carry the vintage quarter as a prefix (F03Q1...),
and the source table was loaded in file order, so a half-open range filter on
loan_sequence_number is pruned by DuckDB's column zone maps. Chunking the build
on the 28 vintage quarters turns one impossible scan into 28 cheap ones and
caps peak memory.

Resumability
------------
Every (chunk, pass) that completes is written to build_log. Re-running skips
completed work, so an interrupted build continues instead of restarting.
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, __file__.rsplit("/src/", 1)[0] + "/src")

from cpml import config, db  # noqa: E402

CFG = config.load()

FICO = config.band_case("o.credit_score", "fico", ["9999"])
LTV = config.band_case("o.original_ltv", "ltv", ["999"])
TERM = config.band_case("o.original_loan_term", "term_months")

# Base projection over one vintage chunk of the source performance table.
BASE = f"""
    SELECT f.loan_sequence_number,
           f.monthly_reporting_period            AS ym,
           f.loan_age,
           COALESCE(f.current_actual_upb, 0)     AS upb,
           COALESCE(f.actual_loss, 0)            AS actual_loss,
           COALESCE(f.zero_balance_removal_upb, 0) AS upb_removed,
           {config.LOAN_STATE_CASE}              AS loan_state
    FROM src.performance f
    WHERE f.loan_sequence_number >= ?
      AND f.loan_sequence_number <  ?
"""

DIMS = [
    ("state", "o.property_state"),
    ("fico_band", FICO),
    ("ltv_band", LTV),
    ("purpose", "o.loan_purpose"),
    ("occupancy", "o.occupancy_status"),
    ("prop_type", "o.property_type"),
    ("channel", "o.channel"),
    ("term_band", TERM),
    ("vintage_year", "CAST(o.vintage_year AS VARCHAR)"),
]


def _cube_sql() -> str:
    """One scan, every marginal cube, via GROUPING SETS.

    The pack only ever needs one dimension at a time (concentration BY state,
    BY FICO band, ...), so marginals are the right shape - a full cross would
    be orders of magnitude larger and would never be read.
    """
    sel_dims = ",\n               ".join(
        f"COALESCE({expr}, 'Missing') AS {name}" for name, expr in DIMS
    )
    sets = ",\n            ".join(
        [f"(ym, loan_state, {name})" for name, _ in DIMS] + ["(ym, loan_state)"]
    )
    dim_name_case = "\n                 ".join(
        f"WHEN GROUPING({name}) = 0 THEN '{name}'" for name, _ in DIMS
    )
    dim_value_coalesce = ", ".join(name for name, _ in DIMS)
    return f"""
    INSERT INTO stg_cube
    WITH p AS ({BASE}),
    j AS (
        SELECT p.ym, p.loan_state, p.upb, p.upb_removed,
               {sel_dims}
        FROM p
        JOIN src.origination o USING (loan_sequence_number)
    )
    SELECT ym,
           loan_state,
           CASE {dim_name_case}
                ELSE 'portfolio' END AS dim_name,
           COALESCE({dim_value_coalesce}, 'ALL') AS dim_value,
           COUNT(*)          AS loans,
           SUM(upb)          AS upb,
           SUM(upb_removed)  AS upb_removed
    FROM j
    GROUP BY GROUPING SETS (
            {sets}
    )
    """


TRANS_SQL = f"""
    INSERT INTO stg_transition
    WITH p AS ({BASE}),
    s AS (
        SELECT loan_sequence_number, ym, loan_age, upb, loan_state,
               LEAD(loan_state) OVER w AS next_state,
               LEAD(loan_age)   OVER w AS next_age
        FROM p
        WINDOW w AS (PARTITION BY loan_sequence_number ORDER BY loan_age)
    )
    SELECT ym                                   AS ym_from,
           loan_state                           AS state_from,
           CASE WHEN next_state IS NULL              THEN 'CENSORED'
                WHEN next_age = loan_age + 1         THEN next_state
                ELSE 'REPORTING_GAP' END        AS state_to,
           COUNT(*)                             AS loans,
           SUM(upb)                             AS upb_from
    FROM s
    WHERE loan_state NOT IN ('PREPAID', 'CREDIT_EVENT', 'OTHER_DISP')
    GROUP BY 1, 2, 3
"""

LOAN_SQL = f"""
    INSERT INTO dim_loan
    WITH p AS ({BASE}),
    a AS (
        SELECT loan_sequence_number,
               MIN(ym)                                                            AS first_ym,
               MAX(ym)                                                            AS last_ym,
               MAX(loan_age)                                                      AS last_age,
               COUNT(*)                                                           AS obs_months,
               MIN(CASE WHEN loan_state = 'DPD90P' THEN loan_age END)             AS first_90p_age,
               MIN(CASE WHEN loan_state = 'DPD90P' THEN ym END)                   AS first_90p_ym,
               MAX(CASE WHEN loan_state IN ('PREPAID','CREDIT_EVENT','OTHER_DISP')
                        THEN loan_state END)                                      AS term_state,
               MAX(CASE WHEN loan_state IN ('PREPAID','CREDIT_EVENT','OTHER_DISP')
                        THEN loan_age END)                                        AS term_age,
               MAX(CASE WHEN loan_state IN ('PREPAID','CREDIT_EVENT','OTHER_DISP')
                        THEN ym END)                                              AS term_ym,
               SUM(actual_loss)                                                   AS actual_loss
        FROM p
        GROUP BY 1
    )
    SELECT a.loan_sequence_number,
           o.vintage_quarter,
           o.vintage_year,
           o.property_state                              AS state,
           COALESCE({FICO}, 'Missing')                   AS fico_band,
           COALESCE({LTV}, 'Missing')                    AS ltv_band,
           COALESCE(o.loan_purpose, 'Missing')           AS purpose,
           COALESCE(o.occupancy_status, 'Missing')       AS occupancy,
           COALESCE(o.property_type, 'Missing')          AS prop_type,
           COALESCE(o.channel, 'Missing')                AS channel,
           COALESCE({TERM}, 'Missing')                   AS term_band,
           o.credit_score, o.original_ltv, o.original_dti,
           o.original_upb, o.original_interest_rate, o.original_loan_term,
           a.first_ym, a.last_ym, a.last_age, a.obs_months,
           a.first_90p_age, a.first_90p_ym,
           a.term_state, a.term_age, a.term_ym,
           a.actual_loss
    FROM a
    JOIN src.origination o USING (loan_sequence_number)
"""

# Deliberately a separate, simpler query path from the cube: it reads the raw
# columns straight off the source with no origination join and no state
# derivation. If the cube and this disagree, the join or the CASE logic is
# wrong - which is exactly what the reconciliation control is for.
CONTROL_SQL = """
    INSERT INTO stg_control
    SELECT monthly_reporting_period            AS ym,
           COUNT(*)                            AS loans,
           SUM(COALESCE(current_actual_upb,0)) AS upb
    FROM src.performance
    WHERE loan_sequence_number >= ?
      AND loan_sequence_number <  ?
    GROUP BY 1
"""

DDL = """
CREATE TABLE IF NOT EXISTS build_log (
    chunk VARCHAR, pass VARCHAR, rows_written BIGINT,
    seconds DOUBLE, finished_at TIMESTAMP, config_version VARCHAR);
CREATE TABLE IF NOT EXISTS stg_cube (
    ym VARCHAR, loan_state VARCHAR, dim_name VARCHAR, dim_value VARCHAR,
    loans BIGINT, upb DOUBLE, upb_removed DOUBLE);
CREATE TABLE IF NOT EXISTS stg_transition (
    ym_from VARCHAR, state_from VARCHAR, state_to VARCHAR,
    loans BIGINT, upb_from DOUBLE);
CREATE TABLE IF NOT EXISTS dim_loan (
    loan_sequence_number VARCHAR, vintage_quarter VARCHAR, vintage_year INTEGER,
    state VARCHAR, fico_band VARCHAR, ltv_band VARCHAR, purpose VARCHAR,
    occupancy VARCHAR, prop_type VARCHAR, channel VARCHAR, term_band VARCHAR,
    credit_score INTEGER, original_ltv INTEGER, original_dti INTEGER,
    original_upb DOUBLE, original_interest_rate DOUBLE, original_loan_term INTEGER,
    first_ym VARCHAR, last_ym VARCHAR, last_age INTEGER, obs_months BIGINT,
    first_90p_age INTEGER, first_90p_ym VARCHAR,
    term_state VARCHAR, term_age INTEGER, term_ym VARCHAR, actual_loss DOUBLE);
CREATE TABLE IF NOT EXISTS stg_control (
    ym VARCHAR, loans BIGINT, upb DOUBLE);
CREATE TABLE IF NOT EXISTS fact_control_totals (
    ym VARCHAR, loans BIGINT, upb DOUBLE);
"""

PASSES = {
    "cube": _cube_sql,
    "trans": lambda: TRANS_SQL,
    "loan": lambda: LOAN_SQL,
    "control": lambda: CONTROL_SQL,
}


def _exec_script(con, script: str) -> None:
    """DuckDB's python API takes one statement at a time."""
    for stmt in [x.strip() for x in script.split(";") if x.strip()]:
        con.execute(stmt)


def _done(con, chunk: str, pas: str) -> bool:
    return con.execute(
        "SELECT COUNT(*) FROM build_log WHERE chunk=? AND pass=?", [chunk, pas]
    ).fetchone()[0] > 0


def run(passes: list[str], vintages: list[str], reset: bool = False,
        time_budget: float = 0.0) -> None:
    # The mart is the default catalog (writable); the 64 GB source is attached
    # read-only beside it and is always referenced as src.<table>.
    con = db.mart(read_only=False)
    _exec_script(con, DDL)
    if reset:
        # Drop rather than DELETE so a schema change is picked up, and because
        # the build host does not permit deleting the database file itself.
        for t in ("build_log", "stg_cube", "stg_transition", "dim_loan",
                  "stg_control", "fact_control_totals", "fact_cube",
                  "fact_transition"):
            con.execute(f"DROP TABLE IF EXISTS {t}")
        _exec_script(con, DDL)
        print("[reset] mart tables dropped and recreated", flush=True)
    con.execute(
        f"ATTACH IF NOT EXISTS '{config.abspath(CFG['source']['freddie_db'])}' "
        "AS src (READ_ONLY)"
    )

    started = time.time()
    order = {"cube": 0, "trans": 1, "loan": 2, "control": 3}
    planned = [(c, p) for p in sorted(passes, key=lambda x: order.get(x, 9))
               for v in vintages for c in config.chunks_for(p, v)]
    todo = [(c, p) for c, p in planned if not _done(con, c, p)]
    done_before = len(planned) - len(todo)
    print(f"[plan] {len(todo)} work items pending, {done_before} already done",
          flush=True)

    # Pack each invocation as full as is safe: never START an item that the
    # worst observed runtime for its pass says could overrun the wall clock.
    est = dict(con.sql(
        "SELECT pass, MAX(seconds) FROM build_log GROUP BY 1").fetchall())
    pending = list(todo)
    while pending:
        elapsed = time.time() - started
        room = (time_budget - elapsed) if time_budget else float("inf")
        # Greedy longest-fit: run the most expensive item that still fits in the
        # remaining wall clock, so a call is not left idle because the next item
        # in list order happens to be a big one.
        idx = next((i for i, (_, p) in enumerate(pending)
                    if est.get(p, 120.0) <= room), None)
        if idx is None:
            cheapest = min(est.get(p, 120.0) for _, p in pending)
            print(f"[budget] {elapsed:.0f}s used, {room:.0f}s left; cheapest "
                  f"pending item needs ~{cheapest:.0f}s - stopping", flush=True)
            break
        prefix, pas = pending.pop(idx)
        lo, hi = config.vintage_range(prefix)
        t0 = time.time()
        n = con.execute(PASSES[pas](), [lo, hi]).fetchone()[0]
        _log(con, prefix, pas, t0, n)
        est[pas] = max(est.get(pas, 0.0), time.time() - t0)

    total = sum(len(config.chunks_for(p, v))
                for v in config.VINTAGE_PREFIXES
                for p in ("cube", "trans", "loan", "control"))
    done_now = con.execute("SELECT COUNT(*) FROM build_log").fetchone()[0]
    print(f"[state] {total - done_now} of {total} total work items remain",
          flush=True)
    con.close()


def _log(con, chunk: str, pas: str, t0: float, rows: int) -> None:
    secs = time.time() - t0
    con.execute(
        "INSERT INTO build_log VALUES (?,?,?,?,?,?)",
        [chunk, pas, int(rows), secs, datetime.now(timezone.utc), CFG["config_version"]],
    )
    print(f"[done] {chunk:7s} {pas:8s} {secs:7.1f}s  {int(rows):>12,} rows", flush=True)


def finalize() -> None:
    """Collapse staging into the published fact tables."""
    con = db.mart(read_only=False)
    _exec_script(con, """
        CREATE OR REPLACE TABLE fact_cube AS
        SELECT ym, loan_state, dim_name, dim_value,
               SUM(loans) AS loans, SUM(upb) AS upb,
               SUM(upb_removed) AS upb_removed
        FROM stg_cube GROUP BY 1,2,3,4;
        CREATE OR REPLACE TABLE fact_transition AS
        SELECT ym_from, state_from, state_to,
               SUM(loans) AS loans, SUM(upb_from) AS upb_from
        FROM stg_transition GROUP BY 1,2,3;
        CREATE OR REPLACE TABLE fact_control_totals AS
        SELECT ym, SUM(loans) AS loans, SUM(upb) AS upb
        FROM stg_control GROUP BY 1;
    """)
    for t in ("fact_cube", "fact_transition", "dim_loan", "fact_control_totals"):
        n = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        print(f"  {t:22s} {n:>12,} rows")
    con.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Build the monitoring mart.")
    ap.add_argument("--passes", default="cube,trans,loan,control")
    ap.add_argument("--vintages", default="all")
    ap.add_argument("--reset", action="store_true")
    ap.add_argument("--finalize", action="store_true")
    ap.add_argument("--time-budget", type=float, default=0.0,
                    help="stop launching new work items after N seconds")
    a = ap.parse_args()
    vs = config.VINTAGE_PREFIXES if a.vintages == "all" else a.vintages.split(",")
    if a.finalize:
        finalize()
    else:
        run(a.passes.split(","), vs, reset=a.reset, time_budget=a.time_budget)
