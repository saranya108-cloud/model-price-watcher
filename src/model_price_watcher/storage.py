"""Synchronous SQLite persistence for accepted catalog snapshots.

Every stored snapshot is a complete successful catalog, including a valid empty
catalog. USD per million tokens is the monetary unit established by Tranche A;
rows describe catalog offerings, not provider endpoints. This module does not
persist pending or failed attempts.

Callers must pass a complete, successfully accepted catalog. When using
Tranche A, call write_snapshot only if ParseResult.accepted is True. A rejected
ParseResult.observations tuple is also empty; an empty sequence alone cannot
establish acceptance.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import (
    MAX_EMAX,
    MAX_PREC,
    MIN_EMIN,
    Context,
    Decimal,
    DecimalException,
    InvalidOperation,
    localcontext,
)
from pathlib import Path
from typing import Any

from model_price_watcher.models import CatalogObservation, SourceMetadata


SCHEMA_VERSION = 1
MAX_JSON_DEPTH = 64

_SNAPSHOT_COLUMNS = frozenset({
    "id", "provider", "started_at", "completed_at",
    "source_url", "source_metadata_json",
})
_OBSERVATION_COLUMNS = frozenset({
    "snapshot_id", "offering_id", "observed_at", "input_per_million",
    "output_per_million", "conditions_json", "source_record_json",
})
_TIMESTAMP = re.compile(
    r"([0-9]{4})-([0-9]{2})-([0-9]{2})"
    r"T([0-9]{2}):([0-9]{2}):([0-9]{2})\.([0-9]{6})Z"
)
_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE snapshots (
        id INTEGER PRIMARY KEY,
        provider TEXT NOT NULL CHECK (length(trim(provider)) > 0),
        started_at TEXT NOT NULL,
        completed_at TEXT NOT NULL,
        source_url TEXT NOT NULL CHECK (length(source_url) > 0),
        source_metadata_json TEXT NOT NULL,
        CHECK (completed_at >= started_at)
    )
    """,
    """
    CREATE TABLE observations (
        snapshot_id INTEGER NOT NULL,
        offering_id TEXT NOT NULL CHECK (length(trim(offering_id)) > 0),
        observed_at TEXT NOT NULL,
        input_per_million TEXT,
        output_per_million TEXT,
        conditions_json TEXT NOT NULL,
        source_record_json TEXT NOT NULL,
        PRIMARY KEY (snapshot_id, offering_id),
        FOREIGN KEY (snapshot_id) REFERENCES snapshots(id)
            ON UPDATE RESTRICT
            ON DELETE RESTRICT
    )
    """,
    """
    CREATE INDEX snapshots_latest_idx
        ON snapshots(provider, completed_at DESC, id DESC)
    """,
    """
    CREATE INDEX observations_offering_history_idx
        ON observations(offering_id, snapshot_id)
    """,
)


class StorageError(RuntimeError):
    """Unsupported or malformed persistent storage state."""


@dataclass(frozen=True)
class SnapshotRecord:
    """One successfully persisted catalog snapshot."""

    id: int
    provider: str
    started_at: datetime
    completed_at: datetime
    source: SourceMetadata


@dataclass(frozen=True)
class ObservationRecord:
    """One offering row from a persisted snapshot.

    Nested evidence dictionaries are detached copies, not immutable mappings.
    """

    snapshot_id: int
    offering_id: str
    observed_at: datetime
    input_per_million: Decimal | None
    output_per_million: Decimal | None
    conditions: dict[str, Any]
    source_record: dict[str, Any]


def open_database(path: str | Path) -> sqlite3.Connection:
    """Open a database, initializing schema version 1 when the file is empty.

    Connections must come from this function. Callers close them with
    connection.close(). Close/reopen is required; this is not a power-loss
    guarantee. Existing non-internal schema objects, including views and
    triggers, prevent adoption of an unversioned database. Version 1 receives
    a bounded tables-and-columns check, not a general schema validator.
    """
    connection = sqlite3.connect(os.fspath(path), isolation_level=None)
    try:
        connection.row_factory = sqlite3.Row
        _enable_foreign_keys(connection)
        version = _user_version(connection)
        if version == SCHEMA_VERSION:
            _verify_version_1(connection)
        elif version == 0:
            _initialize_schema(connection)
        else:
            raise StorageError(f"Unsupported schema version {version}")
        return connection
    except BaseException as error:
        _close_failed_open(connection, error)


def write_snapshot(
    connection: sqlite3.Connection,
    *,
    provider: str,
    started_at: datetime,
    completed_at: datetime,
    source: SourceMetadata,
    observations: Sequence[CatalogObservation],
) -> SnapshotRecord:
    """Persist one complete successful catalog snapshot.

    IMPORTANT: write_snapshot accepts a complete, successfully accepted catalog.
    When using Tranche A, call it only if ParseResult.accepted is True. A rejected
    ParseResult.observations tuple is also empty; never treat that empty tuple
    alone as an accepted empty catalog. Storage cannot infer acceptance from an
    empty sequence.

    Inputs are fully validated and serialized before BEGIN IMMEDIATE. Duplicate
    offering IDs are left to the composite primary key so the write rolls back.
    A successful rollback leaves no rows from the attempt.

    If an exception occurs after the write transaction is acquired, this
    function rolls back when the transaction remains active and re-raises the
    original operation exception. If rollback itself fails, that rollback
    failure remains available as the exception context. Connection state is then
    indeterminate, and partial uncommitted work may remain visible on that
    connection. Callers must close or discard that connection and reopen before
    further use. Absence of persisted data cannot be independently guaranteed
    after rollback failure.
    """
    if connection.in_transaction:
        raise RuntimeError(
            "write_snapshot refuses to join a caller-owned transaction"
        )
    _require_foreign_keys(connection)
    started, completed, location, metadata_json, rows = _prepare_write(
        provider, started_at, completed_at, source, observations,
    )
    acquired = False
    try:
        connection.execute("BEGIN IMMEDIATE")
        acquired = True
        cursor = connection.execute(
            "INSERT INTO snapshots ("
            "provider, started_at, completed_at, source_url, source_metadata_json"
            ") VALUES (?, ?, ?, ?, ?)",
            (provider, started, completed, location, metadata_json),
        )
        snapshot_id = cursor.lastrowid
        for row in rows:
            connection.execute(
                "INSERT INTO observations ("
                "snapshot_id, offering_id, observed_at, input_per_million, "
                "output_per_million, conditions_json, source_record_json"
                ") VALUES (?, ?, ?, ?, ?, ?, ?)",
                (snapshot_id, *row),
            )
        record = SnapshotRecord(
            snapshot_id,
            provider,
            _decode_timestamp(started),
            _decode_timestamp(completed),
            SourceMetadata(location, _decode_json(metadata_json)),
        )
        connection.execute("COMMIT")
        return record
    except BaseException as error:
        if acquired:
            try:
                if connection.in_transaction:
                    connection.rollback()
            except BaseException:
                raise error
        raise


def get_latest_successful_snapshot(
    connection: sqlite3.Connection, provider: str,
) -> SnapshotRecord | None:
    """Return the latest snapshot for an exact provider identity, if any."""
    _require_identity(provider, "provider")
    row = connection.execute(
        "SELECT * FROM snapshots "
        "WHERE provider = ? "
        "ORDER BY completed_at DESC, id DESC "
        "LIMIT 1",
        (provider,),
    ).fetchone()
    if row is None:
        return None
    return _snapshot_from_row(row)


def get_successful_snapshots(
    connection: sqlite3.Connection, provider: str,
) -> tuple[SnapshotRecord, ...]:
    """Return every successful snapshot for an exact provider, oldest first."""
    _require_identity(provider, "provider")
    rows = connection.execute(
        "SELECT * FROM snapshots "
        "WHERE provider = ? "
        "ORDER BY completed_at ASC, id ASC",
        (provider,),
    ).fetchall()
    return tuple(_snapshot_from_row(row) for row in rows)


def get_successful_history(
    connection: sqlite3.Connection, provider: str,
) -> tuple[tuple[SnapshotRecord, tuple[ObservationRecord, ...]], ...]:
    """Read ordered history in one query, retaining empty successful snapshots."""
    _require_identity(provider, "provider")
    rows = connection.execute(
        "SELECT snapshots.*, observations.* FROM snapshots "
        "LEFT JOIN observations ON observations.snapshot_id = snapshots.id "
        "WHERE snapshots.provider = ? "
        "ORDER BY snapshots.completed_at ASC, snapshots.id ASC, "
        "observations.offering_id COLLATE BINARY ASC",
        (provider,),
    )
    history = []
    snapshot = None
    observations = []
    for row in rows:
        if snapshot is None or row["id"] != snapshot.id:
            if snapshot is not None:
                history.append((snapshot, tuple(observations)))
            snapshot = _snapshot_from_row(row)
            observations = []
        if row["snapshot_id"] is not None:
            observations.append(_observation_from_row(row))
    if snapshot is not None:
        history.append((snapshot, tuple(observations)))
    return tuple(history)


def get_snapshot_observations(
    connection: sqlite3.Connection, snapshot_id: int,
) -> tuple[ObservationRecord, ...]:
    """Return immutable rows for one snapshot, ordered by offering identity."""
    if isinstance(snapshot_id, bool) or not isinstance(snapshot_id, int):
        raise TypeError("snapshot_id must be an int")
    rows = connection.execute(
        "SELECT * FROM observations "
        "WHERE snapshot_id = ? "
        "ORDER BY offering_id COLLATE BINARY ASC",
        (snapshot_id,),
    ).fetchall()
    return tuple(_observation_from_row(row) for row in rows)


def get_current_observations(
    connection: sqlite3.Connection, provider: str,
) -> tuple[ObservationRecord, ...]:
    """Return rows from the latest snapshot only, never latest-per-offering."""
    latest = get_latest_successful_snapshot(connection, provider)
    if latest is None:
        return ()
    return get_snapshot_observations(connection, latest.id)


def get_offering_history(
    connection: sqlite3.Connection, *, provider: str, offering_id: str,
) -> tuple[ObservationRecord, ...]:
    """Return one offering's rows for one provider, oldest snapshot first."""
    _require_identity(provider, "provider")
    _require_identity(offering_id, "offering_id")
    rows = connection.execute(
        "SELECT observations.* FROM observations "
        "JOIN snapshots ON snapshots.id = observations.snapshot_id "
        "WHERE snapshots.provider = ? AND observations.offering_id = ? "
        "ORDER BY snapshots.completed_at ASC, snapshots.id ASC",
        (provider, offering_id),
    ).fetchall()
    return tuple(_observation_from_row(row) for row in rows)


def _close_failed_open(connection: sqlite3.Connection, error: BaseException) -> None:
    try:
        if connection.in_transaction:
            connection.rollback()
    except BaseException:
        try:
            connection.close()
        except BaseException:
            pass
        raise error
    try:
        connection.close()
    except BaseException:
        raise error
    raise error


def _initialize_schema(connection: sqlite3.Connection) -> None:
    acquired = False
    try:
        connection.execute("BEGIN IMMEDIATE")
        acquired = True
        version = _user_version(connection)
        if version == SCHEMA_VERSION:
            _verify_version_1(connection)
            connection.rollback()
            return
        if version != 0:
            raise StorageError(f"Unsupported schema version {version}")
        if _user_schema_objects(connection):
            raise StorageError("Refusing to adopt an unversioned database")
        for statement in _SCHEMA_STATEMENTS:
            connection.execute(statement)
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION:d}")
        connection.execute("COMMIT")
    except BaseException as error:
        if acquired:
            try:
                if connection.in_transaction:
                    connection.rollback()
            except BaseException:
                raise error
        raise


def _enable_foreign_keys(connection: sqlite3.Connection) -> None:
    connection.execute("PRAGMA foreign_keys = ON")
    _require_foreign_keys(connection)


def _require_foreign_keys(connection: sqlite3.Connection) -> None:
    row = connection.execute("PRAGMA foreign_keys").fetchone()
    if row is None or int(row[0]) != 1:
        raise StorageError("foreign key enforcement is not enabled")


def _user_version(connection: sqlite3.Connection) -> int:
    return int(connection.execute("PRAGMA user_version").fetchone()[0])


def _user_schema_objects(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    return connection.execute(
        "SELECT type, name FROM sqlite_master "
        "WHERE name NOT GLOB 'sqlite_*' "
        "ORDER BY type, name"
    ).fetchall()


def _verify_version_1(connection: sqlite3.Connection) -> None:
    if not _ordinary_table_exists(connection, "snapshots"):
        raise StorageError("version 1 database is missing the snapshots table")
    if not _ordinary_table_exists(connection, "observations"):
        raise StorageError("version 1 database is missing the observations table")
    snapshot_columns = _table_columns(connection, "snapshots")
    if not _SNAPSHOT_COLUMNS <= snapshot_columns:
        raise StorageError("version 1 snapshots table is missing required columns")
    observation_columns = _table_columns(connection, "observations")
    if not _OBSERVATION_COLUMNS <= observation_columns:
        raise StorageError("version 1 observations table is missing required columns")


def _ordinary_table_exists(connection: sqlite3.Connection, name: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (name,),
    ).fetchone()
    return row is not None


def _table_columns(connection: sqlite3.Connection, name: str) -> set[str]:
    return {
        row[1] for row in connection.execute(f'PRAGMA table_info("{name}")')
    }


def _prepare_write(
    provider: str,
    started_at: datetime,
    completed_at: datetime,
    source: SourceMetadata,
    observations: Sequence[CatalogObservation],
):
    if not isinstance(provider, str):
        raise TypeError("provider must be a string")
    if not provider.strip():
        raise ValueError("provider must be nonblank")
    _require_sqlite_text(provider, "provider")
    if not isinstance(source, SourceMetadata):
        raise TypeError("source must be SourceMetadata")
    if not isinstance(source.location, str):
        raise TypeError("source location must be a string")
    if source.location == "":
        raise ValueError("source location must be nonempty")
    _require_sqlite_text(source.location, "source location")
    started = _encode_timestamp(started_at)
    completed = _encode_timestamp(completed_at)
    if completed < started:
        raise ValueError("completed_at must be at or after started_at")
    metadata_json = _encode_json(source.metadata)
    if isinstance(observations, (str, bytes)) or not isinstance(observations, Sequence):
        raise TypeError("observations must be a sequence")
    rows = []
    observed_at = set()
    for item in observations:
        if not isinstance(item, CatalogObservation):
            raise TypeError("observations must contain CatalogObservation values")
        if not isinstance(item.offering_id, str):
            raise TypeError("offering_id must be a string")
        if not item.offering_id.strip():
            raise ValueError("offering_id must be nonblank")
        _require_sqlite_text(item.offering_id, "offering_id")
        if item.provider != provider:
            raise ValueError("observation provider must equal snapshot provider")
        if not isinstance(item.source, SourceMetadata):
            raise TypeError("observation source must be SourceMetadata")
        if item.source.location != source.location:
            raise ValueError("observation source must equal snapshot source")
        if _encode_json(item.source.metadata) != metadata_json:
            raise ValueError("observation source must equal snapshot source")
        observed = _encode_timestamp(item.observed_at)
        if observed < started or observed > completed:
            raise ValueError("observation time must be within the snapshot interval")
        observed_at.add(observed)
        rows.append((
            item.offering_id,
            observed,
            _encode_money(item.input_usd_per_million),
            _encode_money(item.output_usd_per_million),
            _encode_json(item.unsupported_pricing),
            _encode_json(item.raw_offering),
        ))
    if rows and len(observed_at) != 1:
        raise ValueError("observations must share one UTC observation time")
    return started, completed, source.location, metadata_json, rows


def _require_identity(value: str, name: str) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")


def _require_sqlite_text(value: str, name: str) -> None:
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise ValueError(f"{name} is not representable as SQLite TEXT") from error
    if b"\x00" in encoded:
        raise ValueError(f"{name} is not representable as SQLite TEXT")


def _snapshot_from_row(row: sqlite3.Row) -> SnapshotRecord:
    if not isinstance(row["id"], int) or isinstance(row["id"], bool):
        raise StorageError("malformed snapshot id")
    if not isinstance(row["provider"], str):
        raise StorageError("malformed snapshot provider")
    if not isinstance(row["source_url"], str) or row["source_url"] == "":
        raise StorageError("malformed snapshot source location")
    started = _decode_timestamp(row["started_at"])
    completed = _decode_timestamp(row["completed_at"])
    if completed < started:
        raise StorageError("malformed snapshot interval")
    return SnapshotRecord(
        row["id"],
        row["provider"],
        started,
        completed,
        SourceMetadata(row["source_url"], _decode_json(row["source_metadata_json"])),
    )


def _observation_from_row(row: sqlite3.Row) -> ObservationRecord:
    if not isinstance(row["snapshot_id"], int) or isinstance(row["snapshot_id"], bool):
        raise StorageError("malformed observation snapshot id")
    if not isinstance(row["offering_id"], str):
        raise StorageError("malformed offering_id")
    return ObservationRecord(
        row["snapshot_id"],
        row["offering_id"],
        _decode_timestamp(row["observed_at"]),
        _decode_money(row["input_per_million"]),
        _decode_money(row["output_per_million"]),
        _decode_json(row["conditions_json"]),
        _decode_json(row["source_record_json"]),
    )


def _codec_context() -> Context:
    context = Context(
        prec=MAX_PREC,
        Emax=MAX_EMAX,
        Emin=MIN_EMIN,
        capitals=0,
        clamp=0,
    )
    context.traps[InvalidOperation] = True
    context.clear_flags()
    return context


def _format_numeric(value: Decimal) -> str:
    sign, digits, exponent = value.as_tuple()
    if not isinstance(exponent, int):
        raise ValueError("numeric value must be finite")
    if all(digit == 0 for digit in digits):
        return "0"
    digits = list(digits)
    while digits[-1] == 0:
        digits.pop()
        exponent += 1
    scientific = exponent + len(digits) - 1
    if len(digits) == 1:
        coefficient = str(digits[0])
    else:
        coefficient = str(digits[0]) + "." + "".join(str(digit) for digit in digits[1:])
    prefix = "-" if sign else ""
    return f"{prefix}{coefficient}e{scientific}"


def _encode_money(value: Decimal | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, Decimal):
        raise TypeError("monetary value must be Decimal or None")
    if not value.is_finite():
        raise ValueError("monetary value must be finite")
    if value < 0:
        raise ValueError("monetary value must be non-negative")
    return _format_numeric(value)


def _decode_money(value: Any) -> Decimal | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise StorageError("monetary value must be TEXT or NULL")
    try:
        with localcontext(_codec_context()) as context:
            context.traps[InvalidOperation] = True
            amount = Decimal(value)
            if not amount.is_finite() or amount < 0:
                raise StorageError("malformed monetary value")
            encoded = _format_numeric(amount)
    except StorageError:
        raise
    except (DecimalException, ValueError, ArithmeticError, OverflowError) as error:
        raise StorageError("malformed monetary value") from error
    if encoded != value:
        raise StorageError("noncanonical monetary value")
    return amount


def _encode_json(value: Any) -> str:
    if not isinstance(value, dict):
        raise TypeError("JSON evidence root must be a dictionary")
    if _json_object_depth(value) > MAX_JSON_DEPTH:
        raise ValueError("JSON evidence exceeds maximum nesting depth")
    with localcontext(_codec_context()):
        return _encode_json_value(value, set())


def _json_object_depth(value: Any) -> int:
    if not isinstance(value, (dict, list)):
        return 0
    maximum = 1
    stack = [(value, 1, {id(value)})]
    while stack:
        node, depth, path = stack.pop()
        if depth > maximum:
            maximum = depth
            if maximum > MAX_JSON_DEPTH:
                return maximum
        children = node.values() if isinstance(node, dict) else node
        for child in children:
            if isinstance(child, (dict, list)):
                identity = id(child)
                if identity in path:
                    continue
                stack.append((child, depth + 1, path | {identity}))
    return maximum


def _json_text_depth(text: str) -> int:
    depth = 0
    maximum = 0
    in_string = False
    escape = False
    for char in text:
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in "{[":
            depth += 1
            if depth > maximum:
                maximum = depth
        elif char in "}]":
            depth -= 1
    return maximum


def _encode_json_value(value: Any, stack: set[int]) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return _format_numeric(Decimal(value))
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("JSON numbers must be finite")
        return _format_numeric(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=True)
    if isinstance(value, float):
        raise TypeError("JSON evidence cannot contain float")
    if isinstance(value, dict):
        identity = id(value)
        if identity in stack:
            raise ValueError("JSON evidence cannot contain cycles")
        for key in value:
            if not isinstance(key, str):
                raise TypeError("JSON object keys must be strings")
        stack.add(identity)
        try:
            members = [
                json.dumps(key, ensure_ascii=True)
                + ":"
                + _encode_json_value(value[key], stack)
                for key in sorted(value)
            ]
        finally:
            stack.remove(identity)
        return "{" + ",".join(members) + "}"
    if isinstance(value, list):
        identity = id(value)
        if identity in stack:
            raise ValueError("JSON evidence cannot contain cycles")
        stack.add(identity)
        try:
            members = [_encode_json_value(item, stack) for item in value]
        finally:
            stack.remove(identity)
        return "[" + ",".join(members) + "]"
    raise TypeError(f"unsupported JSON evidence type: {type(value).__name__}")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_constant(value):
    raise ValueError(f"Non-JSON numeric constant: {value}")


def _decode_json(text: Any) -> dict[str, Any]:
    if not isinstance(text, str):
        raise StorageError("JSON evidence must be TEXT")
    if _json_text_depth(text) > MAX_JSON_DEPTH:
        raise StorageError("JSON evidence exceeds maximum nesting depth")
    try:
        with localcontext(_codec_context()) as context:
            context.traps[InvalidOperation] = True
            value = json.loads(
                text,
                parse_int=Decimal,
                parse_float=Decimal,
                object_pairs_hook=_unique_object,
                parse_constant=_reject_constant,
            )
    except (DecimalException, ValueError, TypeError, ArithmeticError, OverflowError, RecursionError) as error:
        raise StorageError("malformed JSON evidence") from error
    if not isinstance(value, dict):
        raise StorageError("JSON evidence root must be an object")
    try:
        encoded = _encode_json(value)
    except (TypeError, ValueError, RecursionError, DecimalException, ArithmeticError) as error:
        raise StorageError("malformed JSON evidence") from error
    if encoded != text:
        raise StorageError("noncanonical JSON evidence")
    return value


def _encode_timestamp(value: datetime) -> str:
    if not isinstance(value, datetime):
        raise TypeError("timestamp must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    try:
        utc = value.astimezone(timezone.utc)
    except (OverflowError, OSError, ValueError) as error:
        raise ValueError("timestamp is not representable in UTC") from error
    year = utc.year
    if year < 1 or year > 9999:
        raise ValueError("timestamp year must be four digits")
    return (
        f"{year:04d}-{utc.month:02d}-{utc.day:02d}"
        f"T{utc.hour:02d}:{utc.minute:02d}:{utc.second:02d}"
        f".{utc.microsecond:06d}Z"
    )


def _decode_timestamp(value: Any) -> datetime:
    if not isinstance(value, str):
        raise StorageError("malformed timestamp")
    match = _TIMESTAMP.fullmatch(value)
    if match is None:
        raise StorageError("malformed timestamp")
    year, month, day, hour, minute, second, microsecond = (int(part) for part in match.groups())
    try:
        decoded = datetime(
            year, month, day, hour, minute, second, microsecond, tzinfo=timezone.utc,
        )
    except ValueError as error:
        raise StorageError("malformed timestamp") from error
    if _encode_timestamp(decoded) != value:
        raise StorageError("noncanonical timestamp")
    return decoded
