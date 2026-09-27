"""Synthetic inputs and direct database assertions, never installed at runtime."""

import hashlib
import zlib

import numpy as np


def paper(
    pid="test/001", text="rarephysicalterm and quantum dynamics λ " * 30
):
    return {
        "paper_id": pid,
        "title": "A paper",
        "abstract": "An abstract",
        "text": text,
        "primary_category": "physics",
        "license": "test",
        "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
    }


def stored_paper(db, paper_id):
    row = dict(
        db.execute(
            "SELECT * FROM papers WHERE paper_id=?", (paper_id,)
        ).fetchone()
    )
    row["text"] = zlib.decompress(row.pop("body")).decode("utf-8")
    return row


def matching_papers(db, expression, category=None):
    # Assert index membership, not the removed Python ranking/serving API.
    return [
        dict(row)
        for row in db.execute(
            "SELECT p.paper_id FROM search JOIN papers p ON p.id=search.rowid "
            "WHERE search MATCH ? AND (? IS NULL OR p.category=?) ORDER BY p.id",
            (expression, category, category),
        )
    ]


def import_state(db):
    return {
        "papers": db.execute("SELECT count(*) FROM papers").fetchone()[0],
        "imports": [
            dict(row)
            for row in db.execute("SELECT * FROM imports ORDER BY shard")
        ],
    }


class ConceptModel:
    dim = 4

    def encode(self, texts, **kwargs):
        assert kwargs["max_length"] is None
        return np.array(
            [
                [
                    int("dogs" in text or "canines" in text),
                    int("galaxies" in text),
                    0.01,
                    0,
                ]
                for text in texts
            ],
            dtype=np.float32,
        )
