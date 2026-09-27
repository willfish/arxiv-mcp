"""Disk-backed arXiv full-text library. No corpus-sized in-memory state."""

import hashlib
from pathlib import Path
import sqlite3
import zlib

SCHEMA = """
CREATE TABLE IF NOT EXISTS papers (
 id INTEGER PRIMARY KEY, paper_id TEXT NOT NULL UNIQUE,
 title TEXT NOT NULL, abstract TEXT NOT NULL, category TEXT NOT NULL,
 license TEXT, sha256 TEXT NOT NULL, text_bytes INTEGER NOT NULL,
 text_chars INTEGER NOT NULL, body BLOB NOT NULL
);
CREATE VIRTUAL TABLE IF NOT EXISTS search USING fts5(
 title, abstract, body, content='', tokenize='porter unicode61'
);
CREATE TABLE IF NOT EXISTS imports (
 shard TEXT PRIMARY KEY, rows_done INTEGER NOT NULL,
 complete INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def connect(path, write=False):
    path = Path(path)
    if write:
        path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(path, timeout=30)
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=FULL")
        db.execute("PRAGMA cache_size=-65536")
        db.executescript(SCHEMA)
    else:
        db = sqlite3.connect(
            path.resolve().as_uri() + "?mode=ro", uri=True, timeout=30
        )
        db.execute("PRAGMA query_only=ON")
    db.row_factory = sqlite3.Row
    return db


def add_paper(db, row):
    text = row["text"]
    raw = text.encode("utf-8")
    sha = hashlib.sha256(raw).hexdigest()
    if row.get("text_sha256") and sha != row["text_sha256"]:
        raise ValueError(f'Text checksum mismatch: {row["paper_id"]}')
    prior = db.execute(
        "SELECT sha256 FROM papers WHERE paper_id=?", (row["paper_id"],)
    ).fetchone()
    if prior:
        if prior["sha256"] != sha:
            raise ValueError(f'Conflicting paper: {row["paper_id"]}')
        return False
    title, abstract = row.get("title") or "", row.get("abstract") or ""
    cursor = db.execute(
        """INSERT INTO papers
      (paper_id,title,abstract,category,license,sha256,
       text_bytes,text_chars,body)
      VALUES (?,?,?,?,?,?,?,?,?)""",
        (
            row["paper_id"],
            title,
            abstract,
            row.get("primary_category") or "",
            row.get("license"),
            sha,
            len(raw),
            len(text),
            zlib.compress(raw, level=3),
        ),
    )
    db.execute(
        "INSERT INTO search(rowid,title,abstract,body) VALUES (?,?,?,?)",
        (cursor.lastrowid, title, abstract, text),
    )
    return True


def update_paper(db, row):
    """Upsert one verified paper inside the caller's checkpoint transaction."""
    raw = row["text"].encode("utf-8")
    sha = hashlib.sha256(raw).hexdigest()
    if row.get("text_sha256") and sha != row["text_sha256"]:
        raise ValueError(f'Text checksum mismatch: {row["paper_id"]}')
    values = (
        sha,
        row.get("title") or "",
        row.get("abstract") or "",
        row.get("primary_category") or "",
        row.get("license"),
    )
    prior = db.execute(
        "SELECT id,sha256,title,abstract,category,license "
        "FROM papers WHERE paper_id=?",
        (row["paper_id"],),
    ).fetchone()
    if prior is None:
        add_paper(db, row)
        return "inserted"
    if tuple(prior)[1:] == values:
        return "unchanged"
    # Contentless FTS5 requires the ORIGINAL tokens to remove an old entry.
    if tuple(prior)[1:4] != values[:3]:
        old = db.execute(
            "SELECT body FROM papers WHERE id=?", (prior["id"],)
        ).fetchone()[0]
        db.execute(
            "INSERT INTO search(search,rowid,title,abstract,body) "
            "VALUES('delete',?,?,?,?)",
            (
                prior["id"],
                prior["title"],
                prior["abstract"],
                zlib.decompress(old).decode("utf-8"),
            ),
        )
        db.execute(
            "INSERT INTO search(rowid,title,abstract,body) VALUES(?,?,?,?)",
            (prior["id"], values[1], values[2], row["text"]),
        )
    if prior["sha256"] != sha:
        db.execute(
            "UPDATE papers SET body=?,sha256=?,text_bytes=?,text_chars=? "
            "WHERE id=?",
            (
                zlib.compress(raw, level=3),
                sha,
                len(raw),
                len(row["text"]),
                prior["id"],
            ),
        )
    db.execute(
        "UPDATE papers SET title=?,abstract=?,category=?,license=? WHERE id=?",
        (*values[1:], prior["id"]),
    )
    return "updated"


def require_stable_corpus(db):
    if db.execute(
        "SELECT 1 FROM settings WHERE key='refresh_target'"
    ).fetchone():
        raise ValueError("Corpus refresh in progress; resume it first")
