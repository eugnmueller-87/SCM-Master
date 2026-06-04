# Deploy the live demo (Railway)

Gets SCM-Master running at a public URL for the demo — same idea as TrueSpend's
Live Demo. Uses an in-container SQLite that **reseeds the demo dataset on every
boot** (data resets on redeploy — perfect for a demo, no database to provision).

## Steps

1. **New project → Deploy from GitHub repo** → pick `eugnmueller-87/SCM-Master`.
2. In the service **Settings**:
   - **Root Directory:** `backend`  ← important: the Dockerfile's `COPY` paths are
     relative to `backend/`.
   - **Builder:** Dockerfile (auto-detected from `backend/Dockerfile`).
3. **Variables** (Settings → Variables):
   - `ANTHROPIC_API_KEY` = your key (enables the Agent drawer, chat bubble, and
     AI demand reasoning; the rest of the app works without it).
   - `SECRET_KEY` = any long random string (JWT signing).
   - *(optional)* leave `DATABASE_URL` unset → defaults to in-container SQLite.
   - `PORT` is injected by Railway automatically; the app binds to it.
4. **Deploy.** On boot the container runs: `alembic upgrade head` →
   `python -m app.seed_demo` (seeds the full demo) → `uvicorn`.
5. Railway gives a public URL like `https://scm-master-production.up.railway.app`.
   Open it → log in with **`admin` / `admin`**.

## After deploy

- Health check: `GET /health` and `GET /readyz`.
- The demo logins (`admin`/`buyer`/`warehouse`/`dc`, password = role) all work.
- Send me the public URL and I'll wire it into the profile README as the
  **Live Demo** link (next to the SCM-Master GitHub link).

## Notes

- SQLite resets each redeploy; to persist data instead, add a Railway Postgres
  and set `DATABASE_URL=postgresql+psycopg://…` plus add `psycopg[binary]` to
  requirements.
- The seed is idempotent — if the container restarts without losing the volume,
  it skips reseeding rather than duplicating.
