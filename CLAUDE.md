# CLAUDE.md

Guidance for Claude Code when working in this repository.

## What this is

A sidecar for Libation that files liberated audiobooks into category folders
for Audiobookshelf. One stdlib-only Python file (`libation_sort.py`), rules in
`rules.toml`, tests in `tests/`. Ships as a container image via GHCR. See
README.md for user-facing docs.

## Status (July 2026)

**Live in production** on Josh's NAS (`neo`) as of 2026-07-26.

- v0.1.0 — initial release
- v0.1.1 — DB snapshot via scratch-dir copy (Libation's DB is WAL-mode; WAL
  can't be opened from a read-only mount — found live, fixed same day)
- v0.2.0 — classification generalized into an ordered, first-match-wins rule
  list with composable matchers; deployed and verified live

The end-to-end pipeline: Audible purchase → Libation container on neo
(hourly `scan && liberate` → `/mnt/storage/media/audio/libation-staging`) →
this sorter (15-min cycle) → `audiobooks/{fiction,nonfiction,autobiographies,
courses}/` → Audiobookshelf scan. `kids/` is hand-curated; children's/teen
titles are flagged `review` and filed to fiction.

## Key design decisions

- **Classification source**: Audible category-ladder IDs read from Libation's
  own `LibationContext.db` — no external API calls. Category *names* are not
  in the DB, only IDs; the shipped `rules.toml` IDs were derived empirically.
- **Validation methodology**: Josh's 338-book hand-sorted library served as
  labeled data. The shipped rules score 319/338 (94%; 96% excluding the
  hand-curated kids shelf), and 6/7 kids books carry the review flag. If you
  change rules or the classifier, re-validate against a sorted library rather
  than reasoning from category names.
- **Safety invariants** (preserve these): never open the live DB (snapshot a
  scratch copy — see WAL note above); never move a folder unless the DB marks
  it Liberated AND it's been write-quiet for `QUIET_SECONDS`; never overwrite
  an existing destination; report unresolvable folders once, not per cycle.
- **Slimness is a feature**: image is `python:3.13-alpine` + one file
  (45.5 MB total, ~20 MiB resident). No dependencies beyond stdlib (Python
  3.11+ for `tomllib`). Don't add packages without strong cause; a Go/Rust
  rewrite was considered and rejected as complexity-for-megabytes.

## Deployment (lives in nas-infra, not here)

Deployed from `~/Code/ansible/nas-infra`:

- `docker/neo/media-server/libation-sort/docker-compose.yml.j2` — service,
  pinned to a version tag (Renovate bumps it)
- `docker/neo/media-server/libation-sort/appdata/libation-sort/rules.toml.j2`
  — **a copy of this repo's `rules.toml`**; keep them in sync when rules change
- `docker/neo/media-server/libation/appdata/libation/Settings.json.j2` —
  Libation's config is IaC-owned (folder template must keep ending in `[<id>]`;
  `DownloadEpisodes: false` keeps podcasts out of the pipeline)
- `docs/libation-pipeline.md` — migration runbook (completed) and day-2 ops

Deploy: `make neo-docker media-server` from nas-infra.

## Release flow

1. Commit to `main` (Josh's standing rule: all work lands on main, pushed).
2. Tag `vX.Y.Z` and push the tag; CI tests then publishes
   `ghcr.io/jhaycr/libation-sort:vX.Y.Z` (public package).
3. Bump the image pin in nas-infra and redeploy.

Watch for: chained shell commands that pipe pytest into `tail` mask its exit
code — a broken-test commit got tagged once this way. Run tests standalone
before tagging.

## Open items

- Apprise notifications are wired but off (no `libation` key configured in
  the apprise container; `APPRISE_URL` commented out in the compose template).
- The laptop Libation install is retired as a liberation source; its local
  `Books/` dir can be deleted once Josh trusts the flow.
