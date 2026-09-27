# Library maintenance

An operator's guide to the corpus behind `arxiv-mcp`. Run maintenance on the **database host**, not on each MCP client. Examples use `/srv/media/arxiv` as the canonical library and POSIX shell syntax.

**C serves the library. Python maintains it.** Maintenance is explicit: there is no automatic upstream-refresh, backup or SSD-publication schedule in this package. Never restart a healthy importer just to inspect its progress.

[Initial setup](#initial-setup) · [Updates](#check-and-apply-upstream-updates) · [Verification](#verify-the-corpus) · [Snapshots](#publish-an-ssd-serving-snapshot) · [Recovery](#recovery-and-troubleshooting)

## Choose the operation

| Need | Operation | Writes corpus data? |
| :--- | :--- | :--- |
| Inspect indexed coverage | `arxiv status` | No |
| Check upstream for a new revision | `arxiv-library-refresh ROOT` | No |
| Import a newer revision | `arxiv-library-refresh ROOT --apply` | Yes, changed papers and checkpoints |
| Verify complete text coverage | `arxiv-library-audit ROOT --full --text-only` | Runs SQLite/FTS integrity checks; does not change indexed content |
| Create an SSD serving generation | `arxiv-library-snapshot ROOT DESTINATION` | Writes a separate, fully audited copy |
| Validate an existing snapshot receipt | `arxiv-library-snapshot ROOT DESTINATION --resolve` | No; prints a pinned database path |

The installed `maintenance` package contains the Python environment and required tools. No pip installation is needed. From this checkout, enter a shell containing both packages with:

```sh
nix shell .#default .#maintenance
export ROOT=/srv/media/arxiv
export ARXIV_DATABASE="$ROOT/library.sqlite3"
```

The database host's normal environment may already provide these commands.

## Initial setup

Use these steps for a **new library**, not to update an existing one. Allow space for the downloaded archives, the compressed-text database and full-text index, SQLite's write-ahead log, and any separate serving generation.

```sh
arxiv-library-prepare "$ROOT"
arxiv-library-download "$ROOT"
arxiv-library-ingest "$ROOT"
```

- Preparation installs the pinned source manifest and verifies or downloads the model assets used by optional embedding maintenance. It does not generate embeddings.
- Downloads resume partial files and verify archive size and SHA-256 before publication. Existing verified archives are reused.
- Ingestion commits paper data, FTS entries and checkpoints in batches. Restarting the command after a genuine failure resumes committed progress.
- To import while downloads are still arriving, use `arxiv-library-ingest "$ROOT" --watch` in a separate supervised job.

Do not start a second manual importer if a managed service already owns the job. Completed downloads do **not** mean the database import is complete.

### Managed Terminus deployment

The separate dotfiles deployment supplies systemd user units and SSH wrappers. Inspect them with:

```sh
systemctl --user status arxiv-ingest.service
journalctl --user -u arxiv-download -u arxiv-ingest -n 40 --no-pager
loginctl show-user "$USER" -p Linger
```

Lingering keeps user jobs alive after logout. An inactive job can be a successful completed job; check its exit status and checkpoints before restarting it. Package updates deliberately preserve healthy in-flight workers. The next start uses the updated package.

Host activation and deployment-specific paths are documented in the [dotfiles arXiv guide](https://github.com/willfish/nix/blob/master/docs/arxiv-library.md).

## Check and apply upstream updates

### 1. Check without changing the library

```sh
arxiv-library-refresh "$ROOT"
```

This resolves Hugging Face's moving ref to an immutable commit and compares archive hashes. The report includes current and target revisions, reused archives, changed archives, a download-size upper bound and removed archive paths. It does not download archives or change the database.

Identical revisions mean there is no upstream update to apply. This says nothing about whether the initial import has finished.

### 2. Apply a selected revision

After the initial import completes:

```sh
arxiv-library-refresh "$ROOT" --apply
```

To apply the exact revision from an earlier check, add `--revision COMMIT`, replacing `COMMIT` with the reported target SHA. Otherwise the command resolves the current upstream ref when it starts.

Refresh refuses an active importer, incomplete initial import, or active embedding writer/index publisher. It does not stop those jobs on your behalf.

| Upstream change | What refresh does |
| :--- | :--- |
| Unchanged archive, including a rename | Reuses the SHA-addressed local file; skips body import |
| New or changed archive | Downloads and verifies the whole archive, then examines its papers |
| Unchanged paper text and metadata | Leaves the paper record and FTS entry untouched |
| New paper | Inserts its text, metadata and FTS entry |
| Revised text or metadata | Updates the existing paper, preserving its internal ID; replaces affected FTS tokens |
| Paper absent from the new snapshot | Retains it locally and reports the retained count; it remains searchable |

Archive repacking can require large downloads even when few papers change. Reused files still require local checksum reads. The first refresh builds a resumable ID-only membership catalogue from the original archives, without reading their text columns. Unchanged-paper counters concern examined archives; wholly reused archives are skipped.

Updates are **atomic per batch**, not across the whole corpus. NAS-backed clients see committed changes during refresh. An independently published SSD snapshot remains unchanged until explicitly republished.

### 3. Resume after interruption

```sh
arxiv-library-refresh "$ROOT" --apply
```

Without an explicit revision, an unfinished apply resumes its stored target, even if Hugging Face has advanced again. Committed batches are not reapplied. A newer refresh cannot overtake an unfinished one.

`arxiv status` exposes `refresh_target` in its settings while an apply is unfinished. Its `imports` list describes the initial import, not subsequent refresh progress. Full audits and snapshot publication refuse an unfinished refresh.

Do not edit `manifest.json`, erase refresh checkpoints, or clear `refresh_target` to bypass a failure. Invalid source checksums or conflicting IDs need investigation and repair, not a forced revision change.

### 4. Verify the result

```sh
arxiv-library-audit "$ROOT" --full --text-only
```

Only republish a serving snapshot after verification succeeds. Refresh does not regenerate embeddings or switch a server's database path.

## Verify the corpus

For a cheap indexed-document count and initial checkpoints:

```sh
arxiv status
```

For a source/checkpoint report, or an independent full audit:

```sh
arxiv-library-audit "$ROOT" --text-only
arxiv-library-audit "$ROOT" --full --text-only
```

The first command above is a progress report, not a completeness certificate; it can still involve substantial database reads. The full audit verifies source hashes and metadata, every stored body's checksum and lengths, FTS paper IDs, and SQLite/FTS integrity. It takes a shared writer-exclusion lock and can perform substantial I/O. Exit code **2** means a requested full audit did not establish completeness; corruption can also terminate the command with an error.

Before a refresh, verification uses the shipped source lock. Afterwards it uses the completed refresh provenance stored transactionally in SQLite. Retained papers are counted separately from current-source coverage; their stored bodies and index membership are still checked. This provenance is trusted local state, not protection against a malicious database owner.

To audit another database against the canonical library's selected revision:

```sh
arxiv-library-audit "$ROOT" --full --text-only \
  --database /path/to/serving/library.sqlite3
```

A previous-revision snapshot cannot pass as the current corpus. Keep `--text-only` for normal BM25 maintenance: omitting it additionally requires complete, matching embedding coverage.

## Publish an SSD serving snapshot

A snapshot is a derived serving copy. The NAS library and its source archives remain authoritative.

```sh
export SERVING="$HOME/.local/share/arxiv-serving"
arxiv-library-snapshot "$ROOT" "$SERVING"
```

The publisher refuses an active importer or unfinished refresh. It requires room for the database plus the larger of **32 GiB or 20%** headroom, creates a consistent SQLite backup, performs a full text audit on the staged copy, writes a receipt and atomically updates `current`. The result is a read-only, standalone database with no required WAL sidecars.

Validate the existing receipt and obtain its resolved generation path:

```sh
arxiv-library-snapshot "$ROOT" "$SERVING" --resolve
```

This checks provenance, size, modification time, read-only state and counts; it does not rehash the entire database. The full audit happens during publication. The check must be repeated after a corpus refresh, which makes old receipts stale relative to the canonical revision.

**Publication does not switch MCP traffic.** Before changing a server's `ARXIV_DATABASE`, test representative searches and exact retrieval against the resolved generation. Then update the server's managed configuration and reconnect clients. Use the resolved generation path to keep a session pinned to one database. Do not add a Python receipt check to every native request, and never cut over to a partial benchmark snapshot.

On the managed fleet, update the Terminus Home Manager configuration and activate with `hmswitch`; do not edit its generated wrapper in place.

## Backups and retention

Use SQLite's online backup facility or a consistent filesystem snapshot. **Do not copy only `library.sqlite3` while ingestion or refresh is writing:** committed data can still be in its WAL.

For example, select a new file on your backup volume, with its parent directory already created:

```sh
export BACKUP=/path/on/backup-volume/library.sqlite3
test ! -e "$BACKUP" && \
  nix shell nixpkgs#sqlite -c sqlite3 -readonly "$ROOT/library.sqlite3" \
    ".backup '$BACKUP'"
```

This backs up SQLite only. Preserve the original manifest, source archives, refresh objects, applicable model/embedding artifacts and the package revision as part of the wider backup. A serving copy on the same machine is not a substitute for an independent backup. Audit a restored database before routing clients to it.

| Path | Retention rule |
| :--- | :--- |
| `manifest.json`, `paper_text/` | Original source identity and verified archives; do not overwrite them to upgrade |
| `library.sqlite3` and live WAL/SHM files | SQLite-managed state; never delete sidecars from an active database |
| `refresh/objects/` | Hash-addressed archives used by refresh and audits; no automatic garbage collector |
| `embeddings/block-*` | Preserved vector blocks; referenced indexes can depend on them |
| `generation-*`, `current` in the serving directory | Retain every generation referenced by `current`, server configuration or an active reader |
| `.staging-*` and unreferenced serving generations | Can remain after interruptions; inspect ownership and readers before cleanup |

The snapshot publisher retains old generations. There is deliberately no blanket deletion command: inspect the active database paths and processes before removing any generation or cache object.

## Recovery and troubleshooting

| Symptom | Response |
| :--- | :--- |
| Download/import stopped | Inspect its logs, disk space and exit status; rerun the same command after correcting the failure |
| Lock unavailable | Identify the importer, refresh, audit or publisher holding it; wait rather than deleting the lock file |
| Hash mismatch or corrupt archive | Preserve the suspect artifact, compare the expected hash, and restore or redownload a verified copy; never change the expected hash to fit it |
| Unfinished refresh | Rerun `arxiv-library-refresh ROOT --apply`; do not start a different revision |
| Pending refresh blocks audit/publication | Complete or repair that refresh first; do not clear its marker manually |
| Snapshot receipt stale | Verify the canonical revision and republish; do not forge or edit the receipt |
| Broad search slow, exact retrieval fast | Investigate storage I/O and contention; verify an SSD serving copy rather than assuming smaller responses or a longer timeout solve ranking cost |
| Newly activated code appears unchanged | Existing MCP sessions and protected in-flight jobs can retain their old executable; reconnect clients, but leave healthy import jobs running |

Lock files are advisory-lock rendezvous points. Their presence alone does not indicate a live owner. Deleting one can let another process bypass a lock still held on the old inode.

## Optional embedding artifacts

Routine maintenance is BM25-only. Retained generation, index-building and embedding-audit tools support existing artifacts and explicit future experiments; there is **no semantic/hybrid serving endpoint**. They are not required to repair or update BM25.

Do not start `arxiv-library-embeddings` or `arxiv-library-index` as part of a normal refresh. Existing vectors are tied to the corpus revision, model and chunking configuration. A changed corpus needs a separate generation plan before those artifacts can be treated as current. The managed embedding service and index timer remain opt-in.
