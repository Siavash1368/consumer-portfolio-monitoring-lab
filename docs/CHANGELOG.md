# Change log

Every entry records what changed, why, and what a reviewer should re-run to
confirm it. Dates are the author's local dates.

## 2026-11-22 - v1.1 (config 1.1.0)

**Added**
- `src/cpml/report/summary.py`: committee one-pager (what changed, where, why,
  so what). Every number is computed from the mart and every sentence is a
  template filled from those numbers. Direction-aware: it says "rose" or "fell",
  "new lending" only when a vintage's balance grew in dollars, and states a
  vintage ordering only when the numbers show it.
- `metrics.shift_share`: 12-month attribution of the 90+ rate into within-
  segment change, mix and interaction. The three sum exactly to the headline
  change (tested).
- `src/cpml/pack/controls.py`: reconciliation as a control. PASS / EXPLAINED /
  FAIL, with documented exceptions in the config that excuse only their exact
  difference. A FAIL makes `run_monthly.py` exit non-zero.
- `tests/`: 9 unit tests on a synthetic mart with hand-computable answers, and
  5 integration tests on the real mart. Runs under pytest or the bundled
  standard-library runner. CI runs the unit tests.
- Tableau extracts and `docs/TABLEAU_GUIDE.md`.

**Fixed**
1. *Roll-rate look-ahead.* Transitions were labelled by the from-month, so an
   as-of-June pack reported the June→July move. Now labelled by the month the
   outcome is observed. In the 2009-06 pack, current→30 changes from 0.62% to
   0.66%, 30→60 from 20.8% to 19.0%, cure from 37.5% to 38.8%. The triggers that
   fire are unchanged. In the 2025-09 pack the old method produced a spurious
   9.6% current→30 rate (there is no October 2025 data) and a false red alarm.
   **Re-run:** `test_roll_rate_has_no_look_ahead`, `test_2009_pack_sees_no_later_data`.
2. *Vintage-curve look-ahead.* `vintage_curves` had no as-of filter. Now
   point-in-time, with the denominator at each age restricted to loans old
   enough to have been observed. Curves as of 2025-09 are unchanged.
   **Re-run:** `test_vintage_curve_is_point_in_time`.
3. *DuckDB 2.0 compatibility.* `relation.fetchone()` was removed in DuckDB 2.0;
   scalar reads now use `execute().fetchone()`. Tested on DuckDB 1.5 / pandas 2
   and DuckDB 2.0 / pandas 3.

## 2026-11-17 - initial build

**Scope decisions**

| Decision | Reason |
|---|---|
| Freddie Mac single-family only | No Fannie Mae data exists in the source project. The portfolio line must say Freddie Mac only unless Fannie files are ingested. |
| AWS Athena not implemented | No AWS CLI or credentials available on the build host. A documented-but-unrun query would not meet the "every number comes from code a reviewer can run" rule, so Athena is out of scope rather than faked. |
| Illustrative limits | The risk-appetite limits and early-warning thresholds in `config/monitoring_config.yaml` were set by the author to exercise the machinery. They are not any institution's risk appetite and are labelled as illustrative wherever they appear. |

**Verified source coverage** (queried, not taken from documentation)

- 13,084,969 loans; originations 2003Q1-2009Q4, 28 quarterly vintages
- performance 200212-202509, 933,443,821 monthly records
- the modelling project's derived loan-month panel is 749,070,722 rows over
  10,857,943 loans; this lab reads the raw performance table instead, so the
  pack measures the full servicing book rather than the modelling sample

**Architecture**

The source is a 64 GB DuckDB database on a 3 GB / 2-core host, where a single
unindexed month filter costs ~115 seconds. The build therefore reduces it once
into a monitoring mart and the pack reads only the mart.

Freddie loan sequence numbers carry the vintage quarter as a prefix and the
table was loaded in file order, so a half-open range filter on
`loan_sequence_number` is pruned by DuckDB's zone maps. The build chunks on the
28 vintage quarters; the window pass is sub-chunked two digits deeper so its
sort stays in memory. Every (chunk, pass) is recorded in `build_log`, so an
interrupted build resumes instead of restarting. Full build: 34.5 minutes.

**Two defects found and fixed during the build**

1. *Reporting gaps were being counted as roll-rate transitions.* A plain
   `LEAD()` over a loan's history treats a reappearance after missing months as
   a one-month transition, which manufactures impossible jumps - in one test
   month, 276 current-to-90+ transitions of which only 19 were real. A
   transition is now only counted when the next observation is the next month;
   the rest are labelled `REPORTING_GAP`, excluded from roll-rate denominators,
   and the excluded share is published beside every roll rate. **Re-run:**
   `fact_transition` should contain `REPORTING_GAP` rows, and
   `roll_rate_matrix.csv` should carry a non-null `gap_share`.

2. *One orphaned loan.* The cube joins performance to origination; the
   reconciliation control compares it against an independent query that reads
   the performance table alone. 272 of 274 months tie exactly. The two that do
   not are 2006-08 and 2006-09, where one loan has performance rows and no
   origination record - 2 loan-months and $42,000 out of 933 million rows.
   This is reported, not silently dropped. **Re-run:** the reconciliation
   section of the controls report.

**Measurement conventions adopted**

- Stock measures count only active states. A loan-month carrying a
  zero-balance code is a termination *event*, and its balance is reported as
  zero by construction, so the balance removed is carried separately as
  `upb_removed`.
- Two event definitions are carried end to end on purpose: 90+ days past due
  (the delinquency definition) and a credit-event termination (the loss
  definition). `flow_to_loss_12m` is the bridge between them.
- Rates are reported balance-weighted and count-weighted side by side.
