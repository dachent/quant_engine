"""Run-local SQLite persistence for extraction state and canonical rows.

The database is deliberately a single-writer store.  Each public mutation uses
one short ``BEGIN IMMEDIATE`` transaction and SQLite's rollback journal.  This
module never stores request headers or credential material.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence

from .util import canonical_json, sha256_text, utc_now_ms


COVERAGE_STATUSES = frozenset(
    {
        "complete_to_observed_boundary",
        "partial",
        "unsupported",
        "permission_denied",
        "documented_unverified",
        "excluded_mutation",
        "not_applicable",
    }
)
RUN_STATUSES = frozenset({"RUNNING", "COMPLETED", "FAILED"})


class StorageError(RuntimeError):
    """Base class for persistence contract violations."""


class StorageIntegrityError(StorageError):
    """Raised when SQLite reports corruption or broken foreign keys."""


class ImmutableRunError(StorageError):
    """Raised when a completed or failed run would be changed."""


@dataclass(frozen=True)
class RowWriteStats:
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0


def _row_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return None if row is None else dict(row)


class RunStorage:
    """Own the durable state database for exactly one output run directory."""

    def __init__(self, database: str | Path) -> None:
        self.path = Path(database)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(
            self.path,
            timeout=30.0,
            isolation_level=None,
            check_same_thread=True,
        )
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys=ON")
        self._connection.execute("PRAGMA busy_timeout=30000")
        journal_mode = self._connection.execute(
            "PRAGMA journal_mode=DELETE"
        ).fetchone()[0]
        if str(journal_mode).lower() != "delete":
            self._connection.close()
            raise StorageError(f"SQLite rollback journal unavailable: {journal_mode!r}")
        self._connection.execute("PRAGMA synchronous=FULL")
        self._create_schema()

    def __enter__(self) -> "RunStorage":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    @property
    def connection(self) -> sqlite3.Connection:
        """The connection, exposed for read-only reporting queries."""
        return self._connection

    def close(self) -> None:
        self._connection.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Open one short, non-nestable single-writer transaction."""
        if self._connection.in_transaction:
            raise StorageError("nested transactions are not supported")
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            yield self._connection
        except BaseException:
            self._connection.rollback()
            raise
        else:
            self._connection.commit()

    def _create_schema(self) -> None:
        # executescript owns its transaction; it is used only during startup.
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS runs (
                run_id TEXT PRIMARY KEY,
                mode TEXT NOT NULL CHECK (mode IN ('full', 'incremental')),
                started_at_ms INTEGER NOT NULL,
                run_end_ms INTEGER NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('RUNNING', 'COMPLETED', 'FAILED')),
                completed_at_ms INTEGER,
                extractor_hash TEXT NOT NULL DEFAULT '',
                config_hash TEXT NOT NULL DEFAULT '',
                metadata_json TEXT NOT NULL DEFAULT '{}',
                failure_json TEXT
            );

            CREATE TABLE IF NOT EXISTS requests (
                run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE RESTRICT,
                request_id TEXT NOT NULL,
                request_hash TEXT NOT NULL,
                method TEXT NOT NULL CHECK (method = 'GET'),
                endpoint TEXT NOT NULL,
                params_json TEXT NOT NULL,
                status_code INTEGER NOT NULL,
                elapsed_ms INTEGER NOT NULL CHECK (elapsed_ms >= 0),
                row_count INTEGER NOT NULL CHECK (row_count >= 0),
                response_sha256 TEXT NOT NULL,
                raw_relpath TEXT NOT NULL,
                envelope_relpath TEXT NOT NULL,
                requested_at_ms INTEGER NOT NULL,
                observed_bounds_json TEXT,
                terminal_reason TEXT,
                PRIMARY KEY (run_id, request_id),
                UNIQUE (run_id, request_hash)
            );
            CREATE INDEX IF NOT EXISTS requests_endpoint_idx
                ON requests(run_id, endpoint, requested_at_ms);

            CREATE TABLE IF NOT EXISTS coverage (
                run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE RESTRICT,
                endpoint TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN (
                    'complete_to_observed_boundary', 'partial', 'unsupported',
                    'permission_denied', 'documented_unverified',
                    'excluded_mutation', 'not_applicable'
                )),
                requested_bounds_json TEXT,
                observed_bounds_json TEXT,
                terminal_reason TEXT,
                details_json TEXT NOT NULL DEFAULT '{}',
                updated_at_ms INTEGER NOT NULL,
                PRIMARY KEY (run_id, endpoint)
            );

            CREATE TABLE IF NOT EXISTS canonical_rows (
                run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE RESTRICT,
                dataset TEXT NOT NULL,
                stable_key TEXT NOT NULL,
                row_json TEXT NOT NULL,
                row_sha256 TEXT NOT NULL,
                source_request_id TEXT,
                first_seen_at_ms INTEGER NOT NULL,
                last_seen_at_ms INTEGER NOT NULL,
                PRIMARY KEY (run_id, dataset, stable_key),
                FOREIGN KEY (run_id, source_request_id)
                    REFERENCES requests(run_id, request_id) ON DELETE RESTRICT
            );
            CREATE INDEX IF NOT EXISTS canonical_dataset_idx
                ON canonical_rows(run_id, dataset, stable_key);

            CREATE TABLE IF NOT EXISTS checkpoints (
                run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE RESTRICT,
                scope TEXT NOT NULL,
                checkpoint_key TEXT NOT NULL,
                value_json TEXT NOT NULL,
                done INTEGER NOT NULL DEFAULT 0 CHECK (done IN (0, 1)),
                updated_at_ms INTEGER NOT NULL,
                PRIMARY KEY (run_id, scope, checkpoint_key)
            );
            """
        )

    def integrity_check(self) -> dict[str, Any]:
        quick = [str(row[0]) for row in self._connection.execute("PRAGMA quick_check")]
        foreign_keys = [
            tuple(row) for row in self._connection.execute("PRAGMA foreign_key_check")
        ]
        ok = quick == ["ok"] and not foreign_keys
        return {"ok": ok, "quick_check": quick, "foreign_key_violations": foreign_keys}

    def assert_integrity(self) -> None:
        result = self.integrity_check()
        if not result["ok"]:
            raise StorageIntegrityError(canonical_json(result))

    def create_run(
        self,
        run_id: str,
        *,
        mode: str,
        run_end_ms: int,
        started_at_ms: int | None = None,
        extractor_hash: str = "",
        config_hash: str = "",
        metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create a run, or verify that an existing run has the same identity."""
        if mode not in {"full", "incremental"}:
            raise ValueError("mode must be 'full' or 'incremental'")
        existing = self.get_run(run_id)
        expected_metadata = canonical_json(dict(metadata or {}))
        if existing is not None:
            identity = (
                existing["mode"],
                existing["run_end_ms"],
                existing["extractor_hash"],
                existing["config_hash"],
                existing["metadata_json"],
            )
            expected = (
                mode,
                int(run_end_ms),
                extractor_hash,
                config_hash,
                expected_metadata,
            )
            if identity != expected:
                raise StorageError(
                    f"run {run_id!r} already exists with different identity"
                )
            return existing
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO runs(
                    run_id, mode, started_at_ms, run_end_ms, status,
                    extractor_hash, config_hash, metadata_json
                ) VALUES (?, ?, ?, ?, 'RUNNING', ?, ?, ?)
                """,
                (
                    run_id,
                    mode,
                    int(started_at_ms if started_at_ms is not None else utc_now_ms()),
                    int(run_end_ms),
                    extractor_hash,
                    config_hash,
                    expected_metadata,
                ),
            )
        return self.get_run(run_id) or {}

    def merge_run_metadata(self, run_id: str, updates: Mapping[str, Any]) -> None:
        """Merge keys into a RUNNING run's metadata (read-modify-write)."""
        self._assert_run_mutable(run_id)
        row = self.get_run(run_id) or {}
        merged = json.loads(str(row.get("metadata_json") or "{}"))
        merged.update(dict(updates))
        with self.transaction() as connection:
            connection.execute(
                "UPDATE runs SET metadata_json=? WHERE run_id=?",
                (canonical_json(merged), run_id),
            )

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        row = self._connection.execute(
            "SELECT * FROM runs WHERE run_id=?", (run_id,)
        ).fetchone()
        return _row_dict(row)

    def _assert_run_mutable(self, run_id: str) -> None:
        row = self.get_run(run_id)
        if row is None:
            raise StorageError(f"unknown run: {run_id}")
        if row["status"] != "RUNNING":
            raise ImmutableRunError(f"run {run_id!r} is {row['status']} and immutable")

    def finish_run(
        self,
        run_id: str,
        *,
        status: str = "COMPLETED",
        failure: Mapping[str, Any] | None = None,
        completed_at_ms: int | None = None,
    ) -> None:
        if status not in {"COMPLETED", "FAILED"}:
            raise ValueError("terminal status must be COMPLETED or FAILED")
        self._assert_run_mutable(run_id)
        if status == "COMPLETED":
            self.assert_integrity()
        failure_json = canonical_json(dict(failure)) if failure is not None else None
        with self.transaction() as connection:
            connection.execute(
                "UPDATE runs SET status=?, completed_at_ms=?, failure_json=? WHERE run_id=?",
                (status, int(completed_at_ms or utc_now_ms()), failure_json, run_id),
            )

    def record_request(self, run_id: str, envelope: Mapping[str, Any]) -> bool:
        """Record a durable raw response envelope; return False on exact replay."""
        self._assert_run_mutable(run_id)
        required = {
            "request_id",
            "request_hash",
            "method",
            "endpoint",
            "safe_params",
            "status_code",
            "elapsed_ms",
            "row_count",
            "response_sha256",
            "raw_relpath",
            "envelope_relpath",
            "requested_at_ms",
        }
        missing = sorted(required.difference(envelope))
        if missing:
            raise ValueError(f"request envelope missing: {', '.join(missing)}")
        existing = self.get_request_by_hash(run_id, str(envelope["request_hash"]))
        if existing is not None:
            checks = {
                "request_id": envelope["request_id"],
                "response_sha256": envelope["response_sha256"],
                "endpoint": envelope["endpoint"],
                "params_json": canonical_json(envelope["safe_params"]),
            }
            if any(str(existing[key]) != str(value) for key, value in checks.items()):
                raise StorageError(
                    "request hash replay does not match the durable record"
                )
            return False
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO requests(
                    run_id, request_id, request_hash, method, endpoint, params_json,
                    status_code, elapsed_ms, row_count, response_sha256, raw_relpath,
                    envelope_relpath, requested_at_ms, observed_bounds_json, terminal_reason
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    str(envelope["request_id"]),
                    str(envelope["request_hash"]),
                    str(envelope["method"]),
                    str(envelope["endpoint"]),
                    canonical_json(envelope["safe_params"]),
                    int(envelope["status_code"]),
                    int(envelope["elapsed_ms"]),
                    int(envelope["row_count"]),
                    str(envelope["response_sha256"]),
                    str(envelope["raw_relpath"]),
                    str(envelope["envelope_relpath"]),
                    int(envelope["requested_at_ms"]),
                    canonical_json(envelope["observed_bounds"])
                    if envelope.get("observed_bounds") is not None
                    else None,
                    str(envelope["terminal_reason"])
                    if envelope.get("terminal_reason") is not None
                    else None,
                ),
            )
        return True

    def get_request_by_hash(
        self, run_id: str, request_hash: str
    ) -> dict[str, Any] | None:
        row = self._connection.execute(
            "SELECT * FROM requests WHERE run_id=? AND request_hash=?",
            (run_id, request_hash),
        ).fetchone()
        return _row_dict(row)

    def iter_requests(self, run_id: str) -> Iterator[dict[str, Any]]:
        cursor = self._connection.execute(
            "SELECT * FROM requests WHERE run_id=? ORDER BY requested_at_ms, request_id",
            (run_id,),
        )
        for row in cursor:
            yield dict(row)

    def set_coverage(
        self,
        run_id: str,
        endpoint: str,
        status: str,
        *,
        requested_bounds: Mapping[str, Any] | None = None,
        observed_bounds: Mapping[str, Any] | None = None,
        terminal_reason: str | None = None,
        details: Mapping[str, Any] | None = None,
        updated_at_ms: int | None = None,
    ) -> None:
        if status not in COVERAGE_STATUSES:
            raise ValueError(f"invalid coverage status: {status}")
        self._assert_run_mutable(run_id)
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO coverage(
                    run_id, endpoint, status, requested_bounds_json,
                    observed_bounds_json, terminal_reason, details_json, updated_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id, endpoint) DO UPDATE SET
                    status=excluded.status,
                    requested_bounds_json=excluded.requested_bounds_json,
                    observed_bounds_json=excluded.observed_bounds_json,
                    terminal_reason=excluded.terminal_reason,
                    details_json=excluded.details_json,
                    updated_at_ms=excluded.updated_at_ms
                """,
                (
                    run_id,
                    endpoint,
                    status,
                    canonical_json(requested_bounds)
                    if requested_bounds is not None
                    else None,
                    canonical_json(observed_bounds)
                    if observed_bounds is not None
                    else None,
                    terminal_reason,
                    canonical_json(details or {}),
                    int(updated_at_ms or utc_now_ms()),
                ),
            )

    def list_coverage(self, run_id: str) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self._connection.execute(
                "SELECT * FROM coverage WHERE run_id=? ORDER BY endpoint", (run_id,)
            )
        ]

    def upsert_canonical_rows(
        self,
        run_id: str,
        dataset: str,
        rows: Iterable[tuple[str, Mapping[str, Any], str | None]],
        *,
        seen_at_ms: int | None = None,
    ) -> RowWriteStats:
        """Idempotently store ``(stable_key, row, source_request_id)`` tuples."""
        self._assert_run_mutable(run_id)
        now = int(seen_at_ms or utc_now_ms())
        inserted = updated = unchanged = 0
        materialized: list[tuple[str, str, str, str | None]] = []
        for stable_key, row, source_request_id in rows:
            row_json = canonical_json(dict(row))
            materialized.append(
                (str(stable_key), row_json, sha256_text(row_json), source_request_id)
            )
        with self.transaction() as connection:
            for stable_key, row_json, digest, source_request_id in materialized:
                existing = connection.execute(
                    """
                    SELECT row_sha256, source_request_id FROM canonical_rows
                    WHERE run_id=? AND dataset=? AND stable_key=?
                    """,
                    (run_id, dataset, stable_key),
                ).fetchone()
                if existing is None:
                    connection.execute(
                        """
                        INSERT INTO canonical_rows(
                            run_id, dataset, stable_key, row_json, row_sha256,
                            source_request_id, first_seen_at_ms, last_seen_at_ms
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            run_id,
                            dataset,
                            stable_key,
                            row_json,
                            digest,
                            source_request_id,
                            now,
                            now,
                        ),
                    )
                    inserted += 1
                elif (
                    existing["row_sha256"] == digest
                    and existing["source_request_id"] == source_request_id
                ):
                    unchanged += 1
                else:
                    connection.execute(
                        """
                        UPDATE canonical_rows
                        SET row_json=?, row_sha256=?, source_request_id=?, last_seen_at_ms=?
                        WHERE run_id=? AND dataset=? AND stable_key=?
                        """,
                        (
                            row_json,
                            digest,
                            source_request_id,
                            now,
                            run_id,
                            dataset,
                            stable_key,
                        ),
                    )
                    updated += 1
        return RowWriteStats(inserted=inserted, updated=updated, unchanged=unchanged)

    def iter_canonical_rows(
        self, run_id: str, dataset: str
    ) -> Iterator[dict[str, Any]]:
        cursor = self._connection.execute(
            """
            SELECT stable_key, row_json, row_sha256, source_request_id,
                   first_seen_at_ms, last_seen_at_ms
            FROM canonical_rows WHERE run_id=? AND dataset=? ORDER BY stable_key
            """,
            (run_id, dataset),
        )
        for row in cursor:
            item = dict(row)
            item["row"] = json.loads(item.pop("row_json"))
            yield item

    def dataset_names(self, run_id: str) -> list[str]:
        return [
            str(row[0])
            for row in self._connection.execute(
                "SELECT DISTINCT dataset FROM canonical_rows WHERE run_id=? ORDER BY dataset",
                (run_id,),
            )
        ]

    def dataset_count(self, run_id: str, dataset: str) -> int:
        return int(
            self._connection.execute(
                "SELECT COUNT(*) FROM canonical_rows WHERE run_id=? AND dataset=?",
                (run_id, dataset),
            ).fetchone()[0]
        )

    def set_checkpoint(
        self,
        run_id: str,
        scope: str,
        checkpoint_key: str,
        value: Any,
        *,
        done: bool = False,
        updated_at_ms: int | None = None,
    ) -> None:
        self._assert_run_mutable(run_id)
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO checkpoints(
                    run_id, scope, checkpoint_key, value_json, done, updated_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id, scope, checkpoint_key) DO UPDATE SET
                    value_json=excluded.value_json,
                    done=excluded.done,
                    updated_at_ms=excluded.updated_at_ms
                """,
                (
                    run_id,
                    scope,
                    checkpoint_key,
                    canonical_json(value),
                    1 if done else 0,
                    int(updated_at_ms or utc_now_ms()),
                ),
            )

    def get_checkpoint(
        self, run_id: str, scope: str, checkpoint_key: str
    ) -> dict[str, Any] | None:
        row = self._connection.execute(
            """
            SELECT value_json, done, updated_at_ms FROM checkpoints
            WHERE run_id=? AND scope=? AND checkpoint_key=?
            """,
            (run_id, scope, checkpoint_key),
        ).fetchone()
        if row is None:
            return None
        return {
            "value": json.loads(row["value_json"]),
            "done": bool(row["done"]),
            "updated_at_ms": int(row["updated_at_ms"]),
        }

    def list_checkpoints(
        self, run_id: str, scope: str | None = None
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM checkpoints WHERE run_id=?"
        parameters: Sequence[Any] = (run_id,)
        if scope is not None:
            sql += " AND scope=?"
            parameters = (run_id, scope)
        sql += " ORDER BY scope, checkpoint_key"
        output: list[dict[str, Any]] = []
        for row in self._connection.execute(sql, parameters):
            item = dict(row)
            item["value"] = json.loads(item.pop("value_json"))
            item["done"] = bool(item["done"])
            output.append(item)
        return output
