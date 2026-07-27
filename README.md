# libation-sort

Automatically file [Libation](https://github.com/rmcrackan/Libation)-liberated
audiobooks into your library's category folders — fiction, nonfiction,
courses, whatever your layout is — using the Audible category data Libation
already stores in its own database. Built as a sidecar for
[Audiobookshelf](https://www.audiobookshelf.org/) libraries, but it just
moves folders: any scanner that watches directories will work.

> **Personal project, no support.** Built largely with AI code-generation tools
> to scratch my own itch. Shared in case it's useful to you too. Issues and
> feature requests are welcome and I read them all, but responses and fixes
> happen on hobby-project time. Review and test before relying on it.

## The problem

Libation happily liberates your whole Audible library into one flat directory.
If your audiobook library is organized into category folders, every new
purchase means a manual move. libation-sort closes that gap: point Libation at
a staging directory, and each finished book gets classified and filed into the
right category folder automatically — new purchases flow from Audible to your
organized library with no hands on keyboard.

## How it works

Libation's folder template must end with the Audible ASIN (e.g.
`Andy Weir - Project Hail Mary [B08G9PRS1K]`). On an interval, libation-sort:

1. Scans the staging directory for book folders and extracts each ASIN.
2. Looks the book up in Libation's `LibationContext.db` via a point-in-time
   copy — it never opens the live database, so Libation is never blocked.
3. Skips anything not yet marked **Liberated**, or with file writes in the
   last `QUIET_SECONDS` — in-progress downloads are never touched.
4. Classifies the book from its Audible category ladders, contributors, and
   series using the ordered rules in [`rules.toml`](rules.toml).
5. Moves the folder into `LIBRARY_DIR/<target>/` and (optionally) posts a
   summary to an [Apprise](https://github.com/caronc/apprise-api) endpoint.

Folders it can't resolve (ASIN missing from the DB, destination already
exists) stay in staging and are reported once — not every cycle. Existing
library folders are never overwritten.

Validated against a 338-book library that had been hand-sorted for years:
94% agreement overall, 96% excluding a hand-curated kids shelf.

## Quick start

Stdlib-only Python 3.11+ — run `libation_sort.py` directly, or use the
container image:

```yaml
services:
  libation-sort:
    image: ghcr.io/jhaycr/libation-sort:v0.2.0
    user: 1000:1000
    volumes:
      - /path/to/media/audio:/audio                     # staging + library
      - /path/to/appdata/libation/db:/libation-db:ro    # Libation's DB
      - /path/to/appdata/libation-sort:/config          # rules.toml + state
    environment:
      STAGING_DIR: /audio/libation-staging
      LIBRARY_DIR: /audio/audiobooks
      APPRISE_URL: http://apprise:8000/notify/libation  # optional
    restart: unless-stopped
```

Setup checklist:

- Point Libation's *Books* directory at the staging dir — **outside** the tree
  your library scanner watches, so half-downloaded books never surface.
- Keep Libation's folder template ending in ` [<id>]` (the default does).
- Mount staging and library under a single volume so moves are cheap renames.
- Copy [`rules.toml`](rules.toml) into the `/config` volume and tune it.
- Try it with `DRY_RUN=1` first: it logs what it *would* move, touches nothing.

## Configuration

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

## Rules

Rules are evaluated top to bottom; the first matching rule **with a `target`**
wins and the book moves to `LIBRARY_DIR/<target>/`. `fallback` catches
everything else. Directory names come straight from `target`/`fallback` — your
categories, your names. Rules are validated at startup with pinpointed errors.

Matchers (a rule matches if *any* of its matchers hit):

| Matcher | Matches when... |
|---|---|
| `match.contributors` | any contributor (author/narrator/publisher) is an exact match |
| `match.series_prefixes` | any series name starts with one of these |
| `match.category_roots` | any Audible category ladder starts at one of these ids |
| `match.ladder_prefixes` | a ladder starts with this exact id sequence (root → sub) |

A rule with `flags` but **no** `target` is annotation-only: it adds its flags
and evaluation continues. Flags ride along to whichever rule assigns the
target and show up in logs/notifications, with human-readable text from
`[flag_messages]`.

```toml
fallback = "nonfiction"

[flag_messages]
review = "children's/teen category — move to kids/ if it belongs there"

# Publisher beats category ladders: a Great Courses history lecture is a
# course, not nonfiction.
[[rule]]
name = "the-great-courses"
target = "courses"
match.contributors = ["The Great Courses"]
match.series_prefixes = ["The Great Courses"]

# Annotation-only: flag likely kids' books for manual review, keep going.
[[rule]]
name = "maybe-kids"
flags = ["review"]
match.category_roots = ["18572091011", "18580715011"]

# Memoirs are autobiographies; biographies OF others fall through.
[[rule]]
name = "memoirs"
target = "autobiographies"
match.ladder_prefixes = [["18571951011", "18571984011"]]

[[rule]]
name = "fiction"
target = "fiction"
match.category_roots = ["18580606011", "18574426011"]
```

The shipped [`rules.toml`](rules.toml) implements a
fiction / nonfiction / autobiographies / courses split with a review flag for
children's/teen titles — start from it and rename, reorder, or replace rules
to match your own layout.

### Finding your category IDs

Audible category *names* aren't stored in Libation's DB, only their ids. To
see which ids your library uses:

```sql
SELECT b.Title, group_concat(c.AudibleCategoryId, ' > ')
FROM Books b
JOIN BookCategory bc ON bc.BookId = b.BookId
JOIN CategoryCategoryLadder ccl ON ccl._categoryLaddersCategoryLadderId = bc.CategoryLadderId
JOIN Categories c ON c.CategoryId = ccl._categoriesCategoryId
GROUP BY b.BookId, bc.CategoryLadderId;
```

A practical workflow: if part of your library is already hand-sorted, run that
query, cross-reference a few known books per category, and build rules from
the ids that dominate each shelf. Then iterate with `DRY_RUN=1`.

## Development

```bash
python -m pytest   # no dependencies beyond pytest
```

One file (`libation_sort.py`), stdlib only. CI runs the tests and publishes
the container image to GHCR on pushes to `main` and version tags.

## License

[MIT](LICENSE)
