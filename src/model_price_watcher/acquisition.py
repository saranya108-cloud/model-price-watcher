"""One explicit authenticated OpenRouter catalog acquisition and snapshot.

No retries, pagination, failed-attempt persistence, or downstream analysis.
Completeness is a response contract, not proof against self-consistent upstream
omissions. completed_at precedes persistence; it is neither commit time nor
an upstream freshness guarantee. Timeout is per socket operation, not per scan.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, DecimalException

from model_price_watcher.models import SourceMetadata
from model_price_watcher.providers.openrouter import parse_catalog
from model_price_watcher.storage import SnapshotRecord, write_snapshot
from model_price_watcher.transport import (
    HttpResponse, HttpTransport, TransportError, validate_limits,
)


OPENROUTER_CATALOG_URL = "https://openrouter.ai/api/v1/models?output_modalities=all"
_CONTRACT = "openrouter-models-v1"
_REASONS = frozenset({
    "network_error", "invalid_framing", "response_too_large", "incomplete_body",
    "unsupported_encoding", "http_status", "unsupported_media_type",
    "invalid_response", "invalid_utf8", "catalog_rejected", "invalid_envelope",
    "count_mismatch", "next_page", "empty_catalog", "credential_exposure",
})


class AcquisitionError(RuntimeError):
    """Sanitized acquisition failure; storage errors are never translated."""

    def __init__(self, reason: str, *, status: int | None = None):
        self.reason = reason if reason in _REASONS else "invalid_response"
        self.status = status if type(status) is int and 100 <= status <= 599 else None
        message = self.reason
        if self.status is not None:
            message += f" (HTTP {self.status})"
        super().__init__(message)


@dataclass(frozen=True)
class ScanResult:
    snapshot: SnapshotRecord
    observation_count: int


@dataclass(frozen=True)
class ScanProfile:
    """Declarative acquisition source: storage identity, fixed URL, ordering texts.

    ``order_messages`` keeps each wrapper's existing clock-ordering ValueError
    texts (observed-before-started, completed-before-observed). Transitional.
    """

    provider: str
    url: str
    order_messages: tuple[str, str]


def run_scan(
    connection: sqlite3.Connection,
    *,
    profile: ScanProfile,
    headers: dict[str, str],
    transport: HttpTransport,
    clock: Callable[[], datetime],
    timeout_seconds: float,
    max_response_bytes: int,
    utc: Callable[[datetime], datetime],
    decode: Callable[[HttpResponse, int], str],
    interpret: Callable[[str, datetime], tuple],
    write: Callable[..., SnapshotRecord],
) -> ScanResult:
    """Shared provider-neutral acquisition sequence; one bounded GET, one write.

    started -> fetch -> decode -> observed -> interpret -> completed -> write.
    Transport failures become AcquisitionError outside the handler, without
    exception context. Exceptions from decode, interpret and write propagate
    unchanged; write is the only persistence and runs last, so any earlier
    failure leaves stored history untouched. Callers own their preconditions.
    """
    started_at = utc(clock())
    failure = None
    try:
        response = transport.get(
            profile.url, headers=headers,
            timeout_seconds=timeout_seconds, max_response_bytes=max_response_bytes,
        )
    except TransportError as error:
        failure = error.reason
    if failure is not None:
        raise AcquisitionError(failure)
    text = decode(response, max_response_bytes)
    observed_at = utc(clock())
    if observed_at < started_at:
        raise ValueError(profile.order_messages[0])
    source, observations, count = interpret(text, observed_at)
    completed_at = utc(clock())
    if completed_at < observed_at:
        raise ValueError(profile.order_messages[1])
    snapshot = write(
        connection, provider=profile.provider, started_at=started_at,
        completed_at=completed_at, source=source, observations=observations,
    )
    return ScanResult(snapshot, count)


OPENROUTER_PROFILE = ScanProfile(
    "openrouter", OPENROUTER_CATALOG_URL,
    ("observation time precedes scan start", "completion time precedes observation time"),
)


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError("clock must return a datetime")
    invalid = False
    try:
        if value.tzinfo is None or value.utcoffset() is None:
            invalid = True
        else:
            converted = value.astimezone(timezone.utc)
    except (OverflowError, OSError, ValueError):
        invalid = True
    if invalid:
        raise ValueError("clock must return a timezone-aware datetime representable in UTC")
    return converted


def _decode_response(response: HttpResponse, maximum: int) -> str:
    if not isinstance(response, HttpResponse) or type(response.status) is not int:
        raise AcquisitionError("invalid_response")
    if response.status != 200:
        raise AcquisitionError("http_status", status=response.status)
    if not isinstance(response.body, bytes):
        raise AcquisitionError("invalid_response")
    if len(response.body) > maximum:
        raise AcquisitionError("response_too_large")
    media = []
    encodings = []
    for header in response.headers:
        if not isinstance(header, tuple) or len(header) != 2 or not all(isinstance(v, str) for v in header):
            raise AcquisitionError("invalid_response")
        name, value = header
        if name.lower() == "content-type":
            media.append(value.split(";", 1)[0].strip().lower())
        elif name.lower() == "content-encoding":
            encodings.append(value.strip().lower())
    if media != ["application/json"]:
        raise AcquisitionError("unsupported_media_type")
    if encodings not in ([], ["identity"]):
        raise AcquisitionError("unsupported_encoding")
    invalid = False
    try:
        text = response.body.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        invalid = True
    if invalid:
        raise AcquisitionError("invalid_utf8")
    return text


def _catalog_count(text: str, observation_count: int, api_key: str) -> int:
    """Only called after strict parser acceptance; never constructs observations."""
    invalid = False
    try:
        envelope = json.loads(text, parse_float=Decimal)
    except (ValueError, DecimalException, OverflowError, RecursionError):
        invalid = True
    if invalid:
        raise AcquisitionError("invalid_envelope")
    count = envelope.get("total_count")
    links = envelope.get("links")
    if type(count) is not int or count < 0 or not isinstance(links, dict):
        raise AcquisitionError("invalid_envelope")
    if links.get("next") is not None:
        raise AcquisitionError("next_page")
    if count != observation_count:
        raise AcquisitionError("count_mismatch")
    if observation_count == 0:
        raise AcquisitionError("empty_catalog")
    # Raw rows are retained by storage. Check decoded string evidence as well
    # as wire text so JSON escapes cannot conceal a reflected credential.
    # This checks only disclosure, never monetary meaning or normalization.
    pending = [envelope["data"]]
    while pending:
        value = pending.pop()
        if isinstance(value, str):
            if api_key in value:
                raise AcquisitionError("credential_exposure")
        elif isinstance(value, dict):
            pending.extend(value.keys())
            pending.extend(value.values())
        elif isinstance(value, list):
            pending.extend(value)
    return count


def scan_openrouter(
    connection: sqlite3.Connection,
    *,
    api_key: str,
    transport: HttpTransport,
    clock: Callable[[], datetime],
    timeout_seconds: float = 30.0,
    max_response_bytes: int = 16 * 1024 * 1024,
) -> ScanResult:
    """Acquire once and return only after the existing writer commits.

    The caller supplies a connection from open_database, a key, a transport, and
    an aware clock. No credential discovery occurs. Caller-owned transactions
    are rejected before clock/network activity. Storage owns its transaction
    and all storage exceptions propagate unchanged, including rollback-failure
    semantics: an indeterminate connection must be closed/discarded by its owner.
    """
    if not isinstance(connection, sqlite3.Connection):
        raise TypeError("connection must be a sqlite3.Connection")
    if connection.in_transaction:
        raise RuntimeError("scan_openrouter refuses a caller-owned transaction")
    if not isinstance(api_key, str):
        raise TypeError("api_key must be a string")
    if not api_key.strip() or any(ord(char) < 32 or ord(char) >= 127 for char in api_key):
        raise ValueError("api_key must be nonblank printable ASCII")
    validate_limits(timeout_seconds, max_response_bytes)
    if not callable(clock):
        raise TypeError("clock must be callable")
    headers = {
        "Authorization": "Bearer " + api_key,
        "Accept": "application/json",
        "Accept-Encoding": "identity",
    }

    def decode(response, maximum):
        text = _decode_response(response, maximum)
        # Reject an echoed credential rather than redact or persist source
        # evidence, before the observation clock call.
        if api_key in text:
            raise AcquisitionError("credential_exposure")
        return text

    def interpret(text, observed_at):
        source = SourceMetadata(OPENROUTER_CATALOG_URL, {"acquisition_contract": _CONTRACT})
        parsed = parse_catalog(text, observed_at=observed_at, source=source)
        if parsed.accepted is not True:
            raise AcquisitionError("catalog_rejected")
        count = _catalog_count(text, len(parsed.observations), api_key)
        # Count is trusted only after strict acceptance. Reparse the unchanged text
        # with final provenance instead of mutating any parser-produced observation.
        source = SourceMetadata(OPENROUTER_CATALOG_URL, {
            "acquisition_contract": _CONTRACT, "total_count": count,
        })
        parsed = parse_catalog(text, observed_at=observed_at, source=source)
        if parsed.accepted is not True or len(parsed.observations) != count:
            raise AcquisitionError("catalog_rejected")
        return source, parsed.observations, count

    return run_scan(
        connection, profile=OPENROUTER_PROFILE, headers=headers, transport=transport,
        clock=clock, timeout_seconds=timeout_seconds, max_response_bytes=max_response_bytes,
        utc=_utc, decode=decode, interpret=interpret, write=write_snapshot,
    )
