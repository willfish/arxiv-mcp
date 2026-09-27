from contextlib import closing
import fcntl
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import pyarrow as pa
import pyarrow.parquet as pq

from audit import audit
from embeddings import file_hash
from ingest import ingest
from library import connect
from fixtures import (
    stored_paper as get_paper,
    matching_papers as search_papers,
)
from snapshot import publish, resolve_snapshot
from fixtures import paper


class SnapshotTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "canonical"
        self.destination = Path(self.tmp.name) / "serving"
        self.root.mkdir()
        shard = self.root / "papers.parquet"
        pq.write_table(
            pa.Table.from_pylist(
                [paper("a", "rarephysicalterm λ"), paper("b")]
            ),
            shard,
        )
        self.manifest = {
            "revision": "test",
            "files": [
                {
                    "path": shard.name,
                    "size": shard.stat().st_size,
                    "lfs": {"oid": file_hash(shard)},
                }
            ],
        }
        (self.root / "manifest.json").write_text(json.dumps(self.manifest))
        ingest(self.root)

    def publish(self):
        return publish(self.root, self.destination, self.manifest)

    def resolve(self):
        return resolve_snapshot(self.destination, self.root, self.manifest)

    def test_wal_backup_full_audit_and_pinned_old_reader(self):
        original = self.root / "library.sqlite3"
        with closing(sqlite3.connect(original)) as writer:
            with writer:
                writer.execute(
                    "INSERT INTO settings VALUES('wal-probe','present')"
                )
            wal = Path(str(original) + "-wal")
            before = (file_hash(original), file_hash(wal))
            receipt = self.publish()
            self.assertEqual(before, (file_hash(original), file_hash(wal)))
            first = self.resolve()
            self.assertEqual(receipt["sha256"], file_hash(first))
            self.assertFalse(list(first.parent.glob("library.sqlite3-*")))
            with closing(connect(first)) as reader:
                self.assertEqual(
                    reader.execute(
                        "SELECT value FROM settings WHERE key='wal-probe'"
                    ).fetchone()[0],
                    "present",
                )
                ids = [
                    tuple(r)
                    for r in reader.execute(
                        "SELECT id,paper_id FROM papers ORDER BY id"
                    )
                ]
                self.assertEqual(ids, [(1, "a"), (2, "b")])
                self.assertEqual(
                    get_paper(reader, "a")["text"], "rarephysicalterm λ"
                )
                self.assertTrue(search_papers(reader, "rarephysicalterm"))
                self.publish()
                self.assertNotEqual(first, self.resolve())
                self.assertTrue(first.exists())
                self.assertEqual(get_paper(reader, "a")["paper_id"], "a")

    def test_missing_readiness_fails_closed(self):
        with self.assertRaises(FileNotFoundError):
            self.resolve()

    def test_stale_receipt_and_modified_database_rejected(self):
        self.publish()
        database = self.resolve()
        proof = database.parent / "receipt.json"
        original = proof.read_text()
        proof.chmod(0o644)
        receipt = json.loads(original)
        receipt["audit"]["scope"] = "text-and-embeddings"
        proof.write_text(json.dumps(receipt))
        with self.assertRaisesRegex(ValueError, "readiness"):
            self.resolve()
        proof.write_text(original)
        database.chmod(0o644)
        with self.assertRaisesRegex(ValueError, "readiness"):
            self.resolve()
        database.parent.chmod(0o755)
        with closing(sqlite3.connect(database)) as db, db:
            db.execute("UPDATE papers SET text_chars=text_chars+1")
        database.chmod(0o444)
        database.parent.chmod(0o555)
        with self.assertRaisesRegex(ValueError, "readiness"):
            self.resolve()

    def test_incomplete_wrong_revision_and_unpinned_source_rejected(self):
        with self.assertRaisesRegex(ValueError, "incomplete"):
            publish(self.root, self.destination)
        for sql in [
            "UPDATE imports SET complete=0",
            "UPDATE settings SET value='wrong' WHERE key='revision'",
        ]:
            with self.subTest(sql=sql):
                with closing(
                    sqlite3.connect(self.root / "library.sqlite3")
                ) as db:
                    db.execute("UPDATE imports SET complete=1")
                    db.execute(sql)
                    db.commit()
                with self.assertRaisesRegex(ValueError, "incomplete"):
                    self.publish()
        self.assertFalse((self.destination / "current").exists())

    def test_ingest_lock_is_nonblocking(self):
        with (self.root / "ingest.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(BlockingIOError):
                self.publish()
        self.assertFalse((self.destination / "current").exists())

    def test_failures_preserve_previous_generation(self):
        self.publish()
        previous = self.resolve()

        def fail_full(*args, **kwargs):
            if kwargs.get("full"):
                raise ValueError("corrupt staging")
            return audit(*args, **kwargs)

        def interrupted(path):
            wrapped = Mock(wraps=connect(path))
            wrapped.backup.side_effect = RuntimeError("copy interrupted")
            return wrapped

        cases = [
            (
                patch(
                    "snapshot.shutil.disk_usage",
                    return_value=SimpleNamespace(free=0),
                ),
                ValueError,
            ),
            (patch("snapshot.audit", side_effect=fail_full), ValueError),
            (patch("snapshot.connect", side_effect=interrupted), RuntimeError),
            (
                patch(
                    "snapshot.os.replace",
                    side_effect=OSError("publication failed"),
                ),
                OSError,
            ),
        ]
        for context, error in cases:
            with context, self.assertRaises(error):
                self.publish()
            self.assertEqual(self.resolve(), previous)
            self.assertFalse(list(self.destination.glob(".staging-*")))
            self.assertFalse(list(self.destination.glob(".current-*")))


if __name__ == "__main__":
    unittest.main()
