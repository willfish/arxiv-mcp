"""Verify corpus, passage and index coverage independently of job status."""

import argparse
from contextlib import ExitStack, closing
import fcntl
import hashlib
import json
from pathlib import Path
import sqlite3

import faiss
import numpy as np
import pyarrow.parquet as pq
import zlib

from embeddings import (
    MODEL,
    MODEL_REVISION,
    WINDOW,
    OVERLAP,
    file_hash,
    load_model,
    vector_id,
)
from library import connect
from refresh import retained_papers, source_manifest, source_path


def passage_count(chars):
    return 1 + max(
        0, (chars - WINDOW + WINDOW - OVERLAP - 1) // (WINDOW - OVERLAP)
    )


def index_ids(index):
    if isinstance(index, faiss.IndexIDMap2):
        return faiss.vector_to_array(index.id_map)
    ivf = faiss.extract_index_ivf(index)
    lists = ivf.invlists
    pieces = []
    for number in range(ivf.nlist):
        size = lists.list_size(number)
        if size:
            ptr = lists.get_ids(number)
            try:
                pieces.append(faiss.rev_swig_ptr(ptr, size).copy())
            finally:
                lists.release_ids(number, ptr)
    return np.concatenate(pieces) if pieces else np.empty(0, dtype=np.int64)


def verify_sqlite(database):
    # FTS integrity checking requires a writable connection, but changes no
    # indexed content. Call only after full import coverage has been verified.
    with closing(sqlite3.connect(database, timeout=30)) as db:
        checks = [r[0] for r in db.execute("PRAGMA integrity_check")]
        if checks != ["ok"]:
            raise ValueError(f"SQLite integrity check failed: {checks}")
        with db:
            db.execute("INSERT INTO search(search) VALUES('integrity-check')")


def audit(
    root,
    full=False,
    expected_manifest=None,
    text_only=False,
    database=None,
    _lock_held=False,
):
    root = Path(root)
    database = (
        Path(database) if database is not None else root / "library.sqlite3"
    )
    manifest = json.loads((root / "manifest.json").read_text())
    refreshed = False
    authority = root / "library.sqlite3"
    if authority.exists():
        with closing(connect(authority)) as source:
            try:
                manifest, refreshed = source_manifest(root, source)
            except ValueError as error:
                return {
                    "verified_complete": False,
                    "ready_for_full_audit": False,
                    "reasons": [str(error)],
                }
    reference = expected_manifest
    if reference is None:
        reference = (
            manifest
            if refreshed
            else json.loads(
                Path(__file__).with_name("manifest.json").read_text()
            )
        )
    expected, available, reasons = 0, 0, []
    if manifest != reference:
        return {
            "verified_complete": False,
            "ready_for_full_audit": False,
            "reasons": [
                "Corpus manifest differs from the shipped source lock"
            ],
        }
    shard_rows = {}
    for item in manifest["files"]:
        path = source_path(root, item, refreshed)
        if not path.exists() or path.stat().st_size != item["size"]:
            reasons.append(f'Missing or wrong-sized shard: {item["path"]}')
            continue
        available += 1
        count = pq.ParquetFile(path).metadata.num_rows
        shard_rows[item["path"]] = count
        expected += count
    if not database.exists():
        return {
            "verified_complete": False,
            "ready_for_full_audit": False,
            "reasons": reasons + ["Database not created"],
            "available_shards": available,
        }
    with closing(connect(database)) as db, ExitStack() as resources:
        revision = db.execute(
            "SELECT value FROM settings WHERE key='revision'"
        ).fetchone()
        if not revision or revision[0] != manifest["revision"]:
            return {
                "verified_complete": False,
                "ready_for_full_audit": False,
                "reasons": reasons
                + ["Database revision differs from manifest"],
            }
        papers = db.execute("SELECT count(*) FROM papers").fetchone()[0]
        if refreshed:
            checkpoints = {
                r["hash"]: dict(r)
                for r in db.execute(
                    "SELECT * FROM refresh_shards WHERE revision=?",
                    (manifest["revision"],),
                )
            }
            imports = {
                item["path"]: checkpoints[item["lfs"]["oid"]]
                for item in manifest["files"]
                if item["lfs"]["oid"] in checkpoints
            }
        else:
            imports = {
                r["shard"]: dict(r)
                for r in db.execute("SELECT * FROM imports")
            }
        if set(imports) - set(shard_rows):
            reasons.append("Unexpected imported shard")
        for name, count in shard_rows.items():
            state = imports.get(name)
            if (
                not state
                or not state["complete"]
                or state["rows_done"] != count
            ):
                reasons.append(f"Import incomplete: {name}")
        retained = retained_papers(db, manifest) if refreshed else 0
        if papers != expected + retained:
            reasons.append("Paper count differs from available source rows")
        if (
            refreshed
            and db.execute(
                "SELECT 1 FROM papers p WHERE NOT EXISTS "
                "(SELECT 1 FROM refresh_members m WHERE m.paper_id=p.paper_id) "
                "LIMIT 1"
            ).fetchone()
        ):
            reasons.append("Paper has no known source-archive membership")
        folder = root / "embeddings"
        blocks = [] if text_only else sorted(folder.glob("block-*"))
        states = [
            json.loads((b / "metadata.json").read_text()) for b in blocks
        ]
        encoded = sum(s["papers"] for s in states)
        passages = sum(s["passages"] for s in states)
        if not text_only and encoded != papers:
            reasons.append("Encoded paper count differs from database")
        pointer = folder / "index.json"
        published = (
            json.loads(pointer.read_text())
            if not text_only and pointer.exists()
            else None
        )
        if not text_only and (
            not published
            or published["papers"] != papers
            or published["passages"] != passages
        ):
            reasons.append("Published embedding index is incomplete")
        report = {
            "verified_complete": False,
            "scope": "text" if text_only else "text-and-embeddings",
            "ready_for_full_audit": not reasons,
            "available_shards": available,
            "expected_shards": len(manifest["files"]),
            "source_rows_available": expected,
            "papers": papers,
            "retained_papers": retained,
            "revision": manifest["revision"],
            **(
                {}
                if text_only
                else {
                    "encoded_papers": encoded,
                    "encoded_passages": passages,
                    "published_papers": (
                        published["papers"] if published else 0
                    ),
                }
            ),
            "reasons": reasons,
        }
        if not full or reasons:
            return report
        if not _lock_held:
            lock = resources.enter_context((root / "ingest.lock").open("a"))
            try:
                fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError:
                return report | {
                    "ready_for_full_audit": False,
                    "reasons": ["Corpus writer active"],
                }
            with closing(connect(authority)) as source:
                selected, _ = source_manifest(root, source)
            if selected != manifest:
                raise ValueError("Corpus revision changed before audit lock")
        # Wait for complete import before auditing, avoiding a prolonged
        # read transaction that pins a growing WAL during initial ingestion.
        for item in manifest["files"]:
            path = source_path(root, item, refreshed)
            if file_hash(path) != item["lfs"]["oid"]:
                raise ValueError(f"Source checksum mismatch: {path}")
            columns = [
                "paper_id",
                "text_sha256",
                "title",
                "abstract",
                "primary_category",
                "license",
            ]
            for batch in pq.ParquetFile(path).iter_batches(
                batch_size=256, columns=columns
            ):
                for source in batch.to_pylist():
                    row = db.execute(
                        "SELECT sha256,title,abstract,category,license "
                        "FROM papers WHERE paper_id=?",
                        (source["paper_id"],),
                    ).fetchone()
                    expected_values = (
                        source["text_sha256"],
                        source["title"] or "",
                        source["abstract"] or "",
                        source["primary_category"] or "",
                        source["license"],
                    )
                    if row is None or tuple(row) != expected_values:
                        raise ValueError(
                            "Paper differs from pinned source: "
                            f'{source["paper_id"]}'
                        )
        for row in db.execute(
            "SELECT paper_id,body,sha256,text_bytes,text_chars FROM papers"
        ):
            raw = zlib.decompress(row["body"])
            if (
                hashlib.sha256(raw).hexdigest(),
                len(raw),
                len(raw.decode("utf-8")),
            ) != (row["sha256"], row["text_bytes"], row["text_chars"]):
                raise ValueError(f'Stored body is corrupt: {row["paper_id"]}')
        for query in [
            "SELECT id FROM papers EXCEPT SELECT rowid FROM search",
            "SELECT rowid FROM search EXCEPT SELECT id FROM papers",
        ]:
            if db.execute(query + " LIMIT 1").fetchone():
                raise ValueError("FTS paper IDs differ from library")
        if text_only:
            verify_sqlite(database)
            return report | {
                "verified_complete": True,
                "checks": [
                    "source hashes and metadata",
                    "every stored body",
                    "FTS IDs and integrity",
                ],
            }
        config = json.loads((folder / "config.json").read_text())
        model = load_model(root)
        required = {
            "model": MODEL,
            "model_revision": MODEL_REVISION,
            "dimension": model.dim,
            "dataset_revision": manifest["revision"],
            "window_chars": WINDOW,
            "overlap_chars": OVERLAP,
            "max_tokens": None,
            "format": "float16-normalized",
            "prefix": "title+newline",
            "schema": 1,
        }
        if config != required or published["config"] != config:
            raise ValueError("Model/chunker/index provenance mismatch")
        paper_cursor = iter(
            db.execute("SELECT id,text_chars FROM papers ORDER BY id")
        )
        expected_ids = (
            vector_id(row["id"], i)
            for row in paper_cursor
            for i in range(passage_count(row["text_chars"]))
        )
        # Do not retain one mmap/file descriptor per block. The full corpus
        # exceeds the NAS descriptor limit; one compact ID array is sufficient.
        expected_array = np.empty(passages, dtype=np.int64)
        offset = 0
        last = 0
        for block, state in zip(blocks, states):
            if state["after_id"] != last:
                raise ValueError("Noncontiguous embedding blocks")
            last = state["last_id"]
            for name in ["vectors.npy", "ids.npy"]:
                if file_hash(block / name) != state["sha256"][name]:
                    raise ValueError(
                        f"Embedding checksum mismatch: {block / name}"
                    )
            ids = np.load(block / "ids.npy", mmap_mode="r", allow_pickle=False)
            vectors = np.load(
                block / "vectors.npy", mmap_mode="r", allow_pickle=False
            )
            if vectors.shape != (
                state["passages"],
                model.dim,
            ) or ids.shape != (state["passages"],):
                raise ValueError("Embedding array shape mismatch")
            paper_ids = ids >> 32
            if (
                not len(ids)
                or int(paper_ids[-1]) != state["last_id"]
                or int(paper_ids[0]) <= state["after_id"]
                or len(np.unique(paper_ids)) != state["papers"]
            ):
                raise ValueError(
                    "Embedding block paper boundaries differ from its vectors"
                )
            for packed in ids:
                if next(expected_ids, None) != int(packed):
                    raise ValueError(
                        "Missing, duplicated or incorrect paper passage"
                    )
            for start in range(0, len(vectors), 16384):
                batch = vectors[start : start + 16384].astype(np.float32)
                norms = np.linalg.norm(batch, axis=1)
                if not np.isfinite(batch).all() or not np.all(
                    (norms == 0) | (abs(norms - 1) < 0.002)
                ):
                    raise ValueError("Invalid passage vectors")
            expected_array[offset : offset + len(ids)] = ids
            offset += len(ids)
        if next(expected_ids, None) is not None:
            raise ValueError("Unencoded paper passages remain")
        if published.get("blocks") != [
            [s["last_id"], b.name] for b, s in zip(blocks, states)
        ]:
            raise ValueError(
                "Published ANN block routing differs "
                "from verified vector blocks"
            )
        path = folder / published["path"]
        if path.parent != folder or file_hash(path) != published["sha256"]:
            raise ValueError("Published ANN checksum/path mismatch")
        index = faiss.read_index(
            str(path), faiss.IO_FLAG_MMAP | faiss.IO_FLAG_READ_ONLY
        )
        actual_ids = index_ids(index)
        actual_ids.sort()
        if (
            index.d != model.dim
            or index.ntotal != passages
            or not np.array_equal(actual_ids, expected_array)
        ):
            raise ValueError(
                "Published ANN passage IDs differ from verified source coverage"
            )
        verify_sqlite(database)
    return report | {
        "verified_complete": True,
        "checks": [
            "source hashes and metadata",
            "every stored body",
            "FTS IDs and integrity",
            "model identity",
            "every expected passage",
            "vectors and ANN IDs",
        ],
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("--full", action="store_true")
    parser.add_argument(
        "--text-only",
        action="store_true",
        help="Verify source, stored text and FTS without requiring embeddings",
    )
    parser.add_argument(
        "--database", type=Path, help="Audit a separate serving database"
    )
    args = parser.parse_args()
    result = audit(
        args.root, args.full, text_only=args.text_only, database=args.database
    )
    print(json.dumps(result, indent=2))
    raise SystemExit(2 if args.full and not result["verified_complete"] else 0)
