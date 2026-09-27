import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from fixtures import import_state, matching_papers, paper, stored_paper
from library import add_paper, connect
from ingest import ingest


class LibraryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "library.sqlite3"
        self.db = connect(self.path, write=True)

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_ingestion_preserves_complete_body_and_indexes_its_tail(self):
        row = paper(text="preamble " * 10000 + "rarephysicalterm λ at the end")
        with self.db:
            add_paper(self.db, row)
        self.assertEqual(
            matching_papers(self.db, "rarephysicalterm"),
            [{"paper_id": row["paper_id"]}],
        )
        self.assertEqual(
            stored_paper(self.db, row["paper_id"])["text"], row["text"]
        )

    def test_filters_and_duplicate_integrity(self):
        with self.db:
            self.assertTrue(add_paper(self.db, paper()))
            self.assertFalse(add_paper(self.db, paper()))
        self.assertEqual(import_state(self.db)["papers"], 1)
        self.assertEqual(
            len(matching_papers(self.db, "quantum", category="physics")), 1
        )
        self.assertEqual(
            matching_papers(self.db, "quantum", category="math"), []
        )
        with self.assertRaises(ValueError):
            add_paper(self.db, paper(text="changed"))
        wrong = paper("wrong")
        wrong["text_sha256"] = "bad"
        with self.assertRaises(ValueError):
            add_paper(self.db, wrong)

    def test_readonly_and_transaction_rollback(self):
        with self.assertRaises(RuntimeError):
            with self.db:
                add_paper(self.db, paper())
                raise RuntimeError("interrupted transaction")
        self.assertEqual(import_state(self.db)["papers"], 0)
        self.assertEqual(matching_papers(self.db, "quantum"), [])
        ro = connect(self.path)
        with self.assertRaises(sqlite3.OperationalError):
            ro.execute("DELETE FROM papers")
        ro.close()


class IngestTest(unittest.TestCase):
    def test_verified_resume_and_idempotence(self):
        import pyarrow as pa
        import pyarrow.parquet as pq

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shard = root / "paper_text/test.parquet"
            shard.parent.mkdir()
            rows = [paper(str(i)) for i in range(1257)]
            pq.write_table(
                pa.Table.from_pylist(rows), shard, row_group_size=700
            )
            item = {
                "path": "paper_text/test.parquet",
                "size": shard.stat().st_size,
                "lfs": {"oid": hashlib.sha256(shard.read_bytes()).hexdigest()},
            }
            (root / "manifest.json").write_text(
                json.dumps({"revision": "test", "files": [item]})
            )

            def interrupted_add(db, row):
                if row["paper_id"] == "511":
                    raise RuntimeError("interrupted batch")
                return add_paper(db, row)

            with patch("ingest.add_paper", side_effect=interrupted_add):
                with self.assertRaisesRegex(RuntimeError, "interrupted batch"):
                    ingest(root)
            db = connect(root / "library.sqlite3")
            self.assertEqual(import_state(db)["papers"], 500)
            self.assertEqual(import_state(db)["imports"][0]["rows_done"], 500)
            self.assertEqual(len(matching_papers(db, "rarephysicalterm")), 500)
            db.close()
            ingest(root, max_papers=13)
            db = connect(root / "library.sqlite3")
            self.assertEqual(import_state(db)["papers"], 513)
            self.assertEqual(import_state(db)["imports"][0]["rows_done"], 513)
            db.close()
            ingest(root)
            ingest(root)
            db = connect(root / "library.sqlite3")
            self.assertEqual(import_state(db)["papers"], 1257)
            self.assertEqual(import_state(db)["imports"][0]["complete"], 1)
            self.assertEqual(len(matching_papers(db, "quantum")), 1257)
            for row in rows:
                self.assertEqual(
                    stored_paper(db, row["paper_id"])["text"], row["text"]
                )
            self.assertEqual(
                db.execute("PRAGMA integrity_check").fetchone()[0], "ok"
            )
            db.close()

    def test_invalid_shard_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "test.parquet").write_bytes(b"broken")
            item = {"path": "test.parquet", "size": 6, "lfs": {"oid": "bad"}}
            (root / "manifest.json").write_text(
                json.dumps({"revision": "test", "files": [item]})
            )
            with self.assertRaises(ValueError):
                ingest(root)


if __name__ == "__main__":
    unittest.main()
