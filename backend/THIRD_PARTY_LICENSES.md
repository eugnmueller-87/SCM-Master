# Third-party licenses

SCM-Master depends on the following third-party packages. All are permissive
(MIT / BSD / Apache-2.0) — none are copyleft, so they impose no obligation to
open-source this product. The only obligation is this attribution notice.

## Direct dependencies

| Package | License | Use |
|---|---|---|
| FastAPI | MIT | web framework |
| Starlette | BSD-3-Clause | ASGI toolkit (via FastAPI) |
| Uvicorn | BSD-3-Clause | ASGI server |
| SQLAlchemy | MIT | ORM |
| Alembic | MIT | DB migrations |
| Pydantic / pydantic-settings | MIT | validation / settings |
| httpx | BSD-3-Clause | HTTP client |
| PyJWT | MIT | auth tokens |
| bcrypt | Apache-2.0 | password hashing |
| python-multipart | Apache-2.0 | form parsing |
| anthropic | MIT | LLM client (advisory/narration only) |
| psycopg | LGPL-3.0* | Postgres driver |
| **statsforecast** | **Apache-2.0** | intermittent-demand forecasting engine |

\* psycopg core is LGPL; we use it as an unmodified, dynamically-linked library
(the `psycopg[binary]` wheel), which does not impose copyleft on our code.

## statsforecast (Nixtla) — Apache-2.0

Used only via [`app/services/forecasting_sf.py`](app/services/forecasting_sf.py),
gated behind `settings.forecast_engine` (default `"builtin"`). It is a CPU-only
statistical library (Croston/SBA/TSB/ADIDA/IMAPA) — **no LLM, no tokens, no
network calls**. Source & license:
<https://github.com/Nixtla/statsforecast> (Apache License 2.0).

Its transitive dependencies (numpy, pandas, scipy, numba, llvmlite, pyarrow,
statsmodels) are all BSD/Apache-2.0/MIT-licensed.

---

Per the Apache-2.0 terms, this notice reproduces the requirement to retain
attribution. No source modifications were made to statsforecast; it is used as a
packaged dependency.
