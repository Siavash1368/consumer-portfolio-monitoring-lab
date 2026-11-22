"""Committee one-pager: what changed, where, why, so what.

Every number on the page is computed here from the mart, through the same
metric functions the pack publishes, and every sentence is a template filled
from those numbers. Nothing is typed in by hand, so the page for any as-of
month is reproducible with:

    python src/cpml/report/summary.py --asof 200906

Scope reminder printed on the page: public Freddie Mac single-family data,
illustrative limits and triggers.
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))

import pandas as pd  # noqa: E402

from cpml import config, db  # noqa: E402
from cpml.pack import controls, limits, metrics  # noqa: E402

CFG = config.load()
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def label(ym: str) -> str:
    return f"{MONTHS[int(ym[4:6]) - 1]}-{ym[:4]}"


def pct(x: float, d: int = 2) -> str:
    return "n/a" if pd.isna(x) else f"{x * 100:.{d}f}%"


def bp(x: float) -> str:
    if pd.isna(x):
        return "n/a"
    v = round(x * 1e4)
    return "0 bp" if v == 0 else f"{v:+.0f} bp"


def pp(x: float) -> str:
    if pd.isna(x):
        return "n/a"
    v = round(x * 100, 1)
    return "0.0 pp" if v == 0 else f"{v:+.1f} pp"


def unemployment(ym: str) -> float:
    path = config.abspath(CFG["reporting"]["macro_unemployment_csv"])
    if not os.path.exists(path):
        return float("nan")
    u = pd.read_csv(path)
    key = f"{ym[:4]}-{ym[4:6]}-01"
    row = u[u["observation_date"] == key]
    return float(row["UNRATE"].iloc[0]) / 100 if len(row) else float("nan")


def build(asof: str) -> str:
    con = db.mart(read_only=True)
    months = metrics.months_available(con, asof)
    if len(months) < 13:
        raise SystemExit("the one-pager needs 12 months of history before the as-of month")
    m3, m12 = months[-4], months[-13]

    trend = metrics.add_rates(metrics.portfolio_trend(con, asof)).set_index("ym")
    rr = metrics.headline_rolls(metrics.roll_rates(con, asof))

    def roll(metric: str, ym: str) -> float:
        s = rr[(rr.metric == metric) & (rr.ym == ym)]
        return float(s["rate_cnt"].iloc[0]) if len(s) else float("nan")

    def trow(col: str, ym: str) -> float:
        return float(trend.loc[ym, col]) if ym in trend.index else float("nan")

    ftl_now = metrics.flow_to_loss_12m(con, asof)
    ftl_then = metrics.flow_to_loss_12m(con, m12)
    window = months[-12:]
    ce_12m = trend.loc[window, "credit_event_loans"].sum()
    ce_upb_12m = trend.loc[window, "credit_event_upb"].sum()

    by_state = metrics.shift_share(con, asof, "state")
    by_vint = metrics.shift_share(con, asof, "vintage_year")
    d_total = trow("dpd90p_rate_upb", asof) - trow("dpd90p_rate_upb", m12)
    up = d_total >= 0
    # rank contributors in the direction the headline actually moved
    top_states = (by_state.sort_values("contribution", ascending=not up)).head(5)
    top5_share = top_states["contribution"].sum() / d_total if d_total else float("nan")

    vc = metrics.vintage_curves(con, asof)
    thr = float(CFG["reporting"]["vintage_min_observed_share"])
    vc_ok = vc[vc["observed_share"] >= thr]

    den = metrics.denominator_check(con, asof).set_index("ym")
    r_cur, r_lag = den.loc[asof, "rate_on_current"], den.loc[asof, "rate_on_lagged_12m"]
    growth = den.loc[asof, "book_growth_12m"]
    u_now, u_then = unemployment(asof), unemployment(m12)

    lim = limits.limit_utilisation(con, asof)
    trg = limits.evaluate_triggers(con, asof)
    rec = controls.summarise(controls.reconcile(con, asof))
    con.close()

    # ------------------------------------------------------------------ page
    L = []
    L.append(f"# Portfolio Monitoring — Committee Summary, {label(asof)}")
    L.append("")
    L.append(f"**{CFG['source']['portfolio_name']}** · as of {label(asof)} · "
             f"config {CFG['config_version']} · generated "
             f"{datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC · "
             f"*public data; limits and triggers are illustrative*")
    L.append("")
    L.append(f"| Metric | {label(asof)} | {label(m3)} | {label(m12)} | 12-month change |")
    L.append("|---|---:|---:|---:|---:|")
    rows = [
        ("Active loans", lambda y: f"{trow('active_loans', y):,.0f}", None),
        ("Active balance ($bn)", lambda y: f"{trow('active_upb', y) / 1e9:,.1f}", None),
        ("90+ DPD rate (balance)", lambda y: pct(trow("dpd90p_rate_upb", y)), "dpd90p_rate_upb"),
        ("30–89 DPD rate (balance)", lambda y: pct(trow("dpd30_rate_upb", y) + trow("dpd60_rate_upb", y)), None),
        ("Current → 30 roll", lambda y: pct(roll("roll_cur_to_30", y)), "roll_cur_to_30"),
        ("30 → 60 roll", lambda y: pct(roll("roll_30_to_60", y), 1), "roll_30_to_60"),
        ("Cure, 30 DPD → current", lambda y: pct(roll("cure_30_to_cur", y), 1), "cure_30_to_cur"),
    ]
    for name, fmt, key in rows:
        if key is None:
            if name.startswith("30–89"):
                chg = bp((trow("dpd30_rate_upb", asof) + trow("dpd60_rate_upb", asof))
                         - (trow("dpd30_rate_upb", m12) + trow("dpd60_rate_upb", m12)))
            elif name.startswith("Active balance"):
                chg = f"{growth * 100:+.1f}%"
            else:
                chg = f"{(trow('active_loans', asof) / trow('active_loans', m12) - 1) * 100:+.1f}%"
        elif key == "roll_cur_to_30":
            chg = bp(roll(key, asof) - roll(key, m12))
        elif key.startswith("roll") or key.startswith("cure"):
            chg = pp(roll(key, asof) - roll(key, m12))
        else:
            chg = bp(trow(key, asof) - trow(key, m12))
        L.append(f"| {name} | {fmt(asof)} | {fmt(m3)} | {fmt(m12)} | {chg} |")
    L.append(f"| Flow to loss, trailing 12m | {pct(ftl_now, 1)} | | {pct(ftl_then, 1)} | {pp(ftl_now - ftl_then)} |")
    L.append("")

    # what changed
    L.append("## 1 · What changed")
    d3 = trow("dpd90p_rate_upb", asof) - trow("dpd90p_rate_upb", m3)
    L.append(f"- **90+ delinquency {'rose' if up else 'fell'} from {pct(trow('dpd90p_rate_upb', m12))} to "
             f"{pct(trow('dpd90p_rate_upb', asof))} of balance ({bp(d_total)} in 12 months, "
             f"{bp(d3)} in the last 3).**")
    L.append(f"- Early-stage inflow is {pct(roll('roll_cur_to_30', asof))} a month; of loans at 30 days, "
             f"{pct(roll('roll_30_to_60', asof), 1)} roll to 60 and {pct(roll('cure_30_to_cur', asof), 1)} cure "
             f"(cure was {pct(roll('cure_30_to_cur', m12), 1)} a year ago).")
    L.append(f"- {ce_12m:,.0f} loans left the book through a credit event in the last 12 months "
             f"(${ce_upb_12m / 1e9:,.1f}bn of balance removed).")
    L.append("")

    # where
    L.append("## 2 · Where")
    L.append(f"Five states explain **{pct(top5_share, 0)}** of the 12-month "
             f"{'rise' if up else 'fall'} in the 90+ rate:")
    L.append("")
    L.append("| State | Share of balance | 90+ rate now | 90+ rate 12m ago | Contribution |")
    L.append("|---|---:|---:|---:|---:|")
    for _, r in top_states.iterrows():
        L.append(f"| {r.dim_value} | {pct(r.share_1, 1)} | {pct(r.rate_1)} | {pct(r.rate_0)} | {bp(r.contribution)} |")
    L.append("")

    # why
    rate_eff = by_vint["rate_effect"].sum()
    mix_eff = by_vint["mix_effect"].sum() + by_vint["interaction"].sum()
    # the vintage that moved the rate most, in the direction it moved
    worst = by_vint.sort_values("rate_effect", ascending=not up).iloc[0]
    newest = by_vint.sort_values("dim_value").iloc[-1]
    L.append("## 3 · Why")
    lead = ("Deterioration, not mix" if up and abs(rate_eff) >= abs(mix_eff)
            else "Mix, not deterioration" if up
            else "Improvement inside vintages" if abs(rate_eff) >= abs(mix_eff)
            else "Run-off mix, not improvement")
    L.append(f"- **{lead}.** Split by vintage, the change *inside* vintages contributed "
             f"{bp(rate_eff)}; mix shifts (with interaction) contributed {bp(mix_eff)}. The largest single "
             f"driver is the {worst.dim_value} vintage (90+ rate {pct(worst.rate_0)} → {pct(worst.rate_1)}).")
    # only call it new lending if the vintage's balance actually grew in dollars;
    # a rising share on a shrinking book is just everyone else paying down
    if newest.upb_1 > newest.upb_0 and newest.share_1 > newest.share_0:
        L.append(f"- **New lending is masking it.** The {newest.dim_value} vintage grew to "
                 f"{pct(newest.share_1, 1)} of balance at a 90+ rate of {pct(newest.rate_1)}, which holds the "
                 f"headline rate down.")
    # Compare at the oldest age the worst-driver vintage has reached with at
    # least the configured share of its loans observed, against every older
    # vintage at that same age. The sentence states only what the numbers show.
    wv = int(worst.dim_value)
    reach = vc_ok[vc_ok["vintage_year"] == wv]["mob"]
    if up and len(reach):
        cmp_m = int(reach.max())
        at_m = (vc_ok[(vc_ok["mob"] == cmp_m) & (vc_ok["vintage_year"] <= wv)]
                .set_index("vintage_year")["cum_ever90_rate"].sort_index())
        sv = ", ".join(f"{int(v)} {pct(at_m[v], 1)}" for v in at_m.index)
        older = at_m.drop(wv)
        if len(older) and at_m[wv] > older.max():
            base_v = int(older.idxmin())
            verdict = (f"The {wv} vintage is the worst at that age — "
                       f"{at_m[wv] / older.min():.1f}x the {base_v} vintage — so this is "
                       f"underwriting quality, not just seasoning.")
        else:
            verdict = (f"The {wv} vintage is not the worst at that age, so its rise is "
                       f"mostly seasoning, not weaker underwriting.")
        if len(at_m) >= 3:
            L.append(f"- **Seasoning.** At {cmp_m} months on book, cumulative ever-90+ by "
                     f"vintage: {sv}. {verdict}")
    L.append(f"- **Denominator.** On balance 12 months earlier the 90+ rate is {pct(r_lag)} "
             f"vs {pct(r_cur)} on current balance; the book changed {growth * 100:+.1f}% over the year.")
    if not pd.isna(u_now):
        L.append(f"- **Macro.** US unemployment went from {pct(u_then, 1)} to {pct(u_now, 1)} over the same 12 months.")
    L.append("")

    # so what
    L.append("## 4 · So what")
    fired = trg[trg["status"].isin(["AMBER", "RED"])]
    for _, t in fired.iterrows():
        f = bp if "chg" in t.metric else (lambda x: pct(x, 1))
        L.append(f"- **{t.trigger_id} {t['name']} — {t.status}** ({f(t.value)}; amber {f(t.amber)}, red {f(t.red)}). "
                 f"*Declared action:* {t.action}")
    watch = lim[lim["status"] != "WITHIN"]
    for _, l in watch.iterrows():
        L.append(f"- **{l.limit_id} {l['name']} — {l.status}** at {pct(l.utilisation, 0)} of limit "
                 f"({pct(l.measured)} vs {pct(l['limit'])}).")
    if fired.empty and watch.empty:
        L.append("- No early-warning trigger or limit is outside its declared band.")
    if up:
        L.append(f"- **Where to look next:** {', '.join(top_states['dim_value'].head(3))} and the "
                 f"{worst.dim_value} vintage carry the largest share of the rise.")
    else:
        pockets = by_state[by_state["contribution"] > 0].head(3)["dim_value"].tolist()
        if pockets:
            L.append(f"- **Where to look next:** the headline improved, but "
                     f"{', '.join(pockets)} moved the other way.")
    L.append(f"- **Controls:** reconciliation to source totals — {rec['pass']} months pass, "
             f"{rec['explained']} explained by documented exceptions, {rec['fail']} fail. "
             f"{'Pack is publishable.' if rec['publishable'] else '**Pack is NOT publishable.**'}")
    L.append("")
    return "\n".join(L)


def write(asof: str, outroot: str = "outputs") -> str:
    md = build(asof)
    outdir = os.path.join(config.REPO_ROOT, outroot, asof)
    os.makedirs(outdir, exist_ok=True)
    path = os.path.join(outdir, "committee_summary.md")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(md)
    return path


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Write the committee one-pager.")
    ap.add_argument("--asof", default=CFG["reporting"]["featured_asof"])
    a = ap.parse_args()
    print(write(a.asof))
