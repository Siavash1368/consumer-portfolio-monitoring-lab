# Consumer Portfolio Monitoring Lab

A monthly credit-portfolio monitoring pipeline over the **Freddie Mac
Single-Family Loan-Level Dataset**: 13,084,969 loans originated 2003Q1–2009Q4,
observed monthly from December 2002 through September 2025 (933,443,821
loan-month records).

One command produces the whole monitoring pack for a reporting month:

```bash
python src/cpml/run_monthly.py --asof 200906
```

> **Scope, stated plainly.** Public agency mortgage data only — one first-lien
> mortgage book, not a bank's portfolio. The risk-appetite limits and
> early-warning thresholds are **illustrative**, set by the author to exercise
> the monitoring machinery. Nothing here is, or imitates, a regulatory
> submission. The raw Freddie Mac data is licensed and is not redistributed;
> the two committed sample packs in `outputs/` are portfolio-level aggregates.

---

## Status

| Component | Status |
|---|---|
| Monitoring mart (933M rows → small aggregate cubes), resumable build | **Built** |
| Roll rates, cure rates, flow-to-loss | **Built** |
| Vintage curves (cumulative ever-90+ and credit events by months on book) | **Built** |
| Concentration by state, FICO, LTV, purpose, occupancy, property type, channel, term, vintage | **Built** |
| Illustrative risk-appetite limits with utilisation | **Built** |
| Early-warning triggers (amber/red, declared actions) and breach log | **Built** |
| Denominator-effect check (current vs lagged balance) | **Built** |
| Reconciliation of mart to independent source control totals | **Built** (report formatting in progress) |
| Model-performance monitoring (PSI / AUROC / calibration escalation) | In progress |
| CECL and PD trends, forecast vs actual | In progress |
| Y-14M-style first-lien schedule, data-quality report, procedures | In progress |
| Tableau dashboards, Excel refresh, test suite | In progress |

The models this lab will monitor (hazard PD, LGD, CECL lifetime loss, DFAST
stress, walk-forward backtests) live in the companion repository
[mortgage-credit-risk-portfolio](https://github.com/Siavash1368/mortgage-credit-risk-portfolio).

---

## Architecture

```
 Freddie Mac source (64 GB DuckDB, read-only)
        │   933,443,821 loan-months
        ▼
 build_mart.py  ── chunked on 28 vintage quarters, resumable via build_log
        │
        ▼
 monitoring mart (DuckDB)
   fact_cube            167,334 rows   month × state × one dimension
   fact_transition        8,712 rows   month × from-state × to-state
   dim_loan          13,084,968 rows   one row per loan, first-event ages
   fact_control_totals      274 rows   independent source totals
        │
        ▼
 run_monthly.py --asof YYYYMM  ── seconds, not minutes
        │
        ▼
 outputs/<asof>/  CSV pack + manifest.json
```

**Why a mart.** On the build host (3 GB RAM, 2 cores) a single unindexed month
filter over the source takes about 115 seconds. Freddie loan sequence numbers
carry the vintage quarter as a prefix and the table is stored in file order, so
a range filter on `loan_sequence_number` is pruned by DuckDB's zone maps
(68 million rows in 7.7 seconds). The build chunks on that prefix; the
window-function pass is split finer so its sort never spills to disk. The
full build takes 34.5 minutes and resumes where it stopped if interrupted.

**Every threshold is declared before the numbers exist.** Limits, triggers,
bands and tolerances live in `config/monitoring_config.yaml`; no number is
hard-coded. `config_version` is stamped onto every pack's manifest.

---

## Two defects the controls found

Recorded in full, with re-run instructions, in [`docs/CHANGELOG.md`](docs/CHANGELOG.md).

1. **Reporting gaps were inflating roll rates.** A plain `LEAD()` over a loan's
   history treats a reappearance after missing months as a one-month
   transition. In one test month that produced 276 current-to-90+ jumps, of
   which 19 were real. A transition now counts only when the next observation
   is the next month; the rest are labelled `REPORTING_GAP`, excluded from the
   denominator, and the excluded share is published beside every roll rate.

2. **An orphaned loan.** The mart joins performance to origination; the
   reconciliation compares it to a separate query that reads performance
   alone. 272 of 274 months tie exactly. The two that don't (2006-08, 2006-09)
   contain one loan with performance records and no origination record —
   2 loan-months, $42,000.

---

## Selected results

| As of | Finding |
|---|---|
| 2009-06 | 8.08M active loans, $1,252.4bn; 90+ rate 2.12% (balance-weighted). Triggers fired: 90+ 3-month change (amber), 30-DPD cure rate (amber), 12-month flow to loss 47.2% (red) |
| 2009-06 | Florida 90+ 6.37% vs California 2.65% vs Texas 0.98% |
| 2009-06 | 90+ by FICO band: 8.89% below 620 to 0.31% at 780+ |
| all | Cumulative ever-90+ at 60 months on book: 2003 vintage 1.42%, 2007 13.50%, 2009 1.82% |
| 2012-06 | **Denominator effect:** 90+ rate 5.27% on current balance vs 3.78% on balance 12 months earlier, while the book ran off 28.3% |

---

## Reproduce

```bash
pip install -r requirements.txt
# point config/monitoring_config.yaml -> source.freddie_db at your copy of the
# Freddie Mac loan-level database (origination + performance tables)
python src/cpml/mart/build_mart.py --time-budget 165   # repeat until 0 items remain
python src/cpml/mart/build_mart.py --finalize
python src/cpml/run_monthly.py --asof 200906
```

## Layout

```
config/monitoring_config.yaml   every limit, trigger, band and tolerance
src/cpml/mart/build_mart.py     source → mart, chunked and resumable
src/cpml/pack/metrics.py        stock, roll rates, vintages, concentration, denominator
src/cpml/pack/limits.py         limit utilisation, triggers, breach log
src/cpml/run_monthly.py         the monthly pack, one parameter
docs/CHANGELOG.md               decisions, verified coverage, defects found
outputs/200906, outputs/202509  committed sample packs (aggregates only)
```
