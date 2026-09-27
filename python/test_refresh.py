from contextlib import closing, redirect_stdout
import fcntl
import io
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import pyarrow as pa
import pyarrow.parquet as pq

from audit import audit
from download import digest, download_file
from ingest import ingest
from library import connect, get_paper, search_papers, update_paper
from refresh import (
    API,
    main,
    plan,
    refresh,
    resolve_manifest,
    source_manifest,
    source_path,
    validate_manifest,
)
from snapshot import publish, resolve_snapshot
from test_library import paper


class RefreshTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name)
        self.root = self.folder / "library"
        self.root.mkdir()
        self.files = {}
        self.downloads = []
        self.base = self.manifest(
            "1",
            [
                ("stable", [paper("stable", "stableword")]),
                ("changing", [paper("changing", "oldneedle λ")]),
            ],
        )
        for item in self.base["files"]:
            path = self.root / item["path"]
            path.parent.mkdir(exist_ok=True)
            shutil.copyfile(self.files[item["lfs"]["oid"]], path)
        (self.root / "manifest.json").write_text(json.dumps(self.base))
        with redirect_stdout(io.StringIO()):
            ingest(self.root)
        self.fetch = patch("refresh.download_file", side_effect=self.download)
        self.fetch.start()
        self.addCleanup(self.fetch.stop)

    def manifest(self, rev, archives):
        files = []
        for name, rows in archives:
            path = (
                self.folder / (rev * 40) / "paper_text" / (name + ".parquet")
            )
            path.parent.mkdir(parents=True, exist_ok=True)
            pq.write_table(
                pa.Table.from_pylist(rows), path, row_group_size=500
            )
            sha = digest(path)
            self.files[sha] = path
            files.append(
                {
                    "path": "paper_text/" + path.name,
                    "size": path.stat().st_size,
                    "lfs": {"oid": sha},
                }
            )
        return {"revision": rev * 40, "files": files}

    def target(self, rows=None, revision="2"):
        target = self.manifest(
            revision,
            [
                (
                    "changing",
                    (
                        rows
                        if rows is not None
                        else [
                            paper("changing", "newneedle 😀"),
                            paper("new", "newpaper"),
                        ]
                    ),
                )
            ],
        )
        target["files"].insert(0, self.base["files"][0])
        return target

    def download(self, revision, item, target):
        if not target.exists():
            self.downloads.append(item["lfs"]["oid"])
            shutil.copyfile(self.files[item["lfs"]["oid"]], target)
        download_file(revision, item, target)

    def apply(self, target):
        with redirect_stdout(io.StringIO()):
            return refresh(self.root, target)

    def database(self):
        return closing(connect(self.root / "library.sqlite3", write=True))

    def test_incremental_download_insert_update_and_noop(self):
        target = self.target()
        before = (self.root / "manifest.json").read_bytes()
        with self.database() as db:
            old_id = db.execute(
                "SELECT id FROM papers WHERE paper_id='changing'"
            ).fetchone()[0]
            db.execute(
                "CREATE TRIGGER protect_stable BEFORE UPDATE ON papers "
                "WHEN OLD.paper_id='stable' BEGIN SELECT RAISE(FAIL,'unchanged write'); END"
            )
        with patch("refresh.update_paper", wraps=update_paper) as upsert:
            result = self.apply(target)
            self.assertEqual(upsert.call_count, 2)
        self.assertEqual(self.downloads, [target["files"][1]["lfs"]["oid"]])
        self.assertEqual(
            (result["inserted"], result["updated"], result["retained"]),
            (1, 1, 0),
        )
        self.assertEqual(before, (self.root / "manifest.json").read_bytes())
        with self.database() as db:
            self.assertEqual(get_paper(db, "changing")["text"], "newneedle 😀")
            self.assertEqual(search_papers(db, "oldneedle"), [])
            self.assertEqual(
                search_papers(db, "newneedle")[0]["paper_id"], "changing"
            )
            self.assertEqual(
                db.execute(
                    "SELECT id FROM papers WHERE paper_id='changing'"
                ).fetchone()[0],
                old_id,
            )
            self.assertEqual(source_manifest(self.root, db), (target, True))
        with patch(
            "refresh.cache_archive", side_effect=AssertionError("no work")
        ):
            self.assertEqual(self.apply(target)["state"], "unchanged")
        self.assertTrue(
            audit(self.root, full=True, text_only=True)["verified_complete"]
        )

    def test_repacked_renamed_and_reused_historical_archives(self):
        # Same archive, different filename: membership and contents are reused.
        renamed = json.loads(json.dumps(self.base))
        renamed["revision"] = "2" * 40
        renamed["files"][0]["path"] = "paper_text/renamed.parquet"
        with patch(
            "refresh.update_paper", side_effect=AssertionError("no body work")
        ):
            self.apply(renamed)
        self.assertFalse(self.downloads)
        # Repacking requires reading a replacement archive, but no paper writes.
        target = self.manifest(
            "3",
            [
                (
                    "combined",
                    [
                        paper("stable", "stableword"),
                        paper("changing", "oldneedle λ"),
                    ],
                )
            ],
        )
        result = self.apply(target)
        self.assertEqual(
            (result["inserted"], result["updated"], result["unchanged"]),
            (0, 0, 2),
        )
        self.apply(self.target(revision="4"))
        revert = json.loads(json.dumps(target))
        revert["revision"] = "5" * 40
        downloads = len(self.downloads)
        result = self.apply(revert)
        self.assertEqual(len(self.downloads), downloads)
        self.assertEqual(result["updated"], 1)
        self.assertEqual(result["retained"], 1)
        with self.assertRaisesRegex(ValueError, "older completed"):
            self.apply(target)

    def test_metadata_only_updates_refresh_tokens_and_filters(self):
        row = paper("changing", "oldneedle λ")
        row.update(
            title="newtitleword",
            abstract="newabstractword",
            primary_category="cs.AI",
            license="newlicense",
        )
        with self.database() as db:
            old = db.execute(
                "SELECT body FROM papers WHERE paper_id='changing'"
            ).fetchone()[0]
        result = self.apply(self.target([row]))
        self.assertEqual(result["updated"], 1)
        with self.database() as db:
            self.assertEqual(
                db.execute(
                    "SELECT body FROM papers WHERE paper_id='changing'"
                ).fetchone()[0],
                old,
            )
            self.assertEqual(
                search_papers(db, "newtitleword", category="cs.AI")[0][
                    "paper_id"
                ],
                "changing",
            )
            self.assertFalse(
                search_papers(db, "newtitleword", category="physics")
            )
            self.assertEqual(
                get_paper(db, "changing")["license"], "newlicense"
            )
        self.assertTrue(
            audit(self.root, full=True, text_only=True)["verified_complete"]
        )

    def test_removed_papers_are_retained_and_snapshot_is_revision_aware(self):
        target = {"revision": "2" * 40, "files": [self.base["files"][0]]}
        result = self.apply(target)
        self.assertEqual(result["retained"], 1)
        report = audit(self.root, full=True, text_only=True)
        self.assertTrue(report["verified_complete"])
        self.assertEqual(
            (
                report["papers"],
                report["source_rows_available"],
                report["retained_papers"],
            ),
            (2, 1, 1),
        )
        destination = self.folder / "serving"
        receipt = publish(self.root, destination)
        self.assertEqual(receipt["revision"], target["revision"])
        path = resolve_snapshot(destination, self.root)
        with closing(connect(path)) as db:
            self.assertTrue(search_papers(db, "oldneedle"))
        with self.database() as db, db:
            db.execute(
                "INSERT INTO papers SELECT id+100,'untracked',title,abstract,category,license,sha256,text_bytes,text_chars,body FROM papers LIMIT 1"
            )
        self.assertFalse(
            audit(self.root, text_only=True)["ready_for_full_audit"]
        )

    def test_failed_batch_rolls_back_papers_fts_and_checkpoint_then_resumes(
        self,
    ):
        rows = [paper("changing", "newneedle")] + [
            paper(f"new-{i}", f"unique{i}") for i in range(510)
        ]
        target = self.target(rows)
        calls = 0

        def interrupted(db, row):
            nonlocal calls
            calls += 1
            if calls == 502:
                raise RuntimeError("interrupted")
            return update_paper(db, row)

        with patch(
            "refresh.update_paper", side_effect=interrupted
        ), self.assertRaisesRegex(RuntimeError, "interrupted"):
            self.apply(target)
        with self.database() as db:
            self.assertEqual(
                db.execute(
                    "SELECT rows_done FROM refresh_shards WHERE revision=? AND hash=?",
                    (target["revision"], target["files"][1]["lfs"]["oid"]),
                ).fetchone()[0],
                500,
            )
            self.assertFalse(
                db.execute(
                    "SELECT 1 FROM papers WHERE paper_id='new-499'"
                ).fetchone()
            )
            self.assertFalse(search_papers(db, "unique499"))
            self.assertEqual(
                db.execute(
                    "SELECT value FROM settings WHERE key='revision'"
                ).fetchone()[0],
                self.base["revision"],
            )
        self.assertIn(
            "progress", audit(self.root, text_only=True)["reasons"][0]
        )
        with self.assertRaisesRegex(ValueError, "progress"):
            publish(self.root, self.folder / "serving")
        with self.assertRaisesRegex(ValueError, "Resume unfinished"):
            self.apply(self.target(revision="3"))
        with patch("refresh.update_paper", wraps=update_paper) as upsert:
            result = self.apply(target)
            self.assertEqual(upsert.call_count, 11)
        self.assertEqual((result["inserted"], result["updated"]), (510, 1))
        self.assertTrue(
            audit(self.root, full=True, text_only=True)["verified_complete"]
        )

    def test_initial_import_locks_and_incomplete_coverage_are_gates(self):
        target = self.target()
        with (self.root / "ingest.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(BlockingIOError):
                self.apply(target)
        with self.database() as db, db:
            db.execute("UPDATE imports SET complete=0")
        with self.assertRaisesRegex(ValueError, "Initial import incomplete"):
            self.apply(target)
        self.assertFalse(self.downloads)

    def test_initial_service_does_not_reimport_refreshed_corpus(self):
        self.apply(self.target())
        with patch(
            "ingest.add_paper", side_effect=AssertionError("initial importer")
        ), redirect_stdout(io.StringIO()):
            ingest(self.root)

    def test_conflicting_ids_and_hashes_rejected_before_paper_updates(self):
        target = self.target([paper("stable", "conflict")])
        with self.assertRaisesRegex(ValueError, "Duplicate paper"):
            self.apply(target)
        with self.database() as db:
            self.assertEqual(get_paper(db, "stable")["text"], "stableword")
            self.assertFalse(
                db.execute(
                    "SELECT 1 FROM settings WHERE key='refresh_target'"
                ).fetchone()
            )
        target = self.target()
        cached = source_path(self.root, target["files"][1], True)
        cached.parent.mkdir(parents=True, exist_ok=True)
        cached.write_bytes(b"corrupt")
        with self.assertRaisesRegex(RuntimeError, "corrupt"):
            self.apply(target)

    def test_bad_text_checksum_rolls_back_body_and_old_search_tokens(self):
        row = paper("changing", "newneedle")
        row["text_sha256"] = "bad"
        with self.assertRaisesRegex(ValueError, "checksum"):
            self.apply(self.target([row]))
        with self.database() as db:
            self.assertEqual(get_paper(db, "changing")["text"], "oldneedle λ")
            self.assertTrue(search_papers(db, "oldneedle"))
            self.assertFalse(search_papers(db, "newneedle"))

    def test_update_failure_after_fts_deletion_is_atomic(self):
        with self.database() as db:
            db.execute(
                "CREATE TRIGGER reject_change BEFORE UPDATE ON papers BEGIN SELECT RAISE(FAIL,'reject'); END"
            )
        with self.assertRaisesRegex(sqlite3.IntegrityError, "reject"):
            self.apply(self.target())
        with self.database() as db:
            self.assertTrue(search_papers(db, "oldneedle"))
            self.assertFalse(search_papers(db, "newneedle"))
            db.execute("DROP TRIGGER reject_change")
        self.apply(self.target())

    def test_check_is_read_only_and_reports_reused_downloads(self):
        target = self.target()
        before = digest(self.root / "library.sqlite3")
        with patch("sys.argv", ["refresh", str(self.root)]), patch(
            "refresh.resolve_manifest", return_value=target
        ), redirect_stdout(io.StringIO()) as output:
            main()
        report = json.loads(output.getvalue())
        self.assertEqual(report["reused_archives"], 1)
        self.assertEqual(
            report["download_bytes_upper_bound"], target["files"][1]["size"]
        )
        self.assertFalse(self.downloads)
        self.assertEqual(digest(self.root / "library.sqlite3"), before)
        self.assertEqual(report, plan(self.base, target))

    def test_category_license_only_update_does_not_rewrite_fts_or_body(self):
        row = paper("changing", "oldneedle λ")
        row.update(primary_category="cs.AI", license="changed")
        statements = []
        with self.database() as db, db:
            db.set_trace_callback(statements.append)
            self.assertEqual(update_paper(db, row), "updated")
            self.assertFalse(any("INTO search(" in sql for sql in statements))
            self.assertFalse(any("SET body=" in sql for sql in statements))
            self.assertEqual(get_paper(db, "changing")["category"], "cs.AI")

    def test_default_apply_resumes_pinned_revision_without_network_resolution(
        self,
    ):
        target = self.target()
        with patch(
            "refresh.update_paper", side_effect=RuntimeError("stop")
        ), self.assertRaises(RuntimeError):
            self.apply(target)
        with patch("sys.argv", ["refresh", str(self.root), "--apply"]), patch(
            "refresh.resolve_manifest",
            side_effect=AssertionError("must resume pinned revision"),
        ), redirect_stdout(io.StringIO()):
            main()
        with self.database() as db:
            self.assertEqual(source_manifest(self.root, db), (target, True))

    def test_embedding_locks_and_pending_semantic_guard(self):
        from embeddings import generate
        from semantic import SemanticSearch
        from test_semantic import ConceptModel

        folder = self.root / "embeddings"
        folder.mkdir()
        for name in ("writer.lock", "index.lock"):
            with (folder / name).open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with self.assertRaises(BlockingIOError):
                    self.apply(self.target())
        with patch(
            "refresh.update_paper", side_effect=RuntimeError("stop")
        ), self.assertRaises(RuntimeError):
            self.apply(self.target())
        with self.assertRaisesRegex(ValueError, "progress"):
            generate(self.root, ConceptModel(), max_blocks=0)
        with self.database() as db, self.assertRaisesRegex(
            ValueError, "progress"
        ):
            SemanticSearch(self.root).load(db)

    def test_snapshot_receipt_becomes_stale_after_a_refresh(self):
        destination = self.folder / "serving"
        publish(self.root, destination, expected_manifest=self.base)
        old = resolve_snapshot(destination, self.root, self.base)
        self.apply(self.target())
        with self.assertRaisesRegex(ValueError, "readiness"):
            resolve_snapshot(destination, self.root)
        with closing(connect(old)) as db:
            self.assertEqual(get_paper(db, "changing")["text"], "oldneedle λ")
        report = audit(self.root, full=True, text_only=True, database=old)
        self.assertFalse(report["verified_complete"])
        self.assertIn("revision differs", report["reasons"][0])
        publish(self.root, destination)
        self.assertNotEqual(resolve_snapshot(destination, self.root), old)

    def test_full_audit_refuses_an_active_corpus_writer(self):
        self.apply(self.target())
        with (self.root / "ingest.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            report = audit(self.root, full=True, text_only=True)
        self.assertFalse(report["verified_complete"])
        self.assertEqual(report["reasons"], ["Corpus writer active"])

    def test_revision_noop_ignores_irrelevant_upstream_metadata(self):
        same = json.loads(json.dumps(self.base))
        same["files"][0]["type"] = "file"
        same["files"][0]["lfs"]["size"] = same["files"][0]["size"]
        self.assertEqual(self.apply(same)["state"], "unchanged")

    def test_manifest_paths_commits_and_duplicate_hashes_are_validated(self):
        for change in ("path", "revision", "hash", "duplicate", "size"):
            target = self.target()
            if change == "path":
                target["files"][0]["path"] = "paper_text/../../outside.parquet"
            elif change == "revision":
                target["revision"] = "main"
            elif change == "hash":
                target["files"][0]["lfs"]["oid"] = "wrong"
            elif change == "size":
                target["files"][0]["size"] = True
            else:
                target["files"].append(target["files"][0])
            with self.subTest(change=change), self.assertRaises(ValueError):
                validate_manifest(target)


class ResolutionTest(unittest.TestCase):
    def response(self, value, link=""):
        response = io.BytesIO(json.dumps(value).encode())
        response.headers = {"Link": link}
        return response

    def test_ref_is_pinned_and_tree_pagination_followed(self):
        sha = "a" * 40
        prefix = API + f"/tree/{sha}/paper_text?"
        item = {
            "type": "file",
            "path": "paper_text/a.parquet",
            "size": 12,
            "lfs": {"oid": "b" * 64},
        }
        responses = [
            self.response({"sha": sha}),
            self.response([item], f'<{prefix}cursor=next>; rel="next"'),
            self.response([]),
        ]
        with patch(
            "refresh.urllib.request.urlopen", side_effect=responses
        ) as fetch:
            result = resolve_manifest()
        self.assertEqual(result["revision"], sha)
        self.assertEqual(len(result["files"]), 1)
        self.assertIn(sha, fetch.call_args_list[1].args[0])
        self.assertEqual(
            fetch.call_args_list[2].args[0], prefix + "cursor=next"
        )

    def test_off_origin_pagination_is_not_followed(self):
        responses = [
            self.response({"sha": "a" * 40}),
            self.response([], '<https://other.test/secret>; rel="next"'),
        ]
        with patch(
            "refresh.urllib.request.urlopen", side_effect=responses
        ) as fetch, self.assertRaisesRegex(ValueError, "pagination"):
            resolve_manifest()
        self.assertEqual(fetch.call_count, 2)


if __name__ == "__main__":
    unittest.main()
