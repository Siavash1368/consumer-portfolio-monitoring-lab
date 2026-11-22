# Building the Tableau Public dashboard

All numbers come from one file the pipeline writes:
`tableau/extracts/dashboard_long.csv` (rebuild with
`python src/cpml/report/tableau_extracts.py`). Every sheet below filters on the
`view` column. Budget about 45 minutes.

**Where to build it.** Either Tableau Public in the browser (sign in at
public.tableau.com → **Create** → **Web Authoring** → upload the CSV), or the free
Tableau Public desktop app (**Connect → Text file**). The steps are the same.

**Before the first sheet:** in the data pane, make sure `month` and `asof` are
**Date** types, and change `mob` to a **Dimension** (right-click → Convert to
Dimension) and then to **Continuous**.

## Sheet 1 — "Delinquency trend"
1. Drag `view` to Filters → keep only **trend**.
2. `month` → Columns. Right-click it → **Month** (the continuous, green one).
3. `value` → Rows. `series` → Color.
4. Right-click the axis → Format → Numbers → Percentage, 1 decimal.
5. Title: *90+ and 30–89 DPD rate, balance-weighted*.

## Sheet 2 — "Roll rates"
1. Filter `view` = **rolls**. Filter `series` to: *Current to 30*, *30 to 60*,
   *Cure 30 to current*, *90+ to credit event*.
2. `month` → Columns (continuous month). `series` → Rows, then `value` → Rows to the right of it.
3. Right-click the value axis → Edit Axis → **Independent axis ranges for each row**
   (the rates live on very different scales).
4. Format as percentage, 1 decimal.

## Sheet 3 — "Vintage curves"
1. Filter `view` = **vintage**.
2. `mob` → Columns (continuous dimension). `value` → Rows. `series` → Color.
3. Format as percentage. Axis title for `mob`: *Months on book*.
4. Title: *Cumulative ever-90+ by vintage (as of Sep-2025)*.

## Sheet 4 — "Concentration"
1. Filter `view` = **concentration**.
2. `series` → Filters, choose **state**, then right-click the filter → Show Filter
   → set it to **Single value (dropdown)** so you can switch to `fico_band`, `ltv_band`, etc.
3. `asof` → Filters → **Exact date** → Show Filter, single value. Pick 2009-06-01.
4. `dim_value` → Rows. `value` → Columns (this is the share of balance).
   Sort descending. For states, add a Top-15 filter on `dim_value` by SUM(value).
5. `value2` → Color (this is the 90+ rate in the segment). Format both as percentages.

## Sheet 5 — "Limits & triggers"
1. Filter `view` to **limits** and **triggers**. Add `asof` as a filter (same as Sheet 4).
2. `view`, `series` and `status` → Rows. `value` and `threshold` → Text
   (drop `value` on Text, then `threshold` on Text, or use Measure Values).
3. `status` → Color, editing the colours: BREACH / RED red, WATCH / AMBER orange,
   WITHIN / GREEN grey.

## Dashboard
1. New Dashboard, Size **Fixed 1200 × 850**.
2. Add a **Text** object at the top:
   *Consumer Portfolio Monitoring Lab — Freddie Mac Single-Family (public data), 2003–2009 vintages.
   Limits and triggers are illustrative.*
3. Tile: Sheet 1 top-left, Sheet 2 top-right, Sheet 3 bottom-left, Sheet 4
   bottom-right, Sheet 5 as a strip along the bottom.
4. On the `asof` filter card: ▼ → **Apply to Worksheets → Selected Worksheets** → tick Sheets 4 and 5.

## Publish
Desktop: **File → Save to Tableau Public**. Web: **File → Publish**. Title it
*Consumer Portfolio Monitoring Lab*. Copy the URL from the published page and
put it in the README's status table, replacing "link pending".
