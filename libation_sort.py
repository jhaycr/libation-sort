#!/usr/bin/env python3
"""Sort Libation-liberated audiobook folders into category directories.

Libation drops DRM-free audiobooks into a staging directory using a folder
template that ends in the Audible ASIN, e.g.::

    Andy Weir - Project Hail Mary [B08G9PRS1K]

This script reads each book's Audible category ladders, contributors, and
series straight from Libation's own SQLite database (LibationContext.db),
classifies the book (fiction / nonfiction / autobiographies / courses / ...),
and moves the folder into the matching category directory of an
Audiobookshelf-style library.

A folder is only moved once Libation marks the book Liberated in its database
AND the folder has been quiet (no file modifications) for a grace period, so
in-progress downloads are never touched. Books the classifier can't resolve
stay in staging and are reported once via an optional Apprise webhook.

Stdlib only; requires Python 3.11+ (tomllib).
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import sqlite3
import sys
import time
import tomllib
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("libation-sort")

ASIN_RE = re.compile(r"\[([A-Za-z0-9]{10})\]$")

# Libation's LiberatedStatus enum: NotLiberated=0, Liberated=1, Error=2, ...
LIBERATED = 1


@dataclass
class Book:
    asin: str
    title: str
    liberated: bool
    ladders: list[list[str]]  # Audible category-id ladders, root id first
    contributors: list[str]
    series: list[str]


@dataclass
class Decision:
    category: str
    flags: list[str] = field(default_factory=list)


@dataclass
class Config:
    staging_dir: Path
    library_dir: Path
    db_path: Path
    rules_path: Path
    state_path: Path
    interval: int
    quiet_seconds: int
    stuck_after_seconds: int
    apprise_url: str
    run_once: bool
    dry_run: bool

    @classmethod
    def from_env(cls) -> "Config":
        env = os.environ.get
        return cls(
            staging_dir=Path(env("STAGING_DIR", "/staging")),
            library_dir=Path(env("LIBRARY_DIR", "/library")),
            db_path=Path(env("DB_PATH", "/libation-db/LibationContext.db")),
            rules_path=Path(env("RULES_PATH", "/config/rules.toml")),
            state_path=Path(env("STATE_PATH", "/config/state.json")),
            interval=int(env("INTERVAL_SECONDS", "900")),
            quiet_seconds=int(env("QUIET_SECONDS", "600")),
            stuck_after_seconds=int(env("STUCK_AFTER_SECONDS", "86400")),
            apprise_url=env("APPRISE_URL", ""),
            run_once=env("RUN_ONCE", "") not in ("", "0", "false"),
            dry_run=env("DRY_RUN", "") not in ("", "0", "false"),
        )


# --------------------------------------------------------------------------
# Classification
# --------------------------------------------------------------------------

def classify(book: Book, rules: dict) -> Decision:
    """Map a book to a category using the precedence rules in rules.toml.

    Order matters: publisher/series beats category ladders (a Great Courses
    history lecture is a course, not nonfiction), and the memoir check beats
    the fiction roots (celebrity memoirs often also carry a Comedy ladder).
    """
    courses = rules.get("courses", {})
    autob = rules.get("autobiographies", {})
    fiction = rules.get("fiction", {})
    fallback = rules.get("fallback", {}).get("category", "nonfiction")

    if set(book.contributors) & set(courses.get("contributors", [])):
        return Decision("courses")
    prefixes = tuple(courses.get("series_prefixes", []))
    if prefixes and any(s.startswith(prefixes) for s in book.series):
        return Decision("courses")

    flags: list[str] = []
    roots = {ladder[0] for ladder in book.ladders if ladder}
    review_roots = set(fiction.get("review_roots", []))
    if roots & review_roots:
        flags.append("review")

    bio_root = autob.get("bio_root")
    memoir_subs = set(autob.get("memoir_subcategories", []))
    for ladder in book.ladders:
        if len(ladder) >= 2 and ladder[0] == bio_root and ladder[1] in memoir_subs:
            return Decision("autobiographies", flags)

    if roots & (set(fiction.get("category_roots", [])) | review_roots):
        return Decision("fiction", flags)

    if roots & set(courses.get("category_roots", [])):
        return Decision("courses", flags)

    return Decision(fallback, flags)


def target_dir(decision: Decision, rules: dict) -> str:
    return rules.get("dirs", {}).get(decision.category, decision.category)


# --------------------------------------------------------------------------
# Libation database access
# --------------------------------------------------------------------------

def open_db_snapshot(db_path: Path) -> sqlite3.Connection:
    """Return an in-memory copy of the DB so Libation never sees our locks."""
    src = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=15)
    try:
        snap = sqlite3.connect(":memory:")
        src.backup(snap)
        return snap
    finally:
        src.close()


def fetch_book(db: sqlite3.Connection, asin: str) -> Book | None:
    row = db.execute(
        "SELECT BookId, Title FROM Books WHERE AudibleProductId = ?", (asin,)
    ).fetchone()
    if row is None:
        return None
    book_id, title = row

    status = db.execute(
        "SELECT BookStatus FROM UserDefinedItem WHERE BookId = ?", (book_id,)
    ).fetchone()

    ladders: dict[int, list[str]] = {}
    for ladder_id, category_id in db.execute(
        """
        SELECT bc.CategoryLadderId, c.AudibleCategoryId
        FROM BookCategory bc
        JOIN CategoryCategoryLadder ccl
          ON ccl._categoryLaddersCategoryLadderId = bc.CategoryLadderId
        JOIN Categories c ON c.CategoryId = ccl._categoriesCategoryId
        WHERE bc.BookId = ?
        ORDER BY bc.CategoryLadderId, ccl.rowid
        """,
        (book_id,),
    ):
        ladders.setdefault(ladder_id, []).append(category_id)

    contributors = [
        name
        for (name,) in db.execute(
            """
            SELECT c.Name FROM BookContributor bc
            JOIN Contributors c ON c.ContributorId = bc.ContributorId
            WHERE bc.BookId = ?
            """,
            (book_id,),
        )
    ]
    series = [
        name
        for (name,) in db.execute(
            """
            SELECT s.Name FROM SeriesBook sb
            JOIN Series s ON s.SeriesId = sb.SeriesId
            WHERE sb.BookId = ?
            """,
            (book_id,),
        )
    ]

    return Book(
        asin=asin,
        title=title,
        liberated=bool(status and status[0] == LIBERATED),
        ladders=list(ladders.values()),
        contributors=contributors,
        series=series,
    )


# --------------------------------------------------------------------------
# Filesystem
# --------------------------------------------------------------------------

def newest_mtime(path: Path) -> float:
    newest = path.stat().st_mtime
    for root, dirs, files in os.walk(path):
        for name in dirs + files:
            try:
                newest = max(newest, (Path(root) / name).stat().st_mtime)
            except OSError:
                pass
    return newest


def is_quiet(path: Path, quiet_seconds: int, now: float) -> bool:
    return now - newest_mtime(path) >= quiet_seconds


# --------------------------------------------------------------------------
# State (used to avoid re-sending the same warnings every cycle)
# --------------------------------------------------------------------------

def load_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def save_state(path: Path, state: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state, indent=2))
    except OSError:
        log.exception("could not write state file %s", path)


# --------------------------------------------------------------------------
# Notifications
# --------------------------------------------------------------------------

def notify(apprise_url: str, title: str, body: str) -> None:
    if not apprise_url:
        return
    data = json.dumps({"title": title, "body": body}).encode()
    req = urllib.request.Request(
        apprise_url, data=data, headers={"Content-Type": "application/json"}
    )
    try:
        urllib.request.urlopen(req, timeout=15)
    except OSError:
        log.exception("apprise notification failed")


# --------------------------------------------------------------------------
# Main pass
# --------------------------------------------------------------------------

def run_once(cfg: Config, rules: dict) -> list[str]:
    """Process the staging directory once; return human-readable event lines."""
    now = time.time()
    state = load_state(cfg.state_path)
    reported: dict[str, str] = state.get("reported", {})
    events: list[str] = []
    present: set[str] = set()

    try:
        db = open_db_snapshot(cfg.db_path)
    except sqlite3.Error:
        log.exception("could not snapshot Libation DB at %s; skipping cycle", cfg.db_path)
        return []

    def stuck(folder: Path, reason: str) -> None:
        """Warn (once per folder+reason) about a folder that isn't moving."""
        present.add(folder.name)
        age = now - folder.stat().st_mtime
        if age < cfg.stuck_after_seconds:
            return
        if reported.get(folder.name) == reason:
            return
        reported[folder.name] = reason
        events.append(f"Stuck in staging: {folder.name} — {reason}")
        log.warning("stuck: %s — %s", folder.name, reason)

    with db:
        for folder in sorted(p for p in cfg.staging_dir.iterdir() if p.is_dir()):
            match = ASIN_RE.search(folder.name)
            if not match:
                stuck(folder, "folder name has no [ASIN] suffix")
                continue
            asin = match.group(1)

            book = fetch_book(db, asin)
            if book is None:
                stuck(folder, "ASIN not found in Libation database")
                continue
            if not book.liberated:
                stuck(folder, "not marked Liberated yet")
                continue
            if not is_quiet(folder, cfg.quiet_seconds, now):
                log.info("waiting (recent writes): %s", folder.name)
                present.add(folder.name)
                continue

            decision = classify(book, rules)
            dest_parent = cfg.library_dir / target_dir(decision, rules)
            dest = dest_parent / folder.name
            if dest.exists():
                stuck(folder, f"destination already exists: {dest}")
                continue

            if cfg.dry_run:
                log.info("[dry-run] %s -> %s", folder.name, dest_parent)
                events.append(f"[dry-run] {folder.name} -> {dest_parent.name}")
                continue

            dest_parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(folder), str(dest))
            line = f"Moved to {dest_parent.name}: {folder.name}"
            if "review" in decision.flags:
                line += "  ⚠ children's/teen category — move to kids/ if it belongs there"
            events.append(line)
            log.info("moved %s -> %s (flags=%s)", folder.name, dest_parent, decision.flags)
            reported.pop(folder.name, None)

    # Forget warnings for folders that are gone (moved or hand-sorted).
    state["reported"] = {k: v for k, v in reported.items() if k in present}
    if not cfg.dry_run:
        save_state(cfg.state_path, state)

    if events:
        notify(cfg.apprise_url, "libation-sort", "\n".join(events))
    return events


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stdout,
    )
    cfg = Config.from_env()
    rules = tomllib.loads(cfg.rules_path.read_text())
    log.info(
        "libation-sort starting: staging=%s library=%s db=%s interval=%ss dry_run=%s",
        cfg.staging_dir, cfg.library_dir, cfg.db_path, cfg.interval, cfg.dry_run,
    )
    while True:
        try:
            run_once(cfg, rules)
        except Exception:
            log.exception("cycle failed")
        if cfg.run_once:
            return 0
        time.sleep(cfg.interval)


if __name__ == "__main__":
    sys.exit(main())
