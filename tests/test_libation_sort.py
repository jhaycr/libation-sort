import json
import os
import sqlite3
import time
import tomllib
from pathlib import Path

import pytest

import libation_sort as ls

RULES = tomllib.loads((Path(__file__).parent.parent / "rules.toml").read_text())


def book(ladders=(), contributors=(), series=(), liberated=True, asin="B000000000"):
    return ls.Book(
        asin=asin,
        title="Test Book",
        liberated=liberated,
        ladders=[list(l) for l in ladders],
        contributors=list(contributors),
        series=list(series),
    )


class TestClassify:
    def test_great_courses_contributor_wins_over_everything(self):
        b = book(ladders=[["18573370011"]], contributors=["The Great Courses"])
        assert ls.classify(b, RULES).category == "courses"

    def test_great_courses_series_prefix(self):
        b = book(series=["The Great Courses: Ancient History"])
        assert ls.classify(b, RULES).category == "courses"

    def test_language_learning_root_is_courses(self):
        b = book(ladders=[["18573267011", "999"]])
        assert ls.classify(b, RULES).category == "courses"

    def test_memoir_sub_is_autobiography(self):
        b = book(ladders=[["18571951011", "18571984011"]])
        assert ls.classify(b, RULES).category == "autobiographies"

    def test_bio_of_someone_else_falls_back_to_nonfiction(self):
        b = book(ladders=[["18571951011", "18571990011"]])
        assert ls.classify(b, RULES).category == "nonfiction"

    def test_memoir_beats_fiction_roots(self):
        # celebrity memoirs often also carry a comedy/fiction ladder
        b = book(ladders=[["24427740011"], ["18571951011", "18572028011"]])
        assert ls.classify(b, RULES).category == "autobiographies"

    def test_fiction_root(self):
        b = book(ladders=[["18580606011", "123"]])
        assert ls.classify(b, RULES).category == "fiction"

    def test_childrens_root_is_fiction_with_review_flag(self):
        d = ls.classify(book(ladders=[["18572091011"]]), RULES)
        assert d.category == "fiction"
        assert "review" in d.flags

    def test_default_is_nonfiction(self):
        b = book(ladders=[["18573370011", "18573475011"]])
        assert ls.classify(b, RULES).category == "nonfiction"

    def test_no_ladders_at_all(self):
        assert ls.classify(book(), RULES).category == "nonfiction"


# ---------------------------------------------------------------------------
# End-to-end: synthetic Libation DB + staging tree
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE Books (BookId INTEGER PRIMARY KEY, AudibleProductId TEXT, Title TEXT);
CREATE TABLE UserDefinedItem (BookId INTEGER, BookStatus INTEGER);
CREATE TABLE BookCategory (BookId INTEGER, CategoryLadderId INTEGER);
CREATE TABLE CategoryCategoryLadder (
    _categoriesCategoryId INTEGER, _categoryLaddersCategoryLadderId INTEGER);
CREATE TABLE Categories (CategoryId INTEGER PRIMARY KEY, AudibleCategoryId TEXT);
CREATE TABLE BookContributor (BookId INTEGER, ContributorId INTEGER, Role INTEGER, "Order" INTEGER);
CREATE TABLE Contributors (ContributorId INTEGER PRIMARY KEY, Name TEXT);
CREATE TABLE SeriesBook (SeriesId INTEGER, BookId INTEGER);
CREATE TABLE Series (SeriesId INTEGER PRIMARY KEY, Name TEXT);
"""


def make_db(path: Path, asin: str, ladder: list[str], liberated: bool = True):
    db = sqlite3.connect(path)
    db.executescript(SCHEMA)
    db.execute("INSERT INTO Books VALUES (1, ?, 'A Book')", (asin,))
    db.execute("INSERT INTO UserDefinedItem VALUES (1, ?)", (1 if liberated else 0,))
    db.execute("INSERT INTO BookCategory VALUES (1, 10)")
    for i, cat_id in enumerate(ladder, start=1):
        db.execute("INSERT INTO Categories VALUES (?, ?)", (i, cat_id))
        db.execute("INSERT INTO CategoryCategoryLadder VALUES (?, 10)", (i,))
    db.commit()
    db.close()


def make_config(tmp_path: Path, **overrides) -> ls.Config:
    staging = tmp_path / "staging"
    library = tmp_path / "library"
    staging.mkdir(exist_ok=True)
    library.mkdir(exist_ok=True)
    defaults = dict(
        staging_dir=staging,
        library_dir=library,
        db_path=tmp_path / "LibationContext.db",
        rules_path=Path(__file__).parent.parent / "rules.toml",
        state_path=tmp_path / "state.json",
        interval=1,
        quiet_seconds=0,
        stuck_after_seconds=0,
        apprise_url="",
        run_once=True,
        dry_run=False,
    )
    defaults.update(overrides)
    return ls.Config(**defaults)


def test_snapshot_wal_db_from_readonly_dir(tmp_path):
    # Libation keeps its DB in WAL mode and we mount it read-only: the
    # snapshot must work with an unwritable directory and a live -wal file.
    dbdir = tmp_path / "db"
    dbdir.mkdir()
    dbfile = dbdir / "LibationContext.db"
    make_db(dbfile, "B08G9PRS1K", ["18580606011"])
    writer = sqlite3.connect(dbfile)  # stays open, like the live Libation app
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("INSERT INTO Books VALUES (2, 'B000000002', 'Wal Book')")
    writer.commit()
    assert dbfile.with_name(dbfile.name + "-wal").exists()
    dbdir.chmod(0o555)
    try:
        snap = ls.open_db_snapshot(dbfile)
        assert snap.execute("SELECT count(*) FROM Books").fetchone()[0] == 2
        snap.close()
    finally:
        dbdir.chmod(0o755)
        writer.close()


def test_moves_liberated_fiction_book(tmp_path):
    asin = "B08G9PRS1K"
    make_db(tmp_path / "LibationContext.db", asin, ["18580606011", "123"])
    folder = tmp_path / "staging" / f"Andy Weir - Project Hail Mary [{asin}]"
    cfg = make_config(tmp_path)
    folder.mkdir(parents=True)
    (folder / "book.m4b").write_bytes(b"audio")

    events = ls.run_once(cfg, RULES)

    assert not folder.exists()
    dest = tmp_path / "library" / "fiction" / folder.name
    assert (dest / "book.m4b").exists()
    assert any("fiction" in e for e in events)


def test_leaves_unliberated_book_alone(tmp_path):
    asin = "B08G9PRS1K"
    make_db(tmp_path / "LibationContext.db", asin, ["18580606011"], liberated=False)
    cfg = make_config(tmp_path)
    folder = tmp_path / "staging" / f"Book [{asin}]"
    folder.mkdir(parents=True)

    ls.run_once(cfg, RULES)

    assert folder.exists()


def test_respects_quiet_period(tmp_path):
    asin = "B08G9PRS1K"
    make_db(tmp_path / "LibationContext.db", asin, ["18580606011"])
    cfg = make_config(tmp_path, quiet_seconds=3600)
    folder = tmp_path / "staging" / f"Book [{asin}]"
    folder.mkdir(parents=True)
    (folder / "part.m4b").write_bytes(b"x")  # just written -> not quiet

    ls.run_once(cfg, RULES)

    assert folder.exists()


def test_dry_run_moves_nothing(tmp_path):
    asin = "B08G9PRS1K"
    make_db(tmp_path / "LibationContext.db", asin, ["18580606011"])
    cfg = make_config(tmp_path, dry_run=True)
    folder = tmp_path / "staging" / f"Book [{asin}]"
    folder.mkdir(parents=True)

    events = ls.run_once(cfg, RULES)

    assert folder.exists()
    assert any("dry-run" in e for e in events)


def test_unknown_asin_reported_once(tmp_path):
    make_db(tmp_path / "LibationContext.db", "B000000001", ["18580606011"])
    cfg = make_config(tmp_path)
    folder = tmp_path / "staging" / "Mystery Book [B099999999]"
    folder.mkdir(parents=True)
    os.utime(folder, (time.time() - 90000, time.time() - 90000))

    first = ls.run_once(cfg, RULES)
    second = ls.run_once(cfg, RULES)

    assert folder.exists()
    assert any("not found" in e for e in first)
    assert not second  # warned once, not again


def test_existing_destination_is_not_overwritten(tmp_path):
    asin = "B08G9PRS1K"
    make_db(tmp_path / "LibationContext.db", asin, ["18580606011"])
    cfg = make_config(tmp_path)
    name = f"Book [{asin}]"
    folder = tmp_path / "staging" / name
    folder.mkdir(parents=True)
    os.utime(folder, (time.time() - 90000, time.time() - 90000))
    dest = tmp_path / "library" / "fiction" / name
    dest.mkdir(parents=True)
    (dest / "keep.m4b").write_bytes(b"original")

    events = ls.run_once(cfg, RULES)

    assert folder.exists()
    assert (dest / "keep.m4b").read_bytes() == b"original"
    assert any("already exists" in e for e in events)
