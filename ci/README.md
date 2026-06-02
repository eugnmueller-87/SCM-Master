# CI workflow

[`ci.yml`](ci.yml) is the GitHub Actions pipeline for this repo: **ruff lint →
migrate-check (autogenerate must be a no-op, so models and migrations can't
drift) → pytest**, run against `backend/`.

It lives here rather than in `.github/workflows/` because adding/updating files
under `.github/workflows/` requires a token with the `workflow` OAuth scope. To
activate it:

```bash
mkdir -p .github/workflows
cp ci/ci.yml .github/workflows/ci.yml
git add .github/workflows/ci.yml && git commit -m "ci: activate workflow" && git push
```

(That push must be made with a credential that has the `workflow` scope — e.g.
`gh auth refresh -s workflow` first.)
