"""Configuration loader.

Single source of truth for every threshold, band and limit used by the pack.
Nothing downstream is allowed to hard-code one of these numbers; the loader
exposes them and also builds the SQL CASE expressions for the segment bands so
the band edges in the YAML are literally the band edges in the queries.
"""
from __future__ import annotations

import os
from functools import lru_cache
from typing import Any, Dict, List

import yaml

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
CONFIG_PATH = os.path.join(REPO_ROOT, "config", "monitoring_config.yaml")


@lru_cache(maxsize=1)
def load(path: str = CONFIG_PATH) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def abspath(relative: str) -> str:
    """Resolve a config path relative to the repo root."""
    return os.path.abspath(os.path.join(REPO_ROOT, relative))


def band_case(column: str, band_key: str, missing_sentinels: List[str] | None = None) -> str:
    """Build a SQL CASE expression from the band edges declared in the YAML.

    The band edges exist in exactly one place. If someone moves 660 to 670 in
    the config, every query moves with it and the pack's provenance line
    records the new config_version.
    """
    cfg = load()
    bands = cfg["bands"][band_key]
    parts = ["CASE"]
    if missing_sentinels:
        sentinel_sql = ", ".join(missing_sentinels)
        parts.append(f"WHEN {column} IS NULL OR {column} IN ({sentinel_sql}) THEN '99 Missing'")
    else:
        parts.append(f"WHEN {column} IS NULL THEN '99 Missing'")
    for b in bands:
        parts.append(f"WHEN {column} BETWEEN {b['lo']} AND {b['hi']} THEN '{b['label']}'")
    parts.append("ELSE '99 Missing' END")
    return "\n           ".join(parts)


# --- status / termination vocabulary -------------------------------------
# One definition, used by the mart, the pack and the tests.
LOAN_STATE_CASE = """
        CASE
          WHEN zero_balance_code = '01' THEN 'PREPAID'
          WHEN zero_balance_code IN ('02','03','09') THEN 'CREDIT_EVENT'
          WHEN zero_balance_code IN ('15','16','96') THEN 'OTHER_DISP'
          WHEN current_loan_delinquency_status IS NULL
               OR current_loan_delinquency_status = 'XX' THEN 'UNK'
          WHEN current_loan_delinquency_status = 'RA' THEN 'DPD90P'
          WHEN TRY_CAST(current_loan_delinquency_status AS INTEGER) = 0 THEN 'CUR'
          WHEN TRY_CAST(current_loan_delinquency_status AS INTEGER) = 1 THEN 'DPD30'
          WHEN TRY_CAST(current_loan_delinquency_status AS INTEGER) = 2 THEN 'DPD60'
          WHEN TRY_CAST(current_loan_delinquency_status AS INTEGER) >= 3 THEN 'DPD90P'
          ELSE 'UNK'
        END
"""

ACTIVE_STATES = ("CUR", "DPD30", "DPD60", "DPD90P", "UNK")
TERMINAL_STATES = ("PREPAID", "CREDIT_EVENT", "OTHER_DISP")
DELINQUENT_STATES = ("DPD30", "DPD60", "DPD90P")

# The 28 Freddie Mac vintage quarters present in the source, as loan-sequence
# prefixes. Chunking the 933M-row performance table on these ranges lets the
# column zone maps prune the scan, which is what makes the mart build tractable
# on a small machine.
VINTAGE_PREFIXES = [f"F{yy:02d}Q{q}" for yy in range(3, 10) for q in range(1, 5)]


def vintage_range(prefix: str) -> tuple[str, str]:
    """Half-open [lo, hi) loan-sequence-number range for a chunk id.

    A 5-character id ('F03Q1') is a whole vintage quarter. A 6-character id
    ('F03Q17') is one tenth of it, split on the first digit of the sequence
    number. The window pass uses the smaller chunks so its sort stays inside
    memory instead of spilling several GB to disk; the aggregate passes use the
    whole quarter, which is cheaper.
    """
    yy, q = int(prefix[1:3]), int(prefix[4])
    nxt = f"F{yy:02d}Q{q + 1}" if q < 4 else f"F{yy + 1:02d}Q1"
    if len(prefix) == 5:
        return prefix, nxt
    # sub-chunk: increment the numeric tail, carrying into the next vintage
    tail = prefix[5:]
    nxt_tail = str(int(tail) + 1).zfill(len(tail))
    return prefix, (nxt if len(nxt_tail) > len(tail) else prefix[:5] + nxt_tail)


# The window pass is sub-chunked; everything else runs a whole quarter.
SUBCHUNK_PASSES = ("trans",)


def chunks_for(pass_name: str, prefix: str) -> list[str]:
    if pass_name in SUBCHUNK_PASSES:
        # Sequence numbers within a quarter cluster on the leading digit, so a
        # one-digit split leaves one chunk holding ~90% of the rows. Two digits
        # gives pieces small enough that the sort never leaves memory; the
        # empty ones are pruned by the zone maps and cost nothing.
        return [f"{prefix}{d1}{d2}" for d1 in range(2) for d2 in range(10)]
    return [prefix]
