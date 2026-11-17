"""Database access.

Two databases, and the distinction matters for the control story:

  * the source modelling database is opened READ ONLY, always. The lab never
    writes to it, so nothing here can corrupt the upstream project.
  * the monitoring mart is the lab's own small database, rebuilt from source
    and safe to delete at any time.
"""
from __future__ import annotations

import os

import duckdb

from . import config


def _tune(con: duckdb.DuckDBPyConnection) -> duckdb.DuckDBPyConnection:
    # The build host is small (3 GB RAM, 2 cores). Spill deliberately rather
    # than fail, and keep the spill off the synced project folder.
    con.sql("SET threads=2")
    con.sql("SET memory_limit='2GB'")
    con.sql("SET preserve_insertion_order=false")
    # Spill target. The 749M-row window sorts spill several GB, which is more
    # than the OS temp volume holds, so the default is a scratch folder beside
    # the mart on the data drive. It is gitignored and safe to delete.
    tmp = os.environ.get("CPML_TMPDIR", "/tmp/cpml_duck")
    os.makedirs(tmp, exist_ok=True)
    con.sql(f"SET temp_directory='{tmp}'")
    con.sql("SET max_temp_directory_size='3GiB'")
    return con


def source(read_only: bool = True) -> duckdb.DuckDBPyConnection:
    cfg = config.load()
    path = config.abspath(cfg["source"]["freddie_db"])
    if not os.path.exists(path):
        raise FileNotFoundError(f"source database not found: {path}")
    return _tune(duckdb.connect(path, read_only=read_only))


def mart(read_only: bool = False) -> duckdb.DuckDBPyConnection:
    cfg = config.load()
    path = config.abspath(cfg["source"]["mart_db"])
    os.makedirs(os.path.dirname(path), exist_ok=True)
    return _tune(duckdb.connect(path, read_only=read_only))


def mart_path() -> str:
    return config.abspath(config.load()["source"]["mart_db"])
