"""Publish fully audited, standalone serving generations without changing NAS."""

import argparse
from contextlib import closing
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import uuid

from audit import audit
from embeddings import file_hash
from library import connect
from refresh import source_manifest

TOTAL_PAPERS = 2856227


def manifest_digest(manifest):
    return hashlib.sha256(
        json.dumps(manifest, sort_keys=True).encode()
    ).hexdigest()


def sync(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def publish(root, destination, expected_manifest=None):
    root, destination = Path(root).resolve(), Path(destination).resolve()
    if root == destination:
        raise ValueError("Serving root must differ from canonical root")
    destination.mkdir(parents=True, exist_ok=True)
    with (root / "ingest.lock").open("a") as ingest_lock, (
        destination / "publish.lock"
    ).open("a") as publish_lock:
        for lock in (ingest_lock, publish_lock):
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with closing(connect(root / "library.sqlite3")) as source:
            manifest, refreshed = source_manifest(root, source)
        report = audit(
            root, text_only=True, expected_manifest=expected_manifest
        )
        if not report["ready_for_full_audit"] or (
            not refreshed
            and expected_manifest is None
            and report.get("papers") != TOTAL_PAPERS
        ):
            raise ValueError(f"Corpus incomplete: {report['reasons']}")
        stage = Path(tempfile.mkdtemp(prefix=".staging-", dir=destination))
        generation = destination / ("generation-" + uuid.uuid4().hex)
        pointer = destination / (".current-" + uuid.uuid4().hex)
        try:
            database = stage / "library.sqlite3"
            with closing(connect(root / "library.sqlite3")) as source:
                source.execute("BEGIN")
                size = (
                    source.execute("PRAGMA page_count").fetchone()[0]
                    * source.execute("PRAGMA page_size").fetchone()[0]
                )
                reserve = max(32 * 1024**3, (size + 4) // 5)
                if shutil.disk_usage(destination).free < size + reserve:
                    raise ValueError("Insufficient SSD staging headroom")
                with closing(sqlite3.connect(database)) as target:
                    source.backup(target, pages=4096)
                source.rollback()
            with closing(sqlite3.connect(database)) as target:
                target.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                mode = target.execute("PRAGMA journal_mode=DELETE").fetchone()
                if mode[0] != "delete":
                    raise ValueError("Snapshot must be standalone")
            report = audit(
                root,
                full=True,
                text_only=True,
                expected_manifest=expected_manifest,
                database=database,
                _lock_held=True,
            )
            if not report["verified_complete"]:
                raise ValueError("Staged corpus failed full text audit")
            if any(stage.glob("library.sqlite3-*")):
                raise ValueError("Snapshot has required or stale sidecars")
            if shutil.disk_usage(destination).free < reserve:
                raise ValueError("Insufficient remaining SSD headroom")
            stat = database.stat()
            receipt = {
                "schema": 1,
                "manifest_sha256": manifest_digest(manifest),
                "revision": manifest["revision"],
                "size": stat.st_size,
                "mtime_ns": stat.st_mtime_ns,
                "sha256": file_hash(database),
                "audit": report,
            }
            proof = stage / "receipt.json"
            proof.write_text(json.dumps(receipt, indent=2) + "\n")
            for path in (database, proof):
                path.chmod(0o444)
                sync(path)
            stage.chmod(0o555)
            sync(stage)
            stage.rename(generation)
            sync(destination)
            pointer.symlink_to(generation.name)
            os.replace(pointer, destination / "current")
            sync(destination)
            return receipt | {"generation": str(generation)}
        finally:
            if pointer.is_symlink():
                pointer.unlink()
            if stage.exists():
                stage.chmod(0o700)
                shutil.rmtree(stage)


def resolve_snapshot(destination, root, expected_manifest=None):
    """Pin a generation; check its receipt without rehashing a huge DB at login.

    The publisher verifies all bytes. Startup binds that local receipt to the
    immutable file's size/mtime and corpus identity. This is not protection
    against malicious same-user edits that deliberately forge the receipt.
    """
    destination, root = Path(destination).resolve(), Path(root)
    generation = (destination / "current").resolve(strict=True)
    if generation.parent != destination or not generation.name.startswith(
        "generation-"
    ):
        raise ValueError("Invalid serving generation path")
    with closing(connect(root / "library.sqlite3")) as source:
        manifest, refreshed = source_manifest(root, source)
    reference = expected_manifest
    if reference is None:
        reference = (
            manifest
            if refreshed
            else json.loads(
                Path(__file__).with_name("manifest.json").read_text()
            )
        )
    receipt = json.loads((generation / "receipt.json").read_text())
    database = generation / "library.sqlite3"
    stat = database.stat()
    report = receipt.get("audit", {})
    if (
        manifest != reference
        or receipt.get("schema") != 1
        or receipt.get("manifest_sha256") != manifest_digest(manifest)
        or receipt.get("revision") != manifest["revision"]
        or not report.get("verified_complete")
        or report.get("scope") != "text"
        or report.get("papers")
        != (
            report.get("source_rows_available", 0)
            + report.get("retained_papers", 0)
        )
        or not report.get("papers")
        or (
            not refreshed
            and expected_manifest is None
            and report["papers"] != TOTAL_PAPERS
        )
        or report.get("available_shards") != len(manifest["files"])
        or report.get("expected_shards") != len(manifest["files"])
        or database.is_symlink()
        or stat.st_mode & 0o222
        or stat.st_size != receipt.get("size")
        or stat.st_mtime_ns != receipt.get("mtime_ns")
        or any(generation.glob("library.sqlite3-*"))
    ):
        raise ValueError("Missing or stale serving readiness proof")
    with closing(connect(database)) as db:
        revision = db.execute(
            "SELECT value FROM settings WHERE key='revision'"
        ).fetchone()
        if (
            not revision
            or revision[0] != manifest["revision"]
            or db.execute("SELECT count(*) FROM papers").fetchone()[0]
            != report["papers"]
        ):
            raise ValueError("Serving database differs from readiness proof")
    return database


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    print(json.dumps(publish(args.root, args.destination), indent=2))
