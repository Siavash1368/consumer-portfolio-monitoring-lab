"""Integration tests on the real mart. Skipped where the mart is not built
(for example in CI, where the licensed source data is not available)."""
import _paths  # noqa: F401

import os
import unittest

from cpml import db
from cpml.pack import controls, metrics


def _mart():
    if not os.path.exists(db.mart_path()):
        raise unittest.SkipTest("monitoring mart not built on this machine")
    return db.mart(read_only=True)


def test_reconciliation_has_no_unexplained_breaks():
    rec = controls.reconcile(_mart(), "202509")
    s = controls.summarise(rec)
    assert s["fail"] == 0, rec[rec.status == "FAIL"]
    assert sorted(rec[rec.status == "EXPLAINED"].ym) == ["200608", "200609"]
    assert s["pass"] == 272


def test_no_negative_balances_or_counts():
    con = _mart()
    neg = con.execute("""SELECT COUNT(*) FROM fact_cube WHERE upb < 0 OR loans < 0
                     OR upb_removed < 0""").fetchone()[0]
    assert neg == 0


def test_real_transition_rows_sum_to_one():
    rr = metrics.roll_rates(_mart(), "200906")
    sums = rr.groupby(["ym", "state_from"])["rate_cnt"].sum()
    assert ((sums - 1.0).abs() < 1e-9).all()


def test_attribution_sums_to_headline_change():
    con = _mart()
    ss = metrics.shift_share(con, "200906", "state")
    t = metrics.add_rates(metrics.portfolio_trend(con, "200906")).set_index("ym")
    change = t.loc["200906", "dpd90p_rate_upb"] - t.loc["200806", "dpd90p_rate_upb"]
    assert abs(ss.contribution.sum() - change) < 1e-12


def test_2009_pack_sees_no_later_data():
    con = _mart()
    vc = metrics.vintage_curves(con, "200906")
    assert vc[vc.vintage_year == 2007].mob.max() <= 30
    assert metrics.roll_rates(con, "200906").ym.max() == "200906"
