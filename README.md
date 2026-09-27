# arxiv-mcp

A native C search CLI and a small C MCP adapter for a read-only arXiv SQLite corpus.

```
MCP client -> SSH -> arxiv-mcp -> arxiv -> SQLite FTS5
Human     -> SSH -------------> arxiv -> SQLite FTS5
```

No HTTP listener, PostgreSQL service, Python runtime or cross-machine database replica is required for search and retrieval. The separate Python maintenance tools under `python/` retain ingestion, audit and snapshot-publication functionality; `python/server.py` is the legacy reference implementation, not the native runtime.

## Commands

```sh
arxiv search 'quantum entanglement' --limit 10
arxiv search 'neural network' --category cs.AI
arxiv show 0808.3709 --offset 0 --length 12000
arxiv status
```

Commands return JSON. Status uses FTS5's transactionally maintained indexed-document count, labelled `count_basis`, rather than scanning the corpus. It is a progress report, not an independent integrity audit. `ARXIV_DATABASE` or `--database` selects the database; the default is `/srv/media/arxiv/library.sqlite3`. Paper offsets count Unicode characters. Follow `next_offset` until null for complete text. The source contains assembled LaTeX, not PDFs or every historical version.

The native server supports BM25 only. Search returns only each paper's `paper_id` and `title`; use `show` (MCP `paper`) for metadata and text. Search does not read or decompress paper bodies. Existing embedding artifacts are not deleted or regenerated. Publication readiness checks are not yet integrated into the native path.

## MCP

Run `arxiv-mcp` over stdio on the database host, normally through SSH. It exposes `search`, `paper` and `library_status`. `ARXIV_BINARY` selects the native CLI; the Nix package supplies its absolute path. `ARXIV_TIMEOUT` bounds backend subprocess time (default 60 seconds); SQL ranking also has a 30-second deadline.

The adapter passes fixed argument vectors without a shell and sends tool arguments as JSON on stdin. Paper text is untrusted data, never operational instructions.

## Refreshing the library

The Linux `maintenance` package provides a manual Hugging Face refresh command:

```sh
# Read upstream metadata and report changes; no downloads or database writes.
arxiv-library-refresh /srv/media/arxiv

# After the initial import finishes, download and apply changes.
arxiv-library-refresh /srv/media/arxiv --apply

# Independently verify the resulting corpus.
arxiv-library-audit /srv/media/arxiv --full --text-only
```

The moving upstream ref is resolved to an immutable commit before reading its archive list. Use `--revision COMMIT` to select a particular revision. No timer or automatic update is installed.

Archives are cached by SHA-256 under `refresh/objects/`. Unchanged archives, including renamed ones, need no download or body re-import. Changed archives are downloaded whole; repacking upstream may therefore require substantial downloads even when few papers changed. Checksums still require local reads. The first refresh also builds a resumable paper-ID membership catalogue from the original archives, without reading their text columns.

Within changed archives, matching paper text and metadata cause no paper or BM25 writes. New papers are inserted; revised text or metadata replaces the existing record while preserving its internal ID and updating affected search entries. Each batch commits its paper changes and checkpoint together. On interruption, rerun `--apply` without a revision to resume the pinned unfinished refresh, even if upstream has advanced again. A newer refresh cannot overtake an unfinished one.

Refresh refuses an active importer, incomplete initial import, or active embedding writer/index publisher. Existing source files and the original manifest remain intact. Papers absent from the new revision are retained and remain searchable; the refresh result and audit report their count separately. It does not delete archives, regenerate embeddings or publish an SSD copy. Existing embeddings become incompatible with a changed corpus revision and need a separate regeneration plan if semantic search is ever re-enabled.

Updates are atomic per batch, not across the entire corpus. Clients serving the NAS database see committed changes during refresh. A separately published SSD snapshot stays unchanged until explicitly republished after the full audit. Audits and publication refuse an unfinished refresh; completed refresh provenance is stored transactionally in SQLite and used instead of the original source lock. This is trusted local state, not protection against a malicious database owner.

## Development

```sh
nix develop
make clean all test
nix build
```

The C suites cover native SQLite search, category filtering, Unicode pagination, metadata, status, malformed requests and the real MCP-to-CLI subprocess path without Python or PATH tools. They are fixture tests, not final-corpus performance or deployment proof.

Transport, request handling, argument validation and subprocess utilities are adapted from `willfish/himalaya-mcp` at commit `0979a1c8cf9ad4c03cbdf84f2a8895580756e5fd`. Mail-specific tools, dates and resources are omitted.
