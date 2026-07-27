# libation-sort

Automatically file [Libation](https://github.com/rmcrackan/Libation)-liberated
audiobooks into category folders (fiction / nonfiction / autobiographies /
courses) for [Audiobookshelf](https://www.audiobookshelf.org/) — using the
Audible category data Libation already stores in its own database.

> **Personal project, no support.** Built largely with AI code-generation tools
> to scratch my own itch. Shared in case it's useful to you too. Issues and
> feature requests are welcome and I read them all, but responses and fixes
> happen on hobby-project time. Review and test before relying on it.

## How it works

Libation liberates books into a **staging** directory using a folder template
ending in the Audible ASIN (e.g. `Andy Weir - Project Hail Mary [B08G9PRS1K]`).
On an interval, libation-sort:

1. Scans staging for book folders and extracts the ASIN from the folder name.
2. Looks the book up in Libation's `LibationContext.db` (a read-only in-memory
   snapshot — it never locks the live database).
3. Skips anything not yet marked **Liberated**, or with file writes in the last
   `QUIET_SECONDS` — in-progress downloads are never touched.
4. Classifies the book from its Audible category ladders, contributors, and
   series (rules in [`rules.toml`](rules.toml)):
   - **courses** — anything from The Great Courses (publisher or series), plus
     language-learning titles (Pimsleur etc.)
   - **autobiographies** — Biographies & Memoirs ladders with a memoir-type
     sub-category (biographies *of others* fall through to nonfiction)
   - **fiction** — fiction-dominant category roots; children's/teen titles land
     here too but are flagged in the notification as candidates for a manually
     curated `kids/` folder
   - **nonfiction** — everything else
5. Moves the folder into `LIBRARY_DIR/<category>/` and (optionally) posts a
   summary to an [Apprise](https://github.com/caronc/apprise-api) endpoint.

Folders it can't resolve (ASIN missing from the DB, destination collision)
stay in staging and are reported once, not every cycle.

Validated against a 338-book library that had been hand-sorted into these
categories: 94% agreement overall, 96% excluding the hand-curated kids shelf.

## Running

Stdlib-only Python 3.11+. Run the script directly, or use the container image:

```yaml
services:
  libation-sort:
    image: ghcr.io/jhaycr/libation-sort:v0.1.0
    user: 1000:1000
    volumes:
      - /path/to/media/audio:/audio
      - /path/to/appdata/libation/db:/libation-db:ro
      - /path/to/appdata/libation-sort:/config
    environment:
      STAGING_DIR: /audio/libation-staging
      LIBRARY_DIR: /audio/audiobooks
      APPRISE_URL: http://apprise:8000/notify/libation   # optional
    restart: unless-stopped
```

Put `rules.toml` in the `/config` volume (start from the one in this repo and
tune the category IDs to taste).

## Configuration (environment)

| Variable | Default | Purpose |
|---|---|---|
| `STAGING_DIR` | `/staging` | Where Libation liberates books to |
| `LIBRARY_DIR` | `/library` | Root containing the category folders |
| `DB_PATH` | `/libation-db/LibationContext.db` | Libation's SQLite database |
| `RULES_PATH` | `/config/rules.toml` | Classification rules |
| `STATE_PATH` | `/config/state.json` | Remembers already-sent warnings |
| `INTERVAL_SECONDS` | `900` | Scan interval |
| `QUIET_SECONDS` | `600` | Folder must be write-quiet this long before moving |
| `STUCK_AFTER_SECONDS` | `86400` | Age before an unresolvable folder is reported |
| `APPRISE_URL` | *(empty = off)* | Apprise API endpoint to POST summaries to |
| `RUN_ONCE` | *(empty)* | Set to `1` to do one pass and exit |
| `DRY_RUN` | *(empty)* | Set to `1` to log/report moves without doing them |
| `LOG_LEVEL` | `INFO` | Python log level |

## Finding your category IDs

Audible category names aren't stored in Libation's DB, only their IDs. To see
which IDs your own sorted library implies, run the classifier in `DRY_RUN=1
RUN_ONCE=1` mode and check the logs — or query the DB directly:

```sql
SELECT b.Title, group_concat(c.AudibleCategoryId, ' > ')
FROM Books b
JOIN BookCategory bc ON bc.BookId = b.BookId
JOIN CategoryCategoryLadder ccl ON ccl._categoryLaddersCategoryLadderId = bc.CategoryLadderId
JOIN Categories c ON c.CategoryId = ccl._categoriesCategoryId
GROUP BY b.BookId, bc.CategoryLadderId;
```

## License

[MIT](LICENSE)
