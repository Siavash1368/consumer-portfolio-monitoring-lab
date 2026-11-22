"""Unit tests on the synthetic mart. Each expected value is computable by hand."""
import _paths  # noqa: F401

import math

from synth import MONTHS, synthetic_dim_loan, synthetic_mart

from cpml.pack import controls, limits, metrics

ASOF = MONTHS[-1]  # 202102


def test_reporting_gaps_excluded_from_roll_rate_denominator():
    rr = metrics.roll_rates(synthetic_mart(planted_roll=0.05, gap_loans=300), ASOF)
    row = rr[(rr.ym == ASOF) & (rr.state_from == "CUR") & (rr.state_to == "DPD30")].iloc[0]
    # 45 of 900 real outcomes; the 300 gap rows and 10 censored rows are not outcomes
    assert math.isclose(row.rate_cnt, 45 / 900)
    assert math.isclose(row.gap_share, 300 / (900 + 300 + 10))
    assert not (rr.state_to.isin(["REPORTING_GAP", "CENSORED"])).any()


def test_transition_rows_sum_to_one():
    rr = metrics.roll_rates(synthetic_mart(), ASOF)
    sums = rr.groupby(["ym", "state_from"])["rate_cnt"].sum()
    assert ((sums - 1.0).abs() < 1e-12).all(), sums


def test_roll_rate_has_no_look_ahead():
    # the transition recorded FROM the as-of month needs next month's data,
    # so it must not appear in an as-of pack
    rr = metrics.roll_rates(synthetic_mart(), ASOF)
    assert rr.ym.max() == ASOF
    assert set(rr.ym) == set(MONTHS[1:])   # first month has no prior month to roll from


def test_limit_utilisation_and_breach():
    lim = limits.limit_utilisation(synthetic_mart(), ASOF).set_index("limit_id")
    assert math.isclose(lim.loc["RA-01", "measured"], 0.03)          # 30 / 1000
    assert math.isclose(lim.loc["RA-01", "utilisation"], 0.03 / 0.04)
    assert lim.loc["RA-01", "status"] == "WITHIN"
    assert math.isclose(lim.loc["RA-02", "measured"], 0.6)            # largest state
    assert lim.loc["RA-02", "status"] == "BREACH"
    assert math.isclose(lim.loc["RA-04", "utilisation"], 0.05 / 0.10)
    assert lim.loc["RA-04", "status"] == "WITHIN"


def test_trigger_fires_on_planted_breach():
    hot = limits.evaluate_triggers(synthetic_mart(planted_roll=0.05), ASOF).set_index("trigger_id")
    assert hot.loc["EW-02", "status"] == "RED"            # 5.0% vs red 2.5%
    assert hot.loc["EW-02", "action"] != ""
    calm = limits.evaluate_triggers(synthetic_mart(planted_roll=0.01), ASOF).set_index("trigger_id")
    assert calm.loc["EW-02", "status"] == "GREEN"          # 1.0% vs amber 1.5%
    assert calm.loc["EW-02", "action"] == ""


def test_reconciliation_ties_out():
    rec = controls.reconcile(synthetic_mart(), ASOF)
    assert (rec.status == "PASS").all()
    assert controls.summarise(rec)["publishable"]


def test_reconciliation_fails_on_planted_break():
    con = synthetic_mart()
    con.execute("UPDATE fact_control_totals SET loans = loans + 3 WHERE ym = ?", [MONTHS[5]])
    rec = controls.reconcile(con, ASOF).set_index("ym")
    assert rec.loc[MONTHS[5], "status"] == "FAIL"
    assert not controls.summarise(rec.reset_index())["publishable"]


def test_documented_exception_excuses_only_its_exact_difference():
    con = synthetic_mart()
    # 200608 is a documented exception: exactly 1 loan and $42,000
    con.execute("INSERT INTO fact_cube VALUES ('200608','CUR','portfolio','ALL',10,1e6,0)")
    con.execute("INSERT INTO fact_control_totals VALUES ('200608', 11, 1e6 + 42000)")
    rec = controls.reconcile(con, ASOF).set_index("ym")
    assert rec.loc["200608", "status"] == "EXPLAINED"
    con.execute("UPDATE fact_control_totals SET loans = 12 WHERE ym = '200608'")
    rec = controls.reconcile(con, ASOF).set_index("ym")
    assert rec.loc["200608", "status"] == "FAIL"


def test_vintage_curve_is_point_in_time():
    vc = metrics.vintage_curves(synthetic_dim_loan(), "201912")
    assert vc.mob.max() == 11                      # Jan-2019 to Dec-2019
    at11 = vc[vc.mob == 11].iloc[0]
    # one of three loans reached 90+ by age 11; the age-30 event is in the future
    assert math.isclose(at11.cum_ever90_rate, 1 / 3)
    assert at11.cum_credit_event_rate == 0.0       # the credit event is at age 36
    later = metrics.vintage_curves(synthetic_dim_loan(), "202201")
    assert math.isclose(later[later.mob == 30].iloc[0].cum_ever90_rate, 2 / 3)
