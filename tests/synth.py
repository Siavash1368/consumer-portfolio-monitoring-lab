"""A tiny synthetic mart with hand-computable answers.

Unit tests run against this, not the 64 GB source, so they run anywhere in
seconds and every expected value can be checked with a calculator.
"""
from __future__ import annotations

import duckdb

MONTHS = [f"{y}{m:02d}" for y in (2020, 2021) for m in range(1, 13)][:14]  # 202001..202102


def _add(rows, ym, dim, val, state, loans, upb):
    rows.append((ym, state, dim, val, loans, upb, 0.0))


def synthetic_mart(planted_roll: float = 0.05, gap_loans: int = 300) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(":memory:")
    con.execute("""CREATE TABLE fact_cube (ym VARCHAR, loan_state VARCHAR, dim_name VARCHAR,
                   dim_value VARCHAR, loans BIGINT, upb DOUBLE, upb_removed DOUBLE)""")
    con.execute("""CREATE TABLE fact_transition (ym_from VARCHAR, state_from VARCHAR,
                   state_to VARCHAR, loans BIGINT, upb_from DOUBLE)""")
    con.execute("CREATE TABLE fact_control_totals (ym VARCHAR, loans BIGINT, upb DOUBLE)")
    rows = []
    # per month: 900 current, 50 at 30, 20 at 60, 30 at 90+  -> 90+ rate 3.0%
    book = {"CUR": 900, "DPD30": 50, "DPD60": 20, "DPD90P": 30}
    for ym in MONTHS:
        for st, n in book.items():
            upb = n * 1e6
            _add(rows, ym, "portfolio", "ALL", st, n, upb)
            # RA-02: one state holds 60% of every bucket -> breach of an 18% limit
            _add(rows, ym, "state", "AA", st, int(n * 0.6), upb * 0.6)
            _add(rows, ym, "state", "BB", st, n - int(n * 0.6), upb * 0.4)
            # RA-04: 5% of balance below 660 -> 50% of a 10% limit
            _add(rows, ym, "fico_band", "01 <620", st, int(n * 0.05), upb * 0.05)
            _add(rows, ym, "fico_band", "05 740-779", st, n - int(n * 0.05), upb * 0.95)
            _add(rows, ym, "ltv_band", "03 71-80", st, n, upb)
            _add(rows, ym, "occupancy", "P", st, n, upb)
            _add(rows, ym, "vintage_year", "2019", st, n, upb)
        con.execute("INSERT INTO fact_control_totals VALUES (?, ?, ?)",
                    [ym, sum(book.values()), sum(book.values()) * 1e6])
        # transitions OUT of this month; 'planted_roll' of current loans go to 30
        to30 = int(900 * planted_roll)
        trans = [("CUR", "CUR", 900 - to30), ("CUR", "DPD30", to30),
                 ("CUR", "REPORTING_GAP", gap_loans), ("CUR", "CENSORED", 10),
                 ("DPD30", "CUR", 20), ("DPD30", "DPD60", 20), ("DPD30", "DPD30", 10)]
        for fr, to, n in trans:
            con.execute("INSERT INTO fact_transition VALUES (?, ?, ?, ?, ?)",
                        [ym, fr, to, n, n * 1e6])
    con.executemany("INSERT INTO fact_cube VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
    return con


def synthetic_dim_loan() -> duckdb.DuckDBPyConnection:
    """Three loans from one vintage, all first observed Jan-2019."""
    con = duckdb.connect(":memory:")
    con.execute("""CREATE TABLE dim_loan (vintage_year INTEGER, first_ym VARCHAR,
                   first_90p_age INTEGER, term_state VARCHAR, term_age INTEGER)""")
    con.executemany("INSERT INTO dim_loan VALUES (?, ?, ?, ?, ?)", [
        (2019, "201901", 5, None, None),            # 90+ at age 5  -> known by Dec-2019
        (2019, "201901", 30, "CREDIT_EVENT", 36),   # 90+ at age 30 -> AFTER Dec-2019
        (2019, "201901", None, "PREPAID", 8),
    ])
    return con
