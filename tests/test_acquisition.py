"""Deterministic OpenRouter scans using fake transport and real SQLite."""

import json
import sqlite3
import traceback
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch

from model_price_watcher import acquisition
from model_price_watcher.acquisition import AcquisitionError, scan_openrouter
from model_price_watcher.models import SourceMetadata
from model_price_watcher.providers.openrouter import parse_catalog
from model_price_watcher.storage import (
    StorageError, get_successful_history, open_database, write_snapshot,
)
from model_price_watcher.transport import HttpResponse, TransportError


KEY = "synthetic-test-key-not-a-credential"
URL = "https://openrouter.ai/api/v1/models?output_modalities=all"
START = datetime(2026, 9, 20, 12, tzinfo=timezone.utc)


class Clock:
    def __init__(self, values=None):
        self.values = values or [START, START + timedelta(seconds=1), START + timedelta(seconds=2)]
        self.calls = 0

    def __call__(self):
        value = self.values[self.calls]
        self.calls += 1
        return value


class FakeTransport:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.error is not None:
            raise self.error
        return self.response


def catalog(**updates):
    document = {
        "data": [{"id": "example/model", "pricing": {"prompt": "0.000001", "completion": "0"}}],
        "total_count": 1, "links": {},
    }
    document.update(updates)
    return json.dumps(document).encode()


def response(body=None, status=200, headers=None):
    return HttpResponse(status, catalog() if body is None else body,
                        (("Content-Type", "application/json"),) if headers is None else headers)


class AcquisitionTests(unittest.TestCase):
    def setUp(self):
        self.connection = open_database(":memory:")
        self.addCleanup(self.connection.close)
        source = SourceMetadata("offline-fixture")
        old_time = START - timedelta(days=1)
        parsed = parse_catalog('{"data":[{"id":"prior","pricing":{"prompt":"1"}}]}',
                               observed_at=old_time, source=source)
        write_snapshot(self.connection, provider="openrouter", started_at=old_time,
                       completed_at=old_time, source=source, observations=parsed.observations)
        self.before = get_successful_history(self.connection, "openrouter")

    def scan(self, transport=None, clock=None, **kwargs):
        options = dict(api_key=KEY, transport=transport or FakeTransport(response()),
                       clock=clock or Clock(), timeout_seconds=4, max_response_bytes=10000)
        options.update(kwargs)
        return scan_openrouter(self.connection, **options)

    def assert_failure(self, item, reason=None):
        transport = item if isinstance(item, FakeTransport) else FakeTransport(item)
        with patch.object(acquisition, "write_snapshot", wraps=write_snapshot) as writer:
            with self.assertRaises(AcquisitionError) as caught:
                self.scan(transport)
            writer.assert_not_called()
        if reason:
            self.assertEqual(caught.exception.reason, reason)
        self.assertEqual(get_successful_history(self.connection, "openrouter"), self.before)
        self.assertFalse(self.connection.in_transaction)
        self.assertNotIn(KEY, "".join(traceback.format_exception(caught.exception)))
        self.assertEqual(len(transport.calls), 1)
        return caught.exception

    def test_success_request_provenance_and_commit(self):
        transport, clock = FakeTransport(response()), Clock()
        with patch.object(acquisition, "write_snapshot", wraps=write_snapshot) as writer:
            result = self.scan(transport, clock)
            self.assertEqual(writer.call_count, 1)
        self.assertEqual(clock.calls, 3)
        self.assertEqual(transport.calls, [(URL, {
            "headers": {"Authorization": "Bearer " + KEY, "Accept": "application/json",
                        "Accept-Encoding": "identity"},
            "timeout_seconds": 4, "max_response_bytes": 10000,
        })])
        history = get_successful_history(self.connection, "openrouter")
        self.assertEqual(len(history), 2)
        self.assertFalse(self.connection.in_transaction)
        self.assertEqual(result.snapshot, history[-1][0])
        self.assertEqual(result.observation_count, 1)
        self.assertEqual(result.snapshot.source.location, URL)
        self.assertEqual(result.snapshot.source.metadata, {
            "acquisition_contract": "openrouter-models-v1", "total_count": 1,
        })
        self.assertEqual(result.snapshot.started_at, START)
        self.assertEqual(result.snapshot.completed_at, START + timedelta(seconds=2))
        observation = history[-1][1][0]
        self.assertEqual(observation.observed_at, START + timedelta(seconds=1))
        self.assertEqual(observation.input_per_million, Decimal(1))
        self.assertEqual(observation.output_per_million, Decimal(0))
        self.assertNotIn(KEY, repr(result))
        self.assertNotIn(KEY, repr(history))

    def test_next_absent_or_null(self):
        for links in ({}, {"next": None}):
            with self.subTest(links=links):
                self.assertEqual(self.scan(FakeTransport(response(catalog(links=links)))).observation_count, 1)

    def test_api_key_validation_without_side_effects(self):
        for key in (None, 1, "", "  ", "bad\r\nheader", "bad\x00header", "nonascii-é"):
            transport, clock = FakeTransport(response()), Clock()
            with self.subTest(key_type=type(key)), self.assertRaises((TypeError, ValueError)) as caught:
                self.scan(transport, clock, api_key=key)
            self.assertEqual(clock.calls, 0)
            self.assertEqual(transport.calls, [])
            self.assertNotIn("bad", str(caught.exception))

    def test_echoed_credential_is_rejected_without_persisting_or_exposing_it(self):
        body = catalog(data=[{"id": "x", "description": KEY}])
        self.assert_failure(response(body), "credential_exposure")
        escaped = body.replace(KEY.encode(), b"".join(
            ("\\u%04x" % ord(char)).encode() for char in KEY
        ))
        self.assert_failure(response(escaped), "credential_exposure")

    def test_parser_rejection_precedes_supplementary_inspection(self):
        # A rejected parser result must never reach envelope inspection.
        from model_price_watcher.models import ParseResult
        rejected = ParseResult(False, (), (), START, SourceMetadata(URL), "ignored")
        with patch.object(acquisition, "parse_catalog", return_value=rejected), \
             patch.object(acquisition, "_catalog_count", side_effect=AssertionError):
            self.assert_failure(response(), "catalog_rejected")

    def test_invalid_limits_fail_before_clock_and_transport(self):
        for options in ({"timeout_seconds": 0}, {"timeout_seconds": float("inf")},
                        {"max_response_bytes": 0}, {"max_response_bytes": True}):
            transport, clock = FakeTransport(response()), Clock()
            with self.subTest(options=options), self.assertRaises((ValueError, TypeError)):
                self.scan(transport, clock, **options)
            self.assertEqual(transport.calls, [])
            self.assertEqual(clock.calls, 0)

    def test_transaction_rejected_before_clock_or_transport(self):
        self.connection.execute("BEGIN")
        transport, clock = FakeTransport(response()), Clock()
        with self.assertRaises(RuntimeError):
            self.scan(transport, clock)
        self.assertTrue(self.connection.in_transaction)
        self.assertEqual(clock.calls, 0)
        self.assertEqual(transport.calls, [])
        self.assertEqual(get_successful_history(self.connection, "openrouter"), self.before)

    def test_status_failures_preserve_history(self):
        for status in (301, 302, 307, 308, 204, 206, 304, 400, 401, 403, 429, 500, 503):
            with self.subTest(status=status):
                error = self.assert_failure(response(status=status), "http_status")
                self.assertEqual(error.status, status)

    def test_transport_failure_preserves_history_and_is_sanitized(self):
        for reason in ("network_error", "response_too_large", "invalid_framing", "incomplete_body"):
            self.assert_failure(FakeTransport(error=TransportError(reason)), reason)

    def test_representation_failures(self):
        for headers in ((), (("Content-Type", "text/html"),),
                        (("Content-Type", "application/jsonx"),),
                        (("Content-Type", "application/json"), ("Content-Encoding", "gzip")),
                        (("Content-Type", "application/json"), ("Content-Encoding", "br")),
                        (("Content-Type", "application/json"), ("Content-Type", "text/html"))):
            with self.subTest(headers=headers):
                self.assert_failure(response(headers=headers))
        self.assert_failure(response(body=b"\xff"), "invalid_utf8")
        self.assert_failure(response(body=b"x" * 10001), "response_too_large")

    def test_media_type_parameters_case_and_identity(self):
        for media in ("application/json", "Application/JSON", "application/json; charset=utf-8"):
            with self.subTest(media=media):
                result = self.scan(FakeTransport(response(headers=(
                    ("cOnTeNt-TyPe", media), ("Content-Encoding", "IDENTITY"),
                ))))
                self.assertEqual(result.observation_count, 1)

    def test_rejected_parser_inputs_never_write(self):
        bodies = [b"{", b'{"data":[],"data":[]}', b'{"data":NaN}',
                  b'{"data":[{}]}', b'{"data":[{"id":"x","pricing":null}]}',
                  b'{"data":[{"id":"x"},{"id":"x"}]}', b'[]']
        for body in bodies:
            with self.subTest(body=body):
                self.assert_failure(response(body), "catalog_rejected")

    def test_completeness_failures_preserve_history(self):
        for update in ({"total_count": True}, {"total_count": -1}, {"total_count": 1.0},
                       {"total_count": "1"}, {"total_count": None}, {"total_count": 2},
                       {"links": None}, {"links": []}, {"links": {"next": "/next"}},
                       {"links": {"next": False}}, {"links": {"next": ""}}):
            with self.subTest(update=update):
                self.assert_failure(response(catalog(**update)))
        for missing in ("links", "total_count"):
            document = json.loads(catalog())
            del document[missing]
            self.assert_failure(response(json.dumps(document).encode()), "invalid_envelope")

    def test_empty_catalog_preserves_history(self):
        self.assert_failure(response(catalog(data=[], total_count=0)), "empty_catalog")

    def test_empty_catalog_without_history(self):
        connection = open_database(":memory:")
        try:
            with self.assertRaises(AcquisitionError):
                scan_openrouter(connection, api_key=KEY, clock=Clock(),
                                transport=FakeTransport(response(catalog(data=[], total_count=0))))
            self.assertEqual(get_successful_history(connection, "openrouter"), ())
        finally:
            connection.close()

    def test_pricing_evidence_rows_are_never_filtered(self):
        rows = [{"id": "unknown"}, {"id": "malformed", "pricing": {"prompt": "bad"}},
                {"id": "image", "pricing": {"image": "0.02"}}]
        result = self.scan(FakeTransport(response(catalog(data=rows, total_count=3))))
        stored = get_successful_history(self.connection, "openrouter")[-1][1]
        self.assertEqual(result.observation_count, 3)
        self.assertEqual({row.offering_id for row in stored}, {"unknown", "malformed", "image"})
        self.assertTrue(all(row.input_per_million is None for row in stored))
        self.assertEqual(stored[0].conditions, {"image": "0.02"})
        self.assertEqual(stored[1].source_record["pricing"]["prompt"], "bad")

    def test_original_text_and_decimal_are_preserved(self):
        text = ' { "data": [{"id":"x", "pricing":{"prompt":0.000001234567890123456789}}], "total_count":1, "links":{} }\n'
        with patch.object(acquisition, "parse_catalog", wraps=parse_catalog) as parser:
            self.scan(FakeTransport(response(text.encode())))
        self.assertTrue(parser.call_args_list)
        self.assertTrue(all(call.args == (text,) for call in parser.call_args_list))
        stored = get_successful_history(self.connection, "openrouter")[-1][1][0]
        self.assertEqual(stored.input_per_million, Decimal("1.234567890123456789"))

    def test_utc_conversion_and_common_time(self):
        zone = timezone(timedelta(hours=-7))
        values = [START.astimezone(zone) + timedelta(seconds=i) for i in range(3)]
        result = self.scan(clock=Clock(values))
        self.assertEqual(result.snapshot.started_at, START)
        self.assertIs(result.snapshot.started_at.tzinfo, timezone.utc)

    def test_invalid_and_backward_times_preserve_history(self):
        for values in ([START.replace(tzinfo=None), START, START],
                       [START, START.replace(tzinfo=None), START],
                       [START, START, START.replace(tzinfo=None)],
                       [START, START - timedelta(seconds=1), START],
                       [START, START + timedelta(seconds=2), START],
                       [None, START, START]):
            with self.subTest(values=values), self.assertRaises((TypeError, ValueError)):
                self.scan(clock=Clock(values))
            self.assertEqual(get_successful_history(self.connection, "openrouter"), self.before)

    def test_storage_errors_propagate_unchanged(self):
        for error in (StorageError("storage unavailable"), ValueError("storage validation")):
            with patch.object(acquisition, "write_snapshot", side_effect=error) as writer:
                with self.assertRaises(type(error)) as caught:
                    self.scan()
                self.assertIs(caught.exception, error)
                self.assertEqual(writer.call_count, 1)
            self.assertEqual(get_successful_history(self.connection, "openrouter"), self.before)

    def test_real_statement_and_commit_failures_roll_back(self):
        for failure in ("insert", "commit"):
            def authorizer(action, arg1, *_):
                if failure == "insert" and action == sqlite3.SQLITE_INSERT and arg1 == "observations":
                    return sqlite3.SQLITE_DENY
                if failure == "commit" and action == sqlite3.SQLITE_TRANSACTION and arg1 == "COMMIT":
                    return sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_OK
            self.connection.set_authorizer(authorizer)
            try:
                with self.subTest(failure=failure), self.assertRaises(sqlite3.DatabaseError):
                    self.scan()
            finally:
                self.connection.set_authorizer(None)
            self.assertFalse(self.connection.in_transaction)
            self.assertEqual(get_successful_history(self.connection, "openrouter"), self.before)

    def test_storage_validation_failure_is_not_acquisition_error(self):
        rows = [{"id": "bad\u0000id"}]
        with self.assertRaises(ValueError):
            self.scan(FakeTransport(response(catalog(data=rows))))
        self.assertEqual(get_successful_history(self.connection, "openrouter"), self.before)

    def test_no_downstream_analysis_or_credential_discovery(self):
        with patch("model_price_watcher.detection.detect_current", side_effect=AssertionError), \
             patch("model_price_watcher.detection.analyze_history", side_effect=AssertionError), \
             patch("model_price_watcher.queries.view_selected_offerings", side_effect=AssertionError), \
             patch("model_price_watcher.queries.select_offerings", side_effect=AssertionError), \
             patch("os.getenv", side_effect=AssertionError), \
             patch("builtins.open", side_effect=AssertionError):
            self.assertEqual(self.scan().observation_count, 1)


if __name__ == "__main__":
    unittest.main()
