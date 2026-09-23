"""One keyless public Standard catalog request; only complete success is stored."""
import sqlite3
from collections.abc import Callable, Sequence
from datetime import datetime, timezone

from model_price_watcher.acquisition import ScanResult, AcquisitionError
from model_price_watcher.models import SourceMetadata
from model_price_watcher.providers.cheaper_inference import (
    STREAM_ID, SOURCE_URL, SOURCE_METADATA, parse_catalog,
)
from model_price_watcher.storage import StorageError, write_snapshot
from model_price_watcher.transport import HttpTransport, HttpResponse, TransportError, validate_limits


def _utc(value):
    if not isinstance(value, datetime):
        raise TypeError('clock must return datetime')
    invalid = False
    try:
        if value.tzinfo is None or value.utcoffset() is None:
            invalid = True
        else:
            result = value.astimezone(timezone.utc)
    except (OverflowError, OSError, ValueError):
        invalid = True
    if invalid:
        raise ValueError('clock must return an aware UTC-representable datetime')
    return result


def _decode_response(response, maximum):
    if not isinstance(response, HttpResponse) or type(response.status) is not int:
        raise AcquisitionError('invalid_response')
    if response.status != 200:
        raise AcquisitionError('http_status', status=response.status)
    if not isinstance(response.body, bytes):
        raise AcquisitionError('invalid_response')
    if len(response.body) > maximum:
        raise AcquisitionError('response_too_large')
    if not isinstance(response.headers, Sequence) or isinstance(response.headers, (str, bytes)):
        raise AcquisitionError('invalid_response')
    media, encoding, lengths, transfer = [], [], [], []
    for pair in response.headers:
        if not isinstance(pair, tuple) or len(pair) != 2 or any(not isinstance(v, str) for v in pair):
            raise AcquisitionError('invalid_response')
        name, value = pair
        if name.lower() == 'content-type':
            media.append(value.split(';', 1)[0].strip().lower())
        elif name.lower() == 'content-encoding':
            encoding.append(value.strip().lower())
        elif name.lower() == 'content-length':
            lengths.append(value.strip())
        elif name.lower() == 'transfer-encoding':
            transfer.append(value.strip().lower())
    if media != ['application/json']:
        raise AcquisitionError('unsupported_media_type')
    if encoding not in ([], ['identity']):
        raise AcquisitionError('unsupported_encoding')
    if len(lengths) > 1 or (lengths and transfer) or transfer not in ([], ['chunked']):
        raise AcquisitionError('invalid_framing')
    if lengths:
        if not lengths[0] or any(c not in '0123456789' for c in lengths[0]):
            raise AcquisitionError('invalid_framing')
        # Compare bounded normalized decimal text; never parse arbitrary-length ints.
        if (lengths[0].lstrip('0') or '0') != str(len(response.body)):
            raise AcquisitionError('incomplete_body')
    failed = False
    try:
        text = response.body.decode('utf-8', errors='strict')
    except UnicodeDecodeError:
        failed = True
    if failed:
        raise AcquisitionError('invalid_utf8')
    return text


def scan_cheaper_inference(connection: sqlite3.Connection, *, transport: HttpTransport,
                          clock: Callable[[], datetime], timeout_seconds: float = 30.0,
                          max_response_bytes: int = 16 * 1024 * 1024) -> ScanResult:
    """Use an existing v2 connection and injected transport/clock, exactly once.

    No retry, redirect, credential, URL override, migration, or failed snapshot.
    Storage exceptions (including indeterminate rollback) propagate unchanged.
    """
    if not isinstance(connection, sqlite3.Connection):
        raise TypeError('connection must be sqlite3.Connection')
    if connection.in_transaction:
        raise RuntimeError('scan refuses caller-owned transaction')
    if connection.execute('PRAGMA user_version').fetchone()[0] != 2:
        raise StorageError('scan requires version 2 database')
    if connection.execute('PRAGMA foreign_keys').fetchone()[0] != 1:
        raise StorageError('scan requires foreign key enforcement')
    validate_limits(timeout_seconds, max_response_bytes)
    if not callable(clock) or not callable(getattr(transport, 'get', None)):
        raise TypeError('clock and transport must be callable')
    started = _utc(clock())
    failure = None
    try:
        response = transport.get(SOURCE_URL, headers={'Accept': 'application/json', 'Accept-Encoding': 'identity'},
                                 timeout_seconds=timeout_seconds, max_response_bytes=max_response_bytes)
    except TransportError as error:
        failure = error.reason
    if failure is not None:
        raise AcquisitionError(failure)
    text = _decode_response(response, max_response_bytes)
    observed = _utc(clock())
    if observed < started:
        raise ValueError('observation time precedes start')
    source = SourceMetadata(SOURCE_URL, dict(SOURCE_METADATA))
    parsed = parse_catalog(text, observed_at=observed, source=source)
    if not parsed.accepted:
        raise AcquisitionError('catalog_rejected')
    if not parsed.observations:
        raise AcquisitionError('empty_catalog')
    completed = _utc(clock())
    if completed < observed:
        raise ValueError('completion precedes observation')
    snapshot = write_snapshot(connection, provider=STREAM_ID, started_at=started, completed_at=completed,
                              source=source, observations=parsed.observations)
    return ScanResult(snapshot, len(parsed.observations))
