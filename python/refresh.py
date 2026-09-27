"""Manually check or resume incremental refreshes of the Hugging Face corpus."""

import argparse
from contextlib import ExitStack, closing
import fcntl
import json
import os
from pathlib import Path
import re
import urllib.parse
import urllib.request

import pyarrow.parquet as pq

from download import download_file
from library import connect, require_stable_corpus, update_paper

API = "https://huggingface.co/api/datasets/secemp9/arxiv-complete"
SCHEMA = """
CREATE TABLE IF NOT EXISTS refresh_runs (
 revision TEXT PRIMARY KEY, manifest TEXT NOT NULL,
 state TEXT NOT NULL, retained INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS refresh_objects (
 hash TEXT PRIMARY KEY, rows INTEGER NOT NULL,
 member_rows INTEGER NOT NULL DEFAULT 0, complete INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS refresh_members (
 hash TEXT NOT NULL, paper_id TEXT NOT NULL, PRIMARY KEY(hash,paper_id)
);
CREATE INDEX IF NOT EXISTS refresh_member_id ON refresh_members(paper_id);
CREATE TABLE IF NOT EXISTS refresh_shards (
 revision TEXT NOT NULL, hash TEXT NOT NULL,
 rows_done INTEGER NOT NULL DEFAULT 0, complete INTEGER NOT NULL DEFAULT 0,
 inserted INTEGER NOT NULL DEFAULT 0, updated INTEGER NOT NULL DEFAULT 0,
 unchanged INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(revision,hash)
);
"""


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def identity(manifest):
    return canonical(
        {
            "revision": manifest["revision"],
            "files": sorted(
                [
                    (x["path"], x["size"], x["lfs"]["oid"])
                    for x in manifest["files"]
                ]
            ),
        }
    )


def validate_manifest(manifest):
    if not re.fullmatch(r"[0-9a-f]{40}", manifest.get("revision", "")):
        raise ValueError("Expected an immutable Hugging Face commit SHA")
    files = manifest.get("files")
    if not isinstance(files, list) or not 1 <= len(files) <= 1000:
        raise ValueError("Expected 1..1000 paper-text archives")
    names, hashes = set(), set()
    for item in files:
        name, sha = item.get("path", ""), item.get("lfs", {}).get("oid", "")
        if (
            not re.fullmatch(
                r"paper_text/[A-Za-z0-9_-][A-Za-z0-9_.-]*\.parquet", name
            )
            or not re.fullmatch(r"[0-9a-f]{64}", sha)
            or type(item.get("size")) is not int
            or item["size"] <= 0
            or item.get("lfs", {}).get("size", item["size"]) != item["size"]
            or name in names
            or sha in hashes
        ):
            raise ValueError("Invalid or duplicated paper-text archive")
        names.add(name)
        hashes.add(sha)
    return manifest


def resolve_manifest(revision="main"):
    # Resolve the moving ref ONCE; all tree pages and downloads use that SHA.
    url = API + "/revision/" + urllib.parse.quote(revision, safe="")
    with urllib.request.urlopen(url, timeout=30) as response:
        sha = json.load(response)["sha"]
    if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError("Hugging Face did not return a commit SHA")
    prefix = API + f"/tree/{sha}/paper_text?"
    url = prefix + "recursive=true&expand=false&limit=1000"
    files, seen = [], set()
    while url:
        if not url.startswith(prefix) or url in seen:
            raise ValueError("Invalid Hugging Face pagination URL")
        seen.add(url)
        with urllib.request.urlopen(url, timeout=30) as response:
            page = json.load(response)
            link = response.headers.get("Link", "")
        if not isinstance(page, list):
            raise ValueError("Invalid Hugging Face tree response")
        for item in page:
            if item.get("type") == "file" and item.get("path", "").endswith(
                ".parquet"
            ):
                files.append({k: item[k] for k in ("path", "size", "lfs")})
        if len(files) > 1000 or len(seen) > 100:
            raise ValueError("Unexpectedly large Hugging Face archive tree")
        next_page = re.search(r'<([^>]+)>;\s*rel="?next"?', link)
        url = next_page.group(1) if next_page else None
    return validate_manifest(
        {"revision": sha, "files": sorted(files, key=lambda x: x["path"])}
    )


def source_manifest(root, db, allow_pending=False):
    if not allow_pending:
        require_stable_corpus(db)
    active = db.execute(
        "SELECT value FROM settings WHERE key='refresh_revision'"
    ).fetchone()
    if not active:
        return json.loads((Path(root) / "manifest.json").read_text()), False
    row = db.execute(
        "SELECT manifest,state FROM refresh_runs WHERE revision=?",
        (active[0],),
    ).fetchone()
    if not row or row["state"] != "complete":
        raise ValueError("Missing completed refresh provenance")
    manifest = validate_manifest(json.loads(row["manifest"]))
    if manifest["revision"] != active[0]:
        raise ValueError("Refresh provenance revision mismatch")
    return manifest, True


def source_path(root, item, refreshed):
    root = Path(root)
    return (
        root / "refresh" / "objects" / (item["lfs"]["oid"] + ".parquet")
        if refreshed
        else root / item["path"]
    )


def plan(base, target):
    old = {x["lfs"]["oid"] for x in base["files"]}
    new = [x for x in target["files"] if x["lfs"]["oid"] not in old]
    return {
        "current_revision": base["revision"],
        "target_revision": target["revision"],
        "reused_archives": len(target["files"]) - len(new),
        "new_or_changed_archives": len(new),
        "download_bytes_upper_bound": sum(x["size"] for x in new),
        "removed_archive_paths": sorted(
            {x["path"] for x in base["files"]}
            - {x["path"] for x in target["files"]}
        ),
    }


def batches_after(path, start, columns=None):
    parquet = pq.ParquetFile(path)
    position = 0
    for group in range(parquet.num_row_groups):
        count = parquet.metadata.row_group(group).num_rows
        if position + count <= start:
            position += count
            continue
        for batch in parquet.iter_batches(
            batch_size=500, row_groups=[group], columns=columns
        ):
            end = position + len(batch)
            if end > start:
                yield end, batch.slice(max(0, start - position)).to_pylist()
            position = end


def cache_archive(root, revision, item, existing=None):
    target = source_path(root, item, True)
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists() and existing is not None and existing.exists():
        os.link(existing, target)
    download_file(revision, item, target)
    # Make archive bytes and their directory entry durable before SQLite
    # commits checkpoints that depend on them.
    with target.open("rb") as archive:
        os.fsync(archive.fileno())
    directory = os.open(target.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    return target


def index_members(db, item, path):
    sha = item["lfs"]["oid"]
    count = pq.ParquetFile(path).metadata.num_rows
    with db:
        db.execute(
            "INSERT OR IGNORE INTO refresh_objects(hash,rows) VALUES(?,?)",
            (sha, count),
        )
    state = db.execute(
        "SELECT * FROM refresh_objects WHERE hash=?", (sha,)
    ).fetchone()
    if state["rows"] != count:
        raise ValueError("Archive row count changed without a hash change")
    if state["complete"]:
        return count
    for end, rows in batches_after(path, state["member_rows"], ["paper_id"]):
        with db:
            for row in rows:
                if not isinstance(row["paper_id"], str) or not row["paper_id"]:
                    raise ValueError("Invalid paper ID")
                db.execute(
                    "INSERT INTO refresh_members VALUES(?,?)",
                    (sha, row["paper_id"]),
                )
            db.execute(
                "UPDATE refresh_objects SET member_rows=? WHERE hash=?",
                (end, sha),
            )
    with db:
        db.execute(
            "UPDATE refresh_objects SET complete=1 WHERE hash=?", (sha,)
        )
    return count


def initial_complete(root, db, manifest):
    states = {r["shard"]: r for r in db.execute("SELECT * FROM imports")}
    expected = 0
    for item in manifest["files"]:
        state = states.get(item["path"])
        if not state or not state["complete"]:
            raise ValueError(
                "Initial import incomplete; wait for it to finish"
            )
        count = pq.ParquetFile(Path(root) / item["path"]).metadata.num_rows
        if state["rows_done"] != count:
            raise ValueError("Initial import checkpoint differs from source")
        expected += count
    if (
        set(states) != {x["path"] for x in manifest["files"]}
        or db.execute("SELECT count(*) FROM papers").fetchone()[0] != expected
    ):
        raise ValueError("Initial paper/shard coverage differs from source")


def retained_papers(db, manifest):
    hashes = [x["lfs"]["oid"] for x in manifest["files"]]
    marks = ",".join("?" for _ in hashes)
    return db.execute(
        "SELECT count(*) FROM papers WHERE paper_id NOT IN "
        f"(SELECT paper_id FROM refresh_members WHERE hash IN ({marks}))",
        hashes,
    ).fetchone()[0]


def summary(db, revision):
    row = db.execute(
        "SELECT coalesce(sum(inserted),0) AS inserted, "
        "coalesce(sum(updated),0) AS updated, "
        "coalesce(sum(unchanged),0) AS unchanged "
        "FROM refresh_shards WHERE revision=?",
        (revision,),
    ).fetchone()
    run = db.execute(
        "SELECT state,retained FROM refresh_runs WHERE revision=?", (revision,)
    ).fetchone()
    return {"revision": revision, **dict(run), **dict(row)}


def refresh(root, manifest):
    """Apply in-place, batch-atomic updates. Never runs against a live importer."""
    root = Path(root)
    validate_manifest(manifest)
    if not (root / "library.sqlite3").exists():
        raise ValueError("Initial database not created")
    with ExitStack() as stack:
        paths = [root / "ingest.lock"]
        if (root / "embeddings").is_dir():
            paths += [
                root / "embeddings" / name
                for name in ("writer.lock", "index.lock")
            ]
        for path in paths:
            lock = stack.enter_context(path.open("a"))
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        db = stack.enter_context(
            closing(connect(root / "library.sqlite3", write=True))
        )
        base, refreshed = source_manifest(root, db, allow_pending=True)
        pending = db.execute(
            "SELECT value FROM settings WHERE key='refresh_target'"
        ).fetchone()
        if not refreshed and not pending:
            initial_complete(root, db, base)
        actual = db.execute(
            "SELECT value FROM settings WHERE key='revision'"
        ).fetchone()
        if not actual or actual[0] != base["revision"]:
            raise ValueError(
                "Database revision differs from source provenance"
            )
        revision = manifest["revision"]
        if pending and pending[0] != revision:
            raise ValueError(f"Resume unfinished revision {pending[0]} first")
        if revision == base["revision"]:
            if identity(manifest) != identity(base):
                raise ValueError(
                    "Manifest changed within an immutable revision"
                )
            return {"revision": revision, "state": "unchanged"}
        db.executescript(SCHEMA)
        prior = db.execute(
            "SELECT * FROM refresh_runs WHERE revision=?", (revision,)
        ).fetchone()
        if prior and (
            prior["manifest"] != canonical(manifest)
            or prior["state"] == "complete"
        ):
            raise ValueError(
                "Cannot replace provenance or replay an older completed revision"
            )
        # Bootstrap archive membership once, reading ONLY the ID column. It
        # allows repacks and removals to be detected without rescanning bodies.
        old_hashes = set()
        for item in base["files"]:
            sha = item["lfs"]["oid"]
            old_hashes.add(sha)
            path = cache_archive(
                root,
                base["revision"],
                item,
                source_path(root, item, refreshed),
            )
            index_members(db, item, path)
        for item in manifest["files"]:
            if item["lfs"]["oid"] not in old_hashes:
                path = cache_archive(root, revision, item)
                index_members(db, item, path)
        hashes = [x["lfs"]["oid"] for x in manifest["files"]]
        marks = ",".join("?" for _ in hashes)
        if db.execute(
            f"SELECT paper_id FROM refresh_members WHERE hash IN ({marks}) "
            "GROUP BY paper_id HAVING count(*)>1 LIMIT 1",
            hashes,
        ).fetchone():
            raise ValueError("Duplicate paper ID across target archives")
        with db:
            db.execute(
                "INSERT OR IGNORE INTO refresh_runs VALUES(?,?,'applying',0)",
                (revision, canonical(manifest)),
            )
            db.execute(
                "INSERT OR REPLACE INTO settings VALUES('refresh_target',?)",
                (revision,),
            )
            for sha in hashes:
                count = db.execute(
                    "SELECT rows FROM refresh_objects WHERE hash=?", (sha,)
                ).fetchone()[0]
                reused = sha in old_hashes
                db.execute(
                    "INSERT OR IGNORE INTO refresh_shards(revision,hash,rows_done,complete) VALUES(?,?,?,?)",
                    (revision, sha, count if reused else 0, int(reused)),
                )
        for item in manifest["files"]:
            sha = item["lfs"]["oid"]
            state = db.execute(
                "SELECT * FROM refresh_shards WHERE revision=? AND hash=?",
                (revision, sha),
            ).fetchone()
            if state["complete"]:
                continue
            for end, rows in batches_after(
                source_path(root, item, True), state["rows_done"]
            ):
                counts = dict.fromkeys(("inserted", "updated", "unchanged"), 0)
                with db:
                    for row in rows:
                        counts[update_paper(db, row)] += 1
                    db.execute(
                        "UPDATE refresh_shards SET rows_done=?,inserted=inserted+?,"
                        "updated=updated+?,unchanged=unchanged+? WHERE revision=? AND hash=?",
                        (
                            end,
                            counts["inserted"],
                            counts["updated"],
                            counts["unchanged"],
                            revision,
                            sha,
                        ),
                    )
                print(f"{item['path']}: {end} rows committed", flush=True)
            with db:
                db.execute(
                    "UPDATE refresh_shards SET complete=1 WHERE revision=? AND hash=?",
                    (revision, sha),
                )
        retained = retained_papers(db, manifest)
        with db:
            db.execute(
                "INSERT OR REPLACE INTO settings VALUES('revision',?)",
                (revision,),
            )
            db.execute(
                "INSERT OR REPLACE INTO settings VALUES('refresh_revision',?)",
                (revision,),
            )
            db.execute("DELETE FROM settings WHERE key='refresh_target'")
            db.execute(
                "UPDATE refresh_runs SET state='complete',retained=? WHERE revision=?",
                (retained, revision),
            )
        return plan(base, manifest) | summary(db, revision)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument(
        "--revision",
        help="Hugging Face ref; defaults to main, or the unfinished revision when applying",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Download and apply; otherwise only check upstream metadata",
    )
    args = parser.parse_args()
    with closing(connect(args.root / "library.sqlite3")) as db:
        base, _ = source_manifest(args.root, db, allow_pending=True)
        pending = db.execute(
            "SELECT value FROM settings WHERE key='refresh_target'"
        ).fetchone()
        if args.apply and pending and args.revision is None:
            target = json.loads(
                db.execute(
                    "SELECT manifest FROM refresh_runs WHERE revision=?",
                    (pending[0],),
                ).fetchone()[0]
            )
        else:
            target = resolve_manifest(args.revision or "main")
    result = refresh(args.root, target) if args.apply else plan(base, target)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
