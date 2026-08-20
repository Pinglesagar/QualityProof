"""Small SQLite persistence boundary for domain records."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Protocol, TypeVar

from pydantic import BaseModel

from qualityproof.models import AuditEvent, MaterializationManifest

ModelT = TypeVar("ModelT", bound=BaseModel)


class Repository(Protocol):
    def put(self, kind: str, record_id: str, record: BaseModel) -> None: ...

    def get(self, kind: str, record_id: str, model: type[ModelT]) -> ModelT | None: ...

    def list(self, kind: str, model: type[ModelT]) -> tuple[ModelT, ...]: ...

    def append_event(self, event: AuditEvent) -> None: ...

    def clear_kind(self, kind: str) -> None: ...

    def delete(self, kind: str, record_id: str) -> None: ...


class SQLiteRepository:
    """Persist validated models as JSON while keeping SQLite behind an interface."""

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.database_path)
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            yield connection
            connection.commit()
        finally:
            connection.close()

    def initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS records (
                    kind TEXT NOT NULL,
                    record_id TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    PRIMARY KEY (kind, record_id)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS audit_events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE,
                    event_type TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    payload TEXT NOT NULL
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_events_type_time "
                "ON audit_events(event_type, occurred_at)"
            )

    def put(self, kind: str, record_id: str, record: BaseModel) -> None:
        if not kind or not record_id:
            raise ValueError("kind and record_id must not be empty")
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO records (kind, record_id, payload)
                VALUES (?, ?, ?)
                ON CONFLICT(kind, record_id) DO UPDATE SET payload = excluded.payload
                """,
                (kind, record_id, record.model_dump_json()),
            )

    def get(self, kind: str, record_id: str, model: type[ModelT]) -> ModelT | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload FROM records WHERE kind = ? AND record_id = ?",
                (kind, record_id),
            ).fetchone()
        if row is None:
            return None
        return model.model_validate_json(str(row[0]))

    def list(self, kind: str, model: type[ModelT]) -> tuple[ModelT, ...]:
        """List one record kind in stable identifier order."""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT payload FROM records WHERE kind = ? ORDER BY record_id",
                (kind,),
            ).fetchall()
        return tuple(model.model_validate_json(str(row[0])) for row in rows)

    def query(
        self,
        kind: str,
        model: type[ModelT],
        *,
        record_id_prefix: str | None = None,
    ) -> tuple[ModelT, ...]:
        """Query records using a safe, deliberately small filter surface."""
        if record_id_prefix is None:
            return self.list(kind, model)
        escaped = (
            record_id_prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        )
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT payload FROM records
                WHERE kind = ? AND record_id LIKE ? ESCAPE '\\'
                ORDER BY record_id
                """,
                (kind, f"{escaped}%"),
            ).fetchall()
        return tuple(model.model_validate_json(str(row[0])) for row in rows)

    def clear_kind(self, kind: str) -> None:
        """Remove one replaceable record set without touching immutable audit events."""
        if not kind:
            raise ValueError("kind must not be empty")
        with self._connect() as connection:
            connection.execute("DELETE FROM records WHERE kind = ?", (kind,))

    def delete(self, kind: str, record_id: str) -> None:
        """Delete one replaceable record without affecting append-only events."""
        if not kind or not record_id:
            raise ValueError("kind and record_id must not be empty")
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM records WHERE kind = ? AND record_id = ?",
                (kind, record_id),
            )

    def replace_sets(
        self, record_sets: Mapping[str, Iterable[tuple[str, BaseModel]]]
    ) -> None:
        """Atomically replace complete materialized record sets."""
        prepared = {
            kind: tuple(records)
            for kind, records in record_sets.items()
        }
        if any(not kind for kind in prepared):
            raise ValueError("record kind must not be empty")
        with self._connect() as connection:
            for kind, records in prepared.items():
                connection.execute("DELETE FROM records WHERE kind = ?", (kind,))
                connection.executemany(
                    "INSERT INTO records (kind, record_id, payload) VALUES (?, ?, ?)",
                    (
                        (kind, record_id, record.model_dump_json())
                        for record_id, record in records
                    ),
                )

    def replace_manifested_set(
        self,
        scope: str,
        record_kind: str,
        records: Iterable[tuple[str, BaseModel]],
    ) -> MaterializationManifest:
        """Atomically reconcile only records owned by one workflow scope.

        Ownership is enforced, not merely recorded. Records are keyed globally by
        ``(kind, record_id)`` while ownership is tracked per scope, so an earlier
        version let one scope's import overwrite another scope's payload and
        delete records another manifest still claimed — silently, and while the
        other manifest went on listing them. Two imports that never mentioned a
        requirement could therefore remove it from the registry and turn a failing
        coverage gate green.
        """
        if not scope or not record_kind:
            raise ValueError("scope and record_kind must not be empty")
        prepared = tuple(records)
        manifest_id = f"{record_kind}:{scope}"
        manifest = MaterializationManifest(
            scope=scope,
            record_kind=record_kind,
            record_ids=tuple(sorted(record_id for record_id, _ in prepared)),
        )
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload FROM records WHERE kind = ? AND record_id = ?",
                ("materialization_manifest", manifest_id),
            ).fetchone()
            previous = (
                MaterializationManifest.model_validate_json(str(row[0])).record_ids
                if row is not None
                else ()
            )
            owned_elsewhere: dict[str, str] = {}
            for other in connection.execute(
                "SELECT record_id, payload FROM records WHERE kind = ?",
                ("materialization_manifest",),
            ).fetchall():
                if str(other[0]) == manifest_id:
                    continue
                claim = MaterializationManifest.model_validate_json(str(other[1]))
                if claim.record_kind != record_kind:
                    continue
                for record_id in claim.record_ids:
                    owned_elsewhere[record_id] = claim.scope
            collisions = sorted(
                f"{record_id} (owned by scope '{owned_elsewhere[record_id]}')"
                for record_id in manifest.record_ids
                if record_id in owned_elsewhere
            )
            if collisions:
                raise ValueError(
                    f"{record_kind} ids are already owned by another scope and cannot be "
                    f"replaced by '{scope}': {', '.join(collisions)}"
                )
            # Only delete what this scope owned and no other scope claims.
            stale = (set(previous) - set(manifest.record_ids)) - set(owned_elsewhere)
            connection.executemany(
                "DELETE FROM records WHERE kind = ? AND record_id = ?",
                ((record_kind, record_id) for record_id in stale),
            )
            connection.executemany(
                """
                INSERT INTO records (kind, record_id, payload)
                VALUES (?, ?, ?)
                ON CONFLICT(kind, record_id) DO UPDATE SET payload = excluded.payload
                """,
                (
                    (record_kind, record_id, record.model_dump_json())
                    for record_id, record in prepared
                ),
            )
            connection.execute(
                """
                INSERT INTO records (kind, record_id, payload)
                VALUES (?, ?, ?)
                ON CONFLICT(kind, record_id) DO UPDATE SET payload = excluded.payload
                """,
                ("materialization_manifest", manifest_id, manifest.model_dump_json()),
            )
        return manifest

    def append_event(self, event: AuditEvent) -> None:
        """Append an immutable audit event; duplicate IDs are rejected."""
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO audit_events
                    (event_id, event_type, occurred_at, payload)
                VALUES (?, ?, ?, ?)
                """,
                (
                    event.id,
                    event.event_type,
                    event.occurred_at.isoformat(),
                    event.model_dump_json(),
                ),
            )

    def list_events(self, event_type: str | None = None) -> tuple[AuditEvent, ...]:
        with self._connect() as connection:
            if event_type is None:
                rows = connection.execute(
                    "SELECT payload FROM audit_events ORDER BY sequence"
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT payload FROM audit_events
                    WHERE event_type = ?
                    ORDER BY sequence
                    """,
                    (event_type,),
                ).fetchall()
        return tuple(AuditEvent.model_validate_json(str(row[0])) for row in rows)
