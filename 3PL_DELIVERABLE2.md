# Deliverable 2: CSV and local file layer

Implemented against `3PL-Pipeline-Prompt (1).md`. These modules provide file handling for the future runner. They do not activate the draft rule engine, write to Linnworks, send emails, or schedule dispatch.

## Implementation

- `lwsync/pipeline/csv_io.py`: validates the 56-column header by position, including both `Item Number` columns; keeps raw records and cell spans. UTF-8 BOM, quoting, escaped quotes, embedded newlines, CRLF/LF/CR, and a final record without a newline are preserved. Numeric order IDs and quantities must be positive int32 values. Errors omit customer field values.
- `CsvRecord.replace({column_index: value})`: edits only selected fields. Existing quoted fields remain quoted; unchanged field bytes remain intact. A replacement requiring quotes in a previously unquoted field is rejected for review instead of silently changing the quoting contract.
- `CsvDocument.orders()`: groups noncontiguous rows by numeric order identity without deduplicating lines. `render()` preserves the original header and relative order of the supplied rows. The rule engine must choose entire orders together.
- `lwsync/pipeline/file_settings.py`: explicit, validated regional folder settings. Held/error paths have no guessed defaults. Overlapping folders and lock files inside data folders are rejected. Loading settings creates no directories.
- `lwsync/pipeline/files.py`: AJC/CS filename classification, stable pickup/read, mutation checks, Linux `flock`, temporary-file publication, date archives/error moves, and unchanged CS passthrough with a warning.
- `publish_csv()`: publishes selected rows under the caller's original input basename. An empty selected subset produces no output, including for an all-held file.

## Deployment requirements and limits

Input must be unchanged for at least 60 seconds. Regular files only are accepted; Linux reads use `O_NOFOLLOW` and nonblocking open to protect against symlink/FIFO replacement. File identity, size and timestamps are compared before/after reading and before an input move. Stability is a heuristic: an uploader pausing longer than 60 seconds can still appear complete. Prefer an uploader completion rename when available.

Provision input/output/staging/archive/held/error folders using the agreed group and setgid directories. Set process umask `0007`. Staging must be outside `orders` and on the same filesystem. New published files are explicitly mode `0660` with the destination directory's group. The process needs permission to assign that group.

Publication writes and fsyncs a temporary file, then performs atomic no-replacement rename. Linux uses `renameat2(RENAME_NOREPLACE)` and fails closed if unsupported; Windows uses its exclusive `rename` for local development. Both directories are fsynced on Linux. Output collisions preserve the existing destination; archives/error moves also refuse overwrites. Cross-filesystem moves fail rather than using a non-atomic copy.

The shared Linux lock file must be writable by the service account and identical for every process reading production inputs. The lock file is not deleted when released. Production locking deliberately rejects Windows execution.

## Integration order for later deliverables

1. Load `FileSettings.from_env("US")` or `("CA")` after normal environment loading.
2. Acquire `run_lock(settings.lock_file)` for the complete run, including both regions.
3. Use `pickup_files()`, then `read_stable()` for each candidate. For AJC files, call `parse_csv(snapshot.data)` before any external action. The future runner handles malformed input by moving its unchanged snapshot to the error folder and recording an alert/outcome.
4. Feed validated rows to the corrected quantity-threshold engine in Deliverable 3. Do not use the existing stock-threshold draft for dispatch.
5. Persist claims/operation progress in Deliverable 4 before external writes or publication. Only then invoke `publish_csv()` or `passthrough_cs()`; an atomic rename alone is not duplicate-dispatch protection.
6. After durable output and held-order outcomes, call `move_input(..., archive_date=...)`. Archive failure must not cause another dispatch on rerun. The future runner owns region/file failure isolation, alerts, dry-run and shadow mode.

After rename, GI may already have collected the output. Never blindly republish after a missing destination, archive failure, crash or uncertain fsync result. Reconcile against durable tracking first. This layer does not make a filesystem rename and database commit one transaction.

## Verification

Use Python 3.12 with `requirements.txt` installed. On Windows:

```powershell
.venv\Scripts\python.exe tests/run_offline.py --pipeline-only
```

On the Linux deployment/test host:

```bash
python tests/run_offline.py --pipeline-only
```

The runner disables dotenv loading and blocks real network/database calls. Tests use temporary folders and invented data. The focused flag does not run or hide failures in the default full offline suite.

Coverage includes byte round-trips, field edits, duplicate headers, malformed UTF-8/CSV, invalid quantities, full-file validation, order grouping, upload mutation, recent/foreign files, collisions, rename failures, cross-filesystem rejection, archives/error moves, CS warnings, selected/empty output, settings isolation, and Linux lock/symlink/permissions checks.

Windows skips Linux-only checks. Run them on Linux with the actual filesystem and service account before deployment. The rule engine, API operations, durable idempotency and production orchestration remain separate deliverables.

Validation on November 16, 2026: **48 passed, 3 skipped** in the focused Windows suite. The skipped tests exercise Linux flock, symlink rejection and group permissions. A previous full offline run also reported two failures in `test_orders_v2.py::test_empty_success_advances_cursor` (US/CA): the order/refund test path attempted a database call blocked by offline isolation. Those files were not changed for Deliverable 2; the full suite is not claimed green.
