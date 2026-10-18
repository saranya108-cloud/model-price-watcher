"""Gate 1a K.6: the shared provider-neutral scan runner and its retained wrappers."""

import ast
import inspect
import json
import textwrap
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import Mock, patch

from model_price_watcher import acquisition, cheaper_inference_acquisition, contracts
from model_price_watcher.acquisition import (
    OPENROUTER_CATALOG_URL, OPENROUTER_PROFILE, AcquisitionError, ScanProfile, ScanResult,
    run_scan, scan_openrouter,
)
from model_price_watcher.cheaper_inference_acquisition import (
    CHEAPER_INFERENCE_PROFILE, scan_cheaper_inference,
)
from model_price_watcher.contracts import ProviderRecord, SourceContract
from model_price_watcher.models import CatalogObservation, SourceMetadata
from model_price_watcher.providers.cheaper_inference import SOURCE_URL, STREAM_ID
from model_price_watcher.storage import (
    StorageError, get_successful_history, open_database, write_snapshot,
)
from model_price_watcher.transport import HttpResponse, TransportError

START = datetime(2026, 9, 20, 12, tzinfo=timezone.utc)
KEY = "synthetic-test-key-not-a-credential"
JSON_HEADERS = (("Content-Type", "application/json"),)
OPENROUTER_BODY = json.dumps({
    "data": [{"id": "example/model", "pricing": {"prompt": "0.000001", "completion": "0"}}],
    "total_count": 1, "links": {},
}).encode()
CI_BODY = (b'{"models":[{"id":"A","model_type":"text","input_per_million":"1.25",'
           b'"output_per_million":"10","discount_percent":"0","endpoint":"/v1/chat/completions",'
           b'"supported_endpoints":[]}]}')

TEST_RECORD = ProviderRecord(
    provider_id="mpw_gate1a_np", streams=("mpw_gate1a_np.catalog",),
    contracts=(SourceContract(
        contract_id="mpw_gate1a_np-v1", location="https://example.invalid/mpw_gate1a_np",
        metadata=(("interpretation_policy", "mpw_gate1a_np-evidence-v1"),
                  ("source_contract", "mpw_gate1a_np-v1")),
        capture="strict", quote_column="forbidden", price_columns="forbidden"),))
TEST_PROFILE = ScanProfile("mpw_gate1a_np.catalog", "https://example.invalid/mpw_gate1a_np",
                           ("observed before started", "completed before observed"))


def _clock(*offsets):
    return Mock(side_effect=[START + timedelta(seconds=s) for s in (offsets or (0, 1, 2))])


def _utc(value):
    return value.astimezone(timezone.utc)


def _test_source():
    contract = TEST_RECORD.contracts[0]
    return SourceMetadata(contract.location, dict(contract.metadata))


def _test_interpret(text, observed_at):
    rows = json.loads(text)["rows"]
    source = _test_source()
    observations = tuple(CatalogObservation(
        TEST_PROFILE.provider, row, observed_at, _test_source(), None, None, {}, {"id": row})
        for row in rows)
    return source, observations, len(observations)


def _decode(response, maximum):
    return response.body.decode("utf-8")


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.connection = open_database(":memory:")
        self.addCleanup(self.connection.close)
        registry = patch.object(contracts, "PROVIDER_RECORDS", (*contracts.PROVIDER_RECORDS, TEST_RECORD))
        registry.start()
        self.addCleanup(registry.stop)
        # Seed prior valid history through the runner itself.
        self.run_test(body=b'{"rows":["prior"]}', clock=_clock(-10, -9, -8))
        self.before = get_successful_history(self.connection, TEST_PROFILE.provider)
        self.assertEqual(len(self.before), 1)

    def run_test(self, *, body=b'{"rows":["a","b"]}', transport=None, clock=None, **overrides):
        if transport is None:
            transport = Mock()
            transport.get.return_value = HttpResponse(200, body, JSON_HEADERS)
        options = dict(profile=TEST_PROFILE, headers={"Accept": "application/json"},
                       transport=transport, clock=clock or _clock(), timeout_seconds=5.0,
                       max_response_bytes=1000, utc=_utc, decode=_decode,
                       interpret=_test_interpret, write=write_snapshot)
        options.update(overrides)
        return run_scan(self.connection, **options)

    def test_successful_generic_scan_persists_through_storage(self):
        transport = Mock()
        transport.get.return_value = HttpResponse(200, b'{"rows":["a","b"]}', JSON_HEADERS)
        clock = _clock()
        result = self.run_test(transport=transport, clock=clock)
        self.assertIsInstance(result, ScanResult)
        self.assertEqual(result.observation_count, 2)
        self.assertEqual(clock.call_count, 3)
        transport.get.assert_called_once_with(
            TEST_PROFILE.url, headers={"Accept": "application/json"},
            timeout_seconds=5.0, max_response_bytes=1000)
        history = get_successful_history(self.connection, TEST_PROFILE.provider)
        self.assertEqual(history[:1], self.before)
        snapshot, rows = history[-1]
        self.assertEqual(snapshot, result.snapshot)
        self.assertEqual((snapshot.started_at, snapshot.completed_at),
                         (START, START + timedelta(seconds=2)))
        self.assertEqual([r.observed_at for r in rows], [START + timedelta(seconds=1)] * 2)

    def test_failures_at_every_step_preserve_history(self):
        storage_error = StorageError("injected")

        def failing_decode(response, maximum):
            raise AcquisitionError("invalid_response")

        def failing_interpret(text, observed_at):
            raise AcquisitionError("catalog_rejected")
        cases = {
            "transport": (dict(transport=Mock(**{"get.side_effect": TransportError("network_error")})),
                          AcquisitionError),
            "decode": (dict(decode=failing_decode), AcquisitionError),
            "observed clock": (dict(clock=_clock(0, -1, 2)), ValueError),
            "interpret": (dict(interpret=failing_interpret), AcquisitionError),
            "completed clock": (dict(clock=_clock(0, 2, 1)), ValueError),
            "write": (dict(write=Mock(side_effect=storage_error)), StorageError),
        }
        for label, (overrides, error) in cases.items():
            with self.subTest(label):
                write = overrides.get("write") or Mock(wraps=write_snapshot)
                overrides["write"] = write
                with self.assertRaises(error) as caught:
                    self.run_test(**overrides)
                self.assertEqual(write.call_count, 1 if label == "write" else 0)
                if label == "write":
                    self.assertIs(caught.exception, storage_error)
                if label == "observed clock":
                    self.assertEqual(str(caught.exception), "observed before started")
                if label == "completed clock":
                    self.assertEqual(str(caught.exception), "completed before observed")
                self.assertEqual(get_successful_history(self.connection, TEST_PROFILE.provider), self.before)
                self.assertFalse(self.connection.in_transaction)

    def test_transport_error_is_deferred_without_context(self):
        transport = Mock(**{"get.side_effect": TransportError("response_too_large")})
        clock = _clock()
        with self.assertRaises(AcquisitionError) as caught:
            self.run_test(transport=transport, clock=clock)
        self.assertEqual(caught.exception.reason, "response_too_large")
        self.assertIsNone(caught.exception.__context__)
        self.assertIsNone(caught.exception.__cause__)
        self.assertEqual(clock.call_count, 1)

    def test_runner_contains_no_provider_literals(self):
        tree = ast.parse(textwrap.dedent(inspect.getsource(run_scan)))
        function = tree.body[0]
        docstring = function.body[0].value
        literals = [n.value for n in ast.walk(function)
                    if isinstance(n, ast.Constant) and isinstance(n.value, str) and n is not docstring]
        self.assertEqual(literals, [])


class WrapperTests(unittest.TestCase):
    def setUp(self):
        self.connection = open_database(":memory:")
        self.addCleanup(self.connection.close)

    def test_each_wrapper_calls_run_scan_exactly_once(self):
        transport = Mock()
        transport.get.return_value = HttpResponse(200, OPENROUTER_BODY, JSON_HEADERS)
        with patch.object(acquisition, "run_scan", wraps=run_scan) as spy:
            scan_openrouter(self.connection, api_key=KEY, transport=transport, clock=_clock())
        self.assertEqual(spy.call_count, 1)
        self.assertIs(spy.call_args.kwargs["profile"], OPENROUTER_PROFILE)
        transport = Mock()
        transport.get.return_value = HttpResponse(200, CI_BODY, JSON_HEADERS)
        with patch.object(cheaper_inference_acquisition, "run_scan", wraps=run_scan) as spy:
            scan_cheaper_inference(self.connection, transport=transport, clock=_clock(3, 4, 5))
        self.assertEqual(spy.call_count, 1)
        self.assertIs(spy.call_args.kwargs["profile"], CHEAPER_INFERENCE_PROFILE)

    def test_openrouter_sequencing(self):
        transport = Mock()
        transport.get.return_value = HttpResponse(200, OPENROUTER_BODY, JSON_HEADERS)
        clock = _clock()
        result = scan_openrouter(self.connection, api_key=KEY, transport=transport, clock=clock)
        self.assertEqual(result.observation_count, 1)
        self.assertEqual(clock.call_count, 3)
        transport.get.assert_called_once_with(
            OPENROUTER_CATALOG_URL,
            headers={"Authorization": "Bearer " + KEY, "Accept": "application/json",
                     "Accept-Encoding": "identity"},
            timeout_seconds=30.0, max_response_bytes=16 * 1024 * 1024)
        self.assertEqual(result.snapshot.source.metadata,
                         {"acquisition_contract": "openrouter-models-v1", "total_count": Decimal(1)})
        # A credential echo is rejected before the observation clock call.
        echo = Mock()
        echo.get.return_value = HttpResponse(
            200, json.dumps({"data": [{"id": KEY}], "total_count": 1, "links": {}}).encode(), JSON_HEADERS)
        clock = _clock()
        with patch.object(acquisition, "write_snapshot") as writer:
            with self.assertRaises(AcquisitionError) as caught:
                scan_openrouter(self.connection, api_key=KEY, transport=echo, clock=clock)
        self.assertEqual(caught.exception.reason, "credential_exposure")
        self.assertEqual(clock.call_count, 1)
        self.assertEqual(writer.call_count, 0)

    def test_cheaper_inference_sequencing(self):
        transport = Mock()
        transport.get.return_value = HttpResponse(200, CI_BODY, JSON_HEADERS)
        clock = _clock()
        result = scan_cheaper_inference(self.connection, transport=transport, clock=clock)
        self.assertEqual(result.observation_count, 1)
        self.assertEqual(clock.call_count, 3)
        transport.get.assert_called_once_with(
            SOURCE_URL, headers={"Accept": "application/json", "Accept-Encoding": "identity"},
            timeout_seconds=30.0, max_response_bytes=16 * 1024 * 1024)
        self.assertEqual(result.snapshot.provider, STREAM_ID)
        for offsets, message in (((0, -1, 2), "observation time precedes start"),
                                 ((0, 2, 1), "completion precedes observation")):
            with self.subTest(message=message), self.assertRaises(ValueError) as caught:
                scan_cheaper_inference(self.connection, transport=transport, clock=_clock(*offsets))
            self.assertEqual(str(caught.exception), message)
        for offsets, message in (((0, -1, 2), "observation time precedes scan start"),
                                 ((0, 2, 1), "completion time precedes observation time")):
            openrouter = Mock()
            openrouter.get.return_value = HttpResponse(200, OPENROUTER_BODY, JSON_HEADERS)
            with self.subTest(message=message), self.assertRaises(ValueError) as caught:
                scan_openrouter(self.connection, api_key=KEY, transport=openrouter, clock=_clock(*offsets))
            self.assertEqual(str(caught.exception), message)

    def test_production_profiles_resolve_to_declared_or_grandfathered_providers(self):
        record = contracts.reserved_record(CHEAPER_INFERENCE_PROFILE.provider)
        self.assertIsNotNone(record)
        self.assertIn(CHEAPER_INFERENCE_PROFILE.provider, record.streams)
        self.assertEqual(CHEAPER_INFERENCE_PROFILE.url, SOURCE_URL)
        self.assertEqual(OPENROUTER_PROFILE.provider, "openrouter")
        self.assertIsNone(contracts.reserved_record(OPENROUTER_PROFILE.provider))
        self.assertEqual(OPENROUTER_PROFILE.url, OPENROUTER_CATALOG_URL)


if __name__ == "__main__":
    unittest.main()
