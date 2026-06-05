# Power BI analytics — forecast accuracy & demand history

How to pull SCM-Master data into Power BI and build a **forecast-accuracy
dashboard** — the agent's demand forecast scored against what actually happened,
over ~18 months of seeded history.

## What the data is

The demo seeds **18 months of dated deployments** (`app.seed_history`), built
through the real services so each unit carries a true `deployed_date`. Because
the demand forecast can run "as of" any past date, we **backtest** it: stand at a
past month-end, ask what the agent forecast for the next 90 days, then compare to
the deployments that actually happened. That produces a per-product, per-period
accuracy series.

Headline on the seeded data: **MAPE ≈ 15%** with a small positive bias (the agent
forecasts slightly conservative — the safe direction for procurement).

## The three feeds (flat CSV, Web connector)

All under `/api/v1/analytics/exports/` — flat CSV, one row per fact, stable
columns. Any authenticated user may pull them.

| Endpoint | One row per | Key columns |
|---|---|---|
| `forecast-accuracy.csv` | as-of date × product | `as_of_date`, `product_code`, `predicted_demand`, `actual_demand`, `abs_error`, `ape` |
| `demand-history.csv` | month × product | `month`, `month_start`, `product_code`, `units_deployed` |
| `spend.csv` | supplier | `supplier_name`, `units`, `spend` |

## Connect Power BI (live demo, no DB credentials)

The live demo needs a bearer token (the API is authenticated). Two ways:

### Option A — Web connector with an auth header

1. Get a token:
   ```
   POST https://scm-master-production.up.railway.app/api/v1/auth/login
   form-encoded: username=admin@example.com & password=admin
   -> { "access_token": "<JWT>", ... }
   ```
2. In Power BI Desktop: **Get Data → Web → Advanced**.
   - URL: `https://scm-master-production.up.railway.app/api/v1/analytics/exports/forecast-accuracy.csv`
   - HTTP request header parameters: `Authorization` = `Bearer <JWT>`
3. Power BI parses the CSV into a table. Repeat for `demand-history.csv` and
   `spend.csv`. Set a scheduled refresh if you want it to re-pull.

> JWTs expire (8h by default). For an always-on dashboard, either lengthen
> `ACCESS_TOKEN_EXPIRE_MINUTES`, or use the production DirectQuery path below.

### Option B — local file (quickest for a one-off dashboard)

```powershell
# with a token in $T
curl -H "Authorization: Bearer $T" `
  https://scm-master-production.up.railway.app/api/v1/analytics/exports/forecast-accuracy.csv `
  -o forecast-accuracy.csv
```
Then **Get Data → Text/CSV** on the saved file.

## Suggested dashboard

- **Forecast accuracy over time** — line chart, `as_of_date` on X, two lines
  `predicted_demand` vs `actual_demand`. Slicer by `product_code`.
- **MAPE by product** — bar chart, `product_code` vs `AVERAGE(ape)`.
- **Bias card** — `AVERAGE(predicted_demand - actual_demand)` (>0 = over-forecast).
- **Demand history** — area chart from `demand-history.csv`, `month_start` × `units_deployed`, stacked by `category`.
- **Spend** — bar from `spend.csv`, `supplier_name` × `spend`.

Useful DAX:
```
MAPE = AVERAGEX(FILTER('forecast-accuracy', 'forecast-accuracy'[actual_demand] > 0), 'forecast-accuracy'[ape])
Bias = AVERAGE('forecast-accuracy'[predicted_demand]) - AVERAGE('forecast-accuracy'[actual_demand])
```

## Production path (DirectQuery)

For a real install, skip the CSV/token dance and connect Power BI straight to
Postgres: deploy with `DATABASE_URL=postgresql+psycopg://…`, expose the same
three facts as `fact_*` views, and use **Get Data → PostgreSQL → DirectQuery**.
The export endpoints and the views return the identical columns, so a dashboard
built on the CSVs re-points to the views unchanged.

## Refresh the underlying data

The history is seeded at container boot (idempotent). Locally:
```powershell
.venv\Scripts\python -m app.seed_demo
.venv\Scripts\python -m app.seed_history
```
