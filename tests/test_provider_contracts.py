"""Gate 1a: declarative provider contracts and the provider-neutral storage boundary.

Covers Revision 2.1 K.2 (storage decoupling), K.3 (legacy identity and shim),
K.4 (construction phase, registry phase, pins, agreement) and K.5 (the
generic-provider persistence proof). Test-only providers are introduced only
as data, by patching ``contracts.PROVIDER_RECORDS``.
"""

import ast
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone, tzinfo
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from unittest.mock import patch

from model_price_watcher import contracts, legacy_cheaper_inference, storage
from model_price_watcher.contracts import ProviderRecord, RegistryError, SourceContract
from model_price_watcher.models import (
    AdvertisedQuoteBasis, AdvertisedTokenQuote, CatalogObservation, SourceMetadata,
)
from model_price_watcher.providers import cheaper_inference as shim
from model_price_watcher.providers.cheaper_inference import (
    SOURCE_METADATA, SOURCE_URL, STREAM_ID, V1_SOURCE_METADATA, is_reserved_stream,
    parse_catalog as parse_ci,
)
from model_price_watcher.storage import (
    StorageError, get_current_observations, get_latest_successful_snapshot,
    get_offering_history, get_snapshot_observations, get_successful_history,
    get_successful_snapshots, open_database, write_snapshot,
)

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "src" / "model_price_watcher"
# SHA-256 of 2fb4be6:src/model_price_watcher/providers/cheaper_inference.py.
BASELINE_PROVIDER_SHA256 = "2b3bb6cf87b3d0acdf0ed007eacd5ba006d68470d085f2eccd5e949c1f1f98f6"

EARLY_STARTED = datetime(2026, 9, 19, 10, 0, tzinfo=timezone.utc)
EARLY_COMPLETED = datetime(2026, 9, 19, 10, 30, tzinfo=timezone.utc)
STARTED = datetime(2026, 9, 19, 12, 0, tzinfo=timezone.utc)
OBSERVED = datetime(2026, 9, 19, 12, 30, tzinfo=timezone.utc)
COMPLETED = datetime(2026, 9, 19, 13, 0, tzinfo=timezone.utc)
LATE_STARTED = datetime(2026, 9, 19, 14, 0, tzinfo=timezone.utc)
LATE_OBSERVED = datetime(2026, 9, 19, 14, 30, tzinfo=timezone.utc)
LATE_COMPLETED = datetime(2026, 9, 19, 15, 0, tzinfo=timezone.utc)


def _contract(contract_id, location, policy, *, price):
    return SourceContract(
        contract_id=contract_id, location=location,
        metadata=(("interpretation_policy", policy), ("source_contract", contract_id)),
        capture="strict", quote_column="forbidden", price_columns=price)


def _record(provider_id, *, price):
    return ProviderRecord(
        provider_id=provider_id, streams=(provider_id + ".catalog",),
        contracts=(_contract(f"{provider_id}-v1", f"https://example.invalid/{provider_id}",
                             f"{provider_id}-evidence-v1", price=price),))


NP = _record("mpw_gate1a_np", price="forbidden")
PP = _record("mpw_gate1a_pp", price="permitted")


def _stream(record):
    return record.streams[0]


def _source(record):
    contract = record.contracts[0]
    return SourceMetadata(contract.location, dict(contract.metadata))


def _observation(record, offering_id="a", **changes):
    priced = record.contracts[0].price_columns == "permitted"
    values = dict(
        provider=_stream(record), offering_id=offering_id, observed_at=OBSERVED,
        source=_source(record),
        input_usd_per_million=Decimal("1.25") if priced else None,
        output_usd_per_million=Decimal("2.5") if priced else None,
        unsupported_pricing={}, raw_offering={"id": offering_id}, advertised_quote=None)
    values.update(changes)
    return CatalogObservation(**values)


def _quote():
    basis = AdvertisedQuoteBasis(*("x" for _ in range(8)))
    return AdvertisedTokenQuote(basis, Decimal("1"), Decimal("2"))


@contextmanager
def generic_registry(*records):
    with patch.object(contracts, "PROVIDER_RECORDS", (*contracts.PROVIDER_RECORDS, *records)):
        yield


@contextmanager
def legacy_trap():
    with patch.object(contracts, "run_legacy_policy",
                      side_effect=AssertionError("legacy dispatch")) as trap:
        yield trap


def _ci_cases():
    model = dict(id="A", model_type="text", input_per_million="1.25",
                 output_per_million="10", discount_percent="0")
    v1 = SourceMetadata(SOURCE_URL, dict(V1_SOURCE_METADATA))
    old, = parse_ci(json.dumps({"models": [model]}), observed_at=OBSERVED, source=v1).observations
    v2 = SourceMetadata(SOURCE_URL, dict(SOURCE_METADATA))
    model.update(endpoint="/v1/responses", supported_endpoints=[])
    current, = parse_ci(json.dumps({"models": [model]}), observed_at=OBSERVED, source=v2).observations
    return v1, old, v2, current


# --------------------------------------------------------------------------- K.2 / K.3


class StaticBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.text = (PACKAGE / "storage.py").read_text(encoding="utf-8")
        self.tree = ast.parse(self.text)

    def test_storage_imports_only_models_and_contracts(self):
        targets = set()
        for node in ast.walk(self.tree):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("model_price_watcher"):
                if node.module == "model_price_watcher":
                    targets.update(f"model_price_watcher.{alias.name}" for alias in node.names)
                else:
                    targets.add(node.module)
            elif isinstance(node, ast.Import):
                targets.update(a.name for a in node.names if a.name.startswith("model_price_watcher"))
        self.assertEqual(targets, {"model_price_watcher.models", "model_price_watcher.contracts"})

    def test_storage_has_no_provider_literals_or_legacy_names(self):
        docstrings = set()
        for node in ast.walk(self.tree):
            if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                body = node.body
                if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                    docstrings.add(id(body[0].value))
        literals = [n.value for n in ast.walk(self.tree)
                    if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docstrings]
        for forbidden in ("cheaper_inference", "openrouter", "public-models", "ci-advertised"):
            with self.subTest(forbidden=forbidden):
                self.assertFalse([value for value in literals if forbidden in value])
        for name in ("STREAM_ID", "is_reserved_stream", "validate_public_source",
                     "validate_public_observation", "legacy_cheaper_inference"):
            with self.subTest(name=name):
                self.assertIsNone(re.search(r"\b" + name + r"\b", self.text))

    def test_legacy_code_reached_only_through_run_legacy_policy(self):
        calls = [n for n in ast.walk(self.tree) if isinstance(n, ast.Attribute)
                 and isinstance(n.value, ast.Name) and n.value.id == "contracts"]
        used = {n.attr for n in calls}
        self.assertEqual(used, {"reserved_record", "resolve_contract", "run_legacy_policy",
                                "RegistryError", "CAPTURE_STRICT", "QUOTE_FORBIDDEN",
                                "PRICES_FORBIDDEN"})
        self.assertEqual(sum(n.attr == "run_legacy_policy" for n in calls), 1)

    def test_contracts_does_not_import_storage(self):
        tree = ast.parse((PACKAGE / "contracts.py").read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                self.assertNotIn("storage", (node.module or "").split("."))
                self.assertNotIn("storage", [a.name for a in node.names])
            elif isinstance(node, ast.Import):
                self.assertFalse([a for a in node.names if "storage" in a.name.split(".")])
        env = dict(os.environ, PYTHONPATH=str(ROOT / "src"))
        result = subprocess.run(
            [sys.executable, "-c", "import sys, model_price_watcher.contracts; "
             "print('model_price_watcher.storage' in sys.modules)"],
            capture_output=True, text=True, env=env, check=True)
        self.assertEqual(result.stdout.strip(), "False")

    def test_legacy_module_is_byte_identical_to_baseline_provider(self):
        data = (PACKAGE / "legacy_cheaper_inference.py").read_bytes()
        self.assertEqual(hashlib.sha256(data).hexdigest(), BASELINE_PROVIDER_SHA256)

    def test_shim_reexports_every_legacy_name_as_identical_object(self):
        namespace = {"__name__": "legacy_names"}
        exec(compile((PACKAGE / "legacy_cheaper_inference.py").read_bytes(), "legacy", "exec"), namespace)
        names = [k for k in namespace if not k.startswith("__")]
        self.assertGreater(len(names), 40)
        for name in names:
            with self.subTest(name=name):
                self.assertIs(getattr(shim, name), getattr(legacy_cheaper_inference, name))

    def test_change_control_notice_lives_outside_legacy_file(self):
        self.assertIn("CHANGE CONTROL", contracts.__doc__)
        self.assertIn("CHANGE CONTROL", shim.__doc__)
        self.assertNotIn(b"CHANGE CONTROL", (PACKAGE / "legacy_cheaper_inference.py").read_bytes())


# --------------------------------------------------------------------------- K.4


class ConstructionPhaseTests(unittest.TestCase):
    def contract_kwargs(self, **changes):
        values = dict(contract_id="t-v1", location="https://example.invalid/t",
                      metadata=(("interpretation_policy", "t-p"), ("source_contract", "t-v1")),
                      capture="strict", quote_column="forbidden", price_columns="forbidden")
        values.update(changes)
        return values

    def record_kwargs(self, **changes):
        values = dict(provider_id="t", streams=("t.s",),
                      contracts=(SourceContract(**self.contract_kwargs()),))
        values.update(changes)
        return values

    def test_valid_construction(self):
        record = ProviderRecord(**self.record_kwargs())
        self.assertEqual(record.provider_id, "t")
        # Frozen and slotted: assignment is refused (FrozenInstanceError on
        # 3.12+, TypeError from CPython 3.11's slotted __setattr__).
        for name, value in (("extra", 1), ("provider_id", "u")):
            with self.subTest(name=name), self.assertRaises((AttributeError, TypeError)):
                setattr(record, name, value)
        self.assertFalse(hasattr(record, "extra"))
        self.assertEqual(record.provider_id, "t")

    def test_type_errors(self):
        class S(str):
            pass
        contract = SourceContract(**self.contract_kwargs())
        contract_cases = {
            "str-subclass contract_id": dict(contract_id=S("t-v1")),
            "str-subclass location": dict(location=S("https://example.invalid/t")),
            "dict metadata": dict(metadata={"source_contract": "t-v1"}),
            "list metadata": dict(metadata=[("source_contract", "t-v1")]),
            "str-subclass metadata key": dict(metadata=((S("source_contract"), "t-v1"),)),
            "str-subclass metadata value": dict(metadata=(("source_contract", S("t-v1")),)),
            "str-subclass capture": dict(capture=S("strict")),
            "callable location": dict(location=lambda: "x"),
            "callable legacy_policy": dict(legacy_policy=lambda: "x"),
            "callable price rule": dict(price_columns=print),
        }
        for label, change in contract_cases.items():
            with self.subTest(label), self.assertRaises(TypeError):
                SourceContract(**self.contract_kwargs(**change))
        record_cases = {
            "str-subclass provider_id": dict(provider_id=S("t")),
            "list streams": dict(streams=["t.s"]),
            "str-subclass stream": dict(streams=(S("t.s"),)),
            "list contracts": dict(contracts=[contract]),
            "non-SourceContract element": dict(contracts=(dict(contract_id="t-v1"),)),
            "callable provider_id": dict(provider_id=lambda: "t"),
        }
        for label, change in record_cases.items():
            with self.subTest(label), self.assertRaises(TypeError):
                ProviderRecord(**self.record_kwargs(**change))

    def test_value_errors(self):
        contract_cases = {
            "empty location": dict(location=""),
            "unsorted metadata": dict(metadata=(("source_contract", "t-v1"), ("interpretation_policy", "t-p"))),
            "duplicate metadata keys": dict(metadata=(("source_contract", "t-v1"), ("source_contract", "t-v1"))),
            "missing source_contract": dict(metadata=(("interpretation_policy", "t-p"),)),
            "contract_id mismatch": dict(contract_id="other"),
            "short metadata pair": dict(metadata=(("source_contract",),)),
            "unknown capture": dict(capture="lenient"),
            "unknown quote rule": dict(quote_column="allowed"),
            "unknown price rule": dict(price_columns="sometimes"),
            "legacy capture without policy": dict(capture="legacy"),
            "legacy quote without policy": dict(quote_column="legacy_policy"),
        }
        for label, change in contract_cases.items():
            with self.subTest(label), self.assertRaises(ValueError):
                SourceContract(**self.contract_kwargs(**change))
        contract = SourceContract(**self.contract_kwargs())
        record_cases = {
            "blank provider_id": dict(provider_id=""),
            "padded provider_id": dict(provider_id=" t", streams=(" t.s",)),
            "NUL provider_id": dict(provider_id="t\0", streams=("t\0.s",)),
            "dotted provider_id": dict(provider_id="t.u", streams=("t.u.s",)),
            "empty streams": dict(streams=()),
            "duplicate streams": dict(streams=("t.s", "t.s")),
            "stream outside namespace": dict(streams=("other.s",)),
            "bare prefix stream": dict(streams=("t.",)),
            "empty contracts": dict(contracts=()),
            "duplicate contracts": dict(contracts=(contract, contract)),
        }
        for label, change in record_cases.items():
            with self.subTest(label), self.assertRaises(ValueError):
                ProviderRecord(**self.record_kwargs(**change))


class StorageDatabaseCase(unittest.TestCase):
    def setUp(self):
        self._tempdir = tempfile.TemporaryDirectory()
        self.path = str(Path(self._tempdir.name) / "history.sqlite")
        self.connection = open_database(self.path)

    def tearDown(self):
        try:
            self.connection.close()
        except sqlite3.Error:
            pass
        self._tempdir.cleanup()

    def fresh(self):
        self.connection.close()
        self._tempdir.cleanup()
        self.setUp()

    def counts(self):
        return tuple(self.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                     for table in ("snapshots", "observations"))

    def write(self, record, rows=None, *, started=STARTED, completed=COMPLETED, **changes):
        values = dict(provider=_stream(record), started_at=started, completed_at=completed,
                      source=_source(record),
                      observations=(_observation(record),) if rows is None else rows)
        values.update(changes)
        return write_snapshot(self.connection, **values)

    def assert_rejected_before_begin(self, error, call):
        before = tuple(self.connection.iterdump())
        begins, statements = [], []

        def authorizer(action, arg1, *_rest):
            if action == sqlite3.SQLITE_TRANSACTION and arg1 == "BEGIN":
                begins.append(arg1)
            return sqlite3.SQLITE_OK
        self.connection.set_authorizer(authorizer)
        self.connection.set_trace_callback(statements.append)
        try:
            with self.assertRaises(error) as caught:
                call()
        finally:
            self.connection.set_authorizer(None)
            self.connection.set_trace_callback(None)
        self.assertEqual(begins, [])
        self.assertFalse(any(sql.startswith("BEGIN") for sql in statements))
        self.assertEqual(tuple(self.connection.iterdump()), before)
        self.assertFalse(self.connection.in_transaction)
        return caught.exception


class RegistryPhaseTests(StorageDatabaseCase):
    def setUp(self):
        super().setUp()
        self.or_source = SourceMetadata("fixture:openrouter", {"capture": "test"})
        self.or_row = CatalogObservation("openrouter", "m", OBSERVED, self.or_source,
                                         Decimal("1"), Decimal("2"), {}, {"id": "m"})
        write_snapshot(self.connection, provider="openrouter", started_at=STARTED,
                       completed_at=COMPLETED, source=self.or_source, observations=(self.or_row,))
        _, _, v2, current = _ci_cases()
        write_snapshot(self.connection, provider=STREAM_ID, started_at=STARTED,
                       completed_at=COMPLETED, source=v2, observations=(current,))

    def v1_database(self):
        path = str(Path(self._tempdir.name) / "v1.sqlite")
        raw = sqlite3.connect(path)
        for statement in storage._SCHEMA_STATEMENTS:
            raw.execute(statement)
        raw.execute("INSERT INTO snapshots (provider, started_at, completed_at, source_url, "
                    "source_metadata_json) VALUES (?, ?, ?, ?, ?)",
                    ("openrouter", storage._encode_timestamp(STARTED), storage._encode_timestamp(COMPLETED),
                     "fixture:openrouter", storage._encode_json({"capture": "test"})))
        raw.execute("PRAGMA user_version = 1")
        raw.commit()
        raw.close()
        return path

    def assert_registry_fault(self, **patches):
        targets = [patch.object(contracts, name, value) for name, value in patches.items()]
        for target in targets:
            target.start()
        try:
            error = self.assert_rejected_before_begin(StorageError, lambda: write_snapshot(
                self.connection, provider="openrouter", started_at=LATE_STARTED,
                completed_at=LATE_COMPLETED, source=self.or_source,
                observations=(CatalogObservation("openrouter", "m", LATE_OBSERVED, self.or_source,
                                                 None, None, {}, {"id": "m"}),)))
            self.assertEqual(str(error), "invalid provider registry")
            self.assertIsInstance(error.__cause__, RegistryError)
            for reader in (lambda: get_successful_history(self.connection, "openrouter"),
                           lambda: get_successful_history(self.connection, STREAM_ID),
                           lambda: get_latest_successful_snapshot(self.connection, STREAM_ID)):
                with self.assertRaises(StorageError) as caught:
                    reader()
                self.assertEqual(str(caught.exception), "invalid provider registry")
                self.assertIsInstance(caught.exception.__cause__, RegistryError)
            path = self.v1_database()
            with self.assertRaises(StorageError) as caught:
                open_database(path, migrate_v1=True)
            self.assertEqual(str(caught.exception), "invalid provider registry")
            raw = sqlite3.connect(path)
            try:
                self.assertEqual(raw.execute("PRAGMA user_version").fetchone()[0], 1)
            finally:
                raw.close()
            os.unlink(path)
        finally:
            for target in reversed(targets):
                target.stop()

    def test_registry_faults_translate_at_storage_boundary(self):
        ci = contracts.CHEAPER_INFERENCE_RECORD

        class SubRecord(ProviderRecord):
            __slots__ = ()
        subclass = object.__new__(SubRecord)
        for field in ("provider_id", "streams", "contracts"):
            object.__setattr__(subclass, field, getattr(NP, field))
        lookalike = SimpleNamespace(provider_id=NP.provider_id, streams=NP.streams, contracts=NP.contracts)

        def mutated(field, value, *, on_contract=False):
            record = _record("mpw_gate1a_mut", price="forbidden")
            object.__setattr__(record.contracts[0] if on_contract else record, field, value)
            return record

        class S(str):
            pass
        duplicate_namespace = ProviderRecord("cheaper_inference", ("cheaper_inference.other",),
                                             NP.contracts)
        dup_contract = ProviderRecord("mpw_gate1a_dup", ("mpw_gate1a_dup.s",), (
            SourceContract(ci.contracts[1].contract_id, "https://example.invalid/dup",
                           ci.contracts[1].metadata, "strict", "forbidden", "forbidden"),))

        def policy_record(policy, provider_id="mpw_gate1a_pol"):
            return ProviderRecord(provider_id, (provider_id + ".s",), (
                SourceContract(f"{provider_id}-v1", f"https://example.invalid/{provider_id}",
                               (("interpretation_policy", policy), ("source_contract", f"{provider_id}-v1")),
                               "strict", "forbidden", "forbidden", legacy_policy=policy),))
        cases = {
            "list registry": dict(PROVIDER_RECORDS=[ci]),
            "record subclass": dict(PROVIDER_RECORDS=(ci, subclass)),
            "look-alike record": dict(PROVIDER_RECORDS=(ci, lookalike)),
            "mutated capture": dict(PROVIDER_RECORDS=(ci, mutated("capture", "lenient", on_contract=True))),
            "mutated str-subclass stream": dict(PROVIDER_RECORDS=(ci, mutated("streams", (S("mpw_gate1a_mut.catalog"),)))),
            "mutated dotted namespace": dict(PROVIDER_RECORDS=(ci, mutated("provider_id", "cheaper_inference.sub"))),
            "duplicate namespace": dict(PROVIDER_RECORDS=(ci, duplicate_namespace)),
            "duplicate contract_id": dict(PROVIDER_RECORDS=(ci, dup_contract)),
            "names v1 legacy policy": dict(PROVIDER_RECORDS=(ci, policy_record("ci-advertised-base-token-v1"))),
            "reuses v2 legacy policy": dict(PROVIDER_RECORDS=(ci, policy_record("ci-advertised-evidence-only-v2"))),
            "unknown policy": dict(PROVIDER_RECORDS=(ci, policy_record("no-such-policy"))),
            "plain dict policies": dict(LEGACY_POLICIES=dict(contracts.LEGACY_POLICIES)),
            "malformed policy binding": dict(LEGACY_POLICIES=MappingProxyType({
                **contracts.LEGACY_POLICIES, "x": ["s", "c"]})),
        }
        for label, patches in cases.items():
            with self.subTest(label):
                self.assert_registry_fault(**patches)
        # The seeded history is untouched and readable once the registry is valid.
        self.assertEqual(len(get_successful_history(self.connection, "openrouter")), 1)
        self.assertEqual(len(get_successful_history(self.connection, STREAM_ID)), 1)


class PinAndAgreementTests(unittest.TestCase):
    def test_complete_cheaper_inference_record_pinned_to_literals(self):
        expected = ProviderRecord(
            provider_id="cheaper_inference",
            streams=("cheaper_inference.public.standard",),
            contracts=(
                SourceContract(
                    contract_id="cheaper-inference-public-models-v1",
                    location="https://api.cheaperinference.com/public/models",
                    metadata=(("catalog_scope", "public-advertised-standard"),
                              ("interpretation_policy", "ci-advertised-base-token-v1"),
                              ("source_contract", "cheaper-inference-public-models-v1"),
                              ("upstream_freshness", "unknown")),
                    capture="legacy", quote_column="legacy_policy", price_columns="forbidden",
                    legacy_policy="ci-advertised-base-token-v1"),
                SourceContract(
                    contract_id="cheaper-inference-public-models-v2",
                    location="https://api.cheaperinference.com/public/models",
                    metadata=(("catalog_scope", "public-advertised-standard"),
                              ("interpretation_policy", "ci-advertised-evidence-only-v2"),
                              ("source_contract", "cheaper-inference-public-models-v2"),
                              ("upstream_freshness", "unknown")),
                    capture="strict", quote_column="forbidden", price_columns="forbidden",
                    legacy_policy="ci-advertised-evidence-only-v2"),
            ),
        )
        record = contracts.CHEAPER_INFERENCE_RECORD
        self.assertEqual(record, expected)
        self.assertEqual(dict(record.contracts[0].metadata), V1_SOURCE_METADATA)
        self.assertEqual(dict(record.contracts[1].metadata), SOURCE_METADATA)
        self.assertEqual(contracts.PROVIDER_RECORDS, (record,))
        self.assertIs(type(contracts.PROVIDER_RECORDS), tuple)

    def test_legacy_policies_pinned(self):
        self.assertIs(type(contracts.LEGACY_POLICIES), MappingProxyType)
        self.assertEqual(dict(contracts.LEGACY_POLICIES), {
            "ci-advertised-base-token-v1":
                ("cheaper_inference.public.standard", "cheaper-inference-public-models-v1"),
            "ci-advertised-evidence-only-v2":
                ("cheaper_inference.public.standard", "cheaper-inference-public-models-v2"),
        })

    def test_shipped_registry_validates(self):
        self.assertEqual(contracts._validate_registry(), contracts.PROVIDER_RECORDS)

    def test_namespace_agreement_without_callbacks(self):
        calls = []

        class Hostile(str):
            def __eq__(self, other): calls.append("eq"); return False
            def __ne__(self, other): calls.append("ne"); return True
            def startswith(self, *args): calls.append("startswith"); return False
            def __str__(self): calls.append("str"); return "openrouter"
            __hash__ = str.__hash__

        class Lying(str):
            def __eq__(self, other): calls.append("eq"); return True
            def __ne__(self, other): calls.append("ne"); return False
            def startswith(self, *args): calls.append("startswith"); return True
            __hash__ = str.__hash__

        contents = ("cheaper_inference", "cheaper_inference.", "cheaper_inference.x", STREAM_ID,
                    "openrouter", "cheaper_inferenceX", "cheaper_inference_public", "")
        values = [*contents, *(Hostile(c) for c in contents), *(Lying(c) for c in contents), None, 5]

        def outcome(function):
            try:
                return function()
            except TypeError:
                return TypeError
        for value in values:
            with self.subTest(value=repr(value), kind=type(value).__name__):
                old = outcome(lambda: is_reserved_stream(value))
                new = outcome(lambda: contracts.reserved_record(value) is not None)
                self.assertEqual(old, new)
        self.assertEqual(calls, [])


# --------------------------------------------------------------------------- K.5


class GenericProviderProofTests(StorageDatabaseCase):
    """K.5 items 1-10. The trap covers generic-provider operations only (item 1)."""

    # Item 1 ---------------------------------------------------------------
    def test_item1_positive_control_cheaper_inference_trips_trap(self):
        _, _, v2, current = _ci_cases()
        with generic_registry(NP, PP), legacy_trap() as trap:
            with self.assertRaises(AssertionError) as caught:
                write_snapshot(self.connection, provider=STREAM_ID, started_at=STARTED,
                               completed_at=COMPLETED, source=v2, observations=(current,))
        self.assertEqual(str(caught.exception), "legacy dispatch")
        self.assertEqual(trap.call_count, 1)
        self.assertEqual(self.counts(), (0, 0))
        self.assertFalse(self.connection.in_transaction)

    def test_item1_legitimate_cheaper_inference_operations_invoke_legacy_validation(self):
        v1, old, v2, current = _ci_cases()
        real = contracts.run_legacy_policy
        with generic_registry(NP, PP), patch.object(contracts, "run_legacy_policy", wraps=real) as spy:
            write_snapshot(self.connection, provider=STREAM_ID, started_at=STARTED,
                           completed_at=COMPLETED, source=v2, observations=(current,))
            self.assertEqual(spy.call_count, 1)
            get_successful_history(self.connection, STREAM_ID)
            self.assertEqual(spy.call_count, 2)
            # Generic-provider operations through the same spy never reach it.
            self.write(NP)
            get_successful_history(self.connection, _stream(NP))
            self.assertEqual(spy.call_count, 2)

    # Item 2 ---------------------------------------------------------------
    def test_item2_positive_round_trip_on_all_six_readers(self):
        for record in (NP, PP):
            with self.subTest(record=record.provider_id):
                self.fresh()
                with generic_registry(NP, PP), legacy_trap() as trap:
                    stream = _stream(record)
                    empty = self.write(record, (), started=EARLY_STARTED, completed=EARLY_COMPLETED)
                    rows = tuple(_observation(record, oid) for oid in ("a", "b", "c"))
                    full = self.write(record, rows)
                    self.assertEqual(full.source, _source(record))
                    self.assertEqual(get_latest_successful_snapshot(self.connection, stream), full)
                    self.assertEqual(get_successful_snapshots(self.connection, stream), (empty, full))
                    history = get_successful_history(self.connection, stream)
                    self.assertEqual([snap for snap, _ in history], [empty, full])
                    self.assertEqual(history[0][1], ())
                    observed = get_snapshot_observations(self.connection, full.id)
                    self.assertEqual(history[1][1], observed)
                    self.assertEqual(get_snapshot_observations(self.connection, empty.id), ())
                    self.assertEqual(get_current_observations(self.connection, stream), observed)
                    self.assertEqual([r.offering_id for r in observed], ["a", "b", "c"])
                    for row, original in zip(observed, rows):
                        self.assertEqual(row.observed_at, OBSERVED)
                        self.assertEqual((row.input_per_million, row.output_per_million),
                                         (original.input_usd_per_million, original.output_usd_per_million))
                        self.assertEqual((row.conditions, row.source_record, row.advertised_quote),
                                         ({}, {"id": row.offering_id}, None))
                    offering, = get_offering_history(self.connection, provider=stream, offering_id="b")
                    self.assertEqual(offering, observed[1])
                    self.assertEqual(trap.call_count, 0)
                expected_prices = (Decimal("1.25"), Decimal("2.5")) if record is PP else (None, None)
                self.assertEqual((observed[0].input_per_million, observed[0].output_per_million),
                                 expected_prices)

    # Item 3 ---------------------------------------------------------------
    def _forge_snapshot(self, snapshot_id, how):
        if how == "location":
            self.connection.execute("UPDATE snapshots SET source_url=? WHERE id=?",
                                    ("https://example.invalid/forged", snapshot_id))
        else:
            metadata = dict(_source(NP).metadata, source_contract="forged")
            self.connection.execute("UPDATE snapshots SET source_metadata_json=? WHERE id=?",
                                    (storage._encode_json(metadata), snapshot_id))

    def test_item3_reader_specific_corruption(self):
        for how in ("location", "metadata"):
            # Forged contract on a non-empty snapshot: every reader that selects it.
            with self.subTest(case="non-empty snapshot contract", how=how):
                self.fresh()
                with generic_registry(NP, PP), legacy_trap() as trap:
                    full = self.write(NP, (_observation(NP, "a"),))
                    self._forge_snapshot(full.id, how)
                    stream = _stream(NP)
                    for name, reader in (
                            ("latest", lambda: get_latest_successful_snapshot(self.connection, stream)),
                            ("snapshots", lambda: get_successful_snapshots(self.connection, stream)),
                            ("history", lambda: get_successful_history(self.connection, stream)),
                            ("snapshot_observations", lambda: get_snapshot_observations(self.connection, full.id)),
                            ("current", lambda: get_current_observations(self.connection, stream)),
                            ("offering_history", lambda: get_offering_history(
                                self.connection, provider=stream, offering_id="a"))):
                        with self.subTest(reader=name), self.assertRaises(StorageError):
                            reader()
                    self.assertEqual(trap.call_count, 0)
            # Forged contract on an existing empty snapshot: readers that select it.
            with self.subTest(case="empty snapshot contract", how=how):
                self.fresh()
                with generic_registry(NP, PP), legacy_trap() as trap:
                    empty = self.write(NP, ())
                    self._forge_snapshot(empty.id, how)
                    stream = _stream(NP)
                    for name, reader in (
                            ("latest", lambda: get_latest_successful_snapshot(self.connection, stream)),
                            ("snapshots", lambda: get_successful_snapshots(self.connection, stream)),
                            ("history", lambda: get_successful_history(self.connection, stream)),
                            ("snapshot_observations", lambda: get_snapshot_observations(self.connection, empty.id)),
                            ("current", lambda: get_current_observations(self.connection, stream))):
                        with self.subTest(reader=name), self.assertRaises(StorageError):
                            reader()
                    self.assertEqual(trap.call_count, 0)
        quote_json = storage._encode_quote(_quote())
        forgeries = (
            ("np quote", NP, "advertised_quote_json", quote_json),
            ("pp quote", PP, "advertised_quote_json", quote_json),
            ("np input price", NP, "input_per_million", "1e0"),
            ("np output price", NP, "output_per_million", "1e0"),
        )
        for label, record, column, value in forgeries:
            # Forged observation columns: only readers that select the forged row.
            with self.subTest(case=label):
                self.fresh()
                with generic_registry(NP, PP), legacy_trap() as trap:
                    full = self.write(record, (_observation(record, "a"), _observation(record, "b")))
                    self.connection.execute(
                        f"UPDATE observations SET {column}=? WHERE offering_id='a'", (value,))
                    stream = _stream(record)
                    for name, reader in (
                            ("history", lambda: get_successful_history(self.connection, stream)),
                            ("snapshot_observations", lambda: get_snapshot_observations(self.connection, full.id)),
                            ("current", lambda: get_current_observations(self.connection, stream)),
                            ("offering_history", lambda: get_offering_history(
                                self.connection, provider=stream, offering_id="a"))):
                        with self.subTest(reader=name), self.assertRaises(StorageError):
                            reader()
                    # Snapshot-only readers keep their existing scope.
                    self.assertEqual(get_latest_successful_snapshot(self.connection, stream), full)
                    self.assertEqual(get_successful_snapshots(self.connection, stream), (full,))
                    # Offering history never selects the forged offering's sibling row.
                    self.assertEqual(len(get_offering_history(
                        self.connection, provider=stream, offering_id="b")), 1)
                    self.assertEqual(trap.call_count, 0)

    # Item 4 ---------------------------------------------------------------
    def test_item4_strict_capture_applies_to_generic_provider(self):
        class SubDatetime(datetime):
            pass

        class Zone(tzinfo):
            def utcoffset(self, value): return timedelta(0)
            def dst(self, value): return timedelta(0)

        class SubString(str):
            pass

        class SubObservation(CatalogObservation):
            pass

        class SubDict(dict):
            pass

        class SubList(list):
            pass
        base = _observation(NP)
        cases = {
            "datetime subclass observed_at": dict(rows=(_observation(NP, observed_at=SubDatetime(2026, 9, 19, 12, 30, tzinfo=timezone.utc)),)),
            "non-timezone tzinfo": dict(rows=(_observation(NP, observed_at=datetime(2026, 9, 19, 12, 30, tzinfo=Zone())),)),
            "datetime subclass started_at": dict(started=SubDatetime(2026, 9, 19, 12, 0, tzinfo=timezone.utc)),
            "str-subclass offering": dict(rows=(_observation(NP, SubString("a"), raw_offering={"id": "a"}),)),
            "CatalogObservation subclass": dict(rows=(SubObservation(*(getattr(base, f) for f in base.__dataclass_fields__)),)),
            "dict-subclass evidence": dict(rows=(_observation(NP, raw_offering=SubDict(id="a")),)),
            "list-subclass evidence": dict(rows=(_observation(NP, unsupported_pricing={"x": SubList()}),)),
            "float evidence": dict(rows=(_observation(NP, raw_offering={"id": "a", "x": 1.5}),)),
        }
        with generic_registry(NP, PP), legacy_trap() as trap:
            for label, change in cases.items():
                with self.subTest(label):
                    rows = change.pop("rows", None)
                    self.assert_rejected_before_begin(TypeError, lambda: self.write(NP, rows, **change))
            self.assertEqual(trap.call_count, 0)

    # Item 5 ---------------------------------------------------------------
    def test_item5_namespace_enforcement(self):
        class SubString(str):
            pass
        with generic_registry(NP, PP), legacy_trap() as trap:
            for provider in ("mpw_gate1a_np", "mpw_gate1a_np.other"):
                with self.subTest(provider=provider):
                    self.assert_rejected_before_begin(ValueError, lambda: self.write(NP, (), provider=provider))
            with self.subTest("str-subclass snapshot provider"):
                self.assert_rejected_before_begin(
                    TypeError, lambda: self.write(NP, (), provider=SubString(_stream(NP))))
            with self.subTest("str-subclass observation provider"):
                self.assert_rejected_before_begin(TypeError, lambda: self.write(
                    NP, (_observation(NP, provider=SubString(_stream(NP))),)))
            self.assertEqual(trap.call_count, 0)

    # Item 6 ---------------------------------------------------------------
    def test_item6_snapshot_authority_and_contract_enforcement(self):
        or_source = SourceMetadata("fixture:openrouter", {"capture": "test"})
        source = _source(NP)
        value_cases = {
            "openrouter observation under generic snapshot": lambda: self.write(
                NP, (_observation(NP, provider="openrouter"),)),
            "generic observation under openrouter snapshot": lambda: write_snapshot(
                self.connection, provider="openrouter", started_at=STARTED, completed_at=COMPLETED,
                source=or_source, observations=(_observation(NP, source=or_source),)),
            "observation source differs": lambda: self.write(
                NP, (_observation(NP, source=SourceMetadata(source.location, dict(source.metadata, extra="x"))),)),
            "wrong location": lambda: self.write(
                NP, (), source=SourceMetadata("https://example.invalid/other", dict(source.metadata))),
            "missing metadata": lambda: self.write(
                NP, (), source=SourceMetadata(source.location, {"source_contract": "mpw_gate1a_np-v1"})),
            "extra metadata": lambda: self.write(
                NP, (), source=SourceMetadata(source.location, dict(source.metadata, extra="x"))),
            "altered metadata": lambda: self.write(
                NP, (), source=SourceMetadata(source.location, dict(source.metadata, interpretation_policy="x"))),
        }
        with generic_registry(NP, PP), legacy_trap() as trap:
            for label, call in value_cases.items():
                with self.subTest(label):
                    self.assert_rejected_before_begin(ValueError, call)
            with self.subTest("non-str metadata value"):
                self.assert_rejected_before_begin(TypeError, lambda: self.write(
                    NP, (), source=SourceMetadata(source.location, dict(source.metadata, count=1))))
            self.assertEqual(trap.call_count, 0)

    # Item 7 ---------------------------------------------------------------
    def test_item7_quote_and_price_rules(self):
        with generic_registry(NP, PP), legacy_trap() as trap:
            for record in (NP, PP):
                with self.subTest("quote presence", record=record.provider_id):
                    self.assert_rejected_before_begin(ValueError, lambda: self.write(
                        record, (_observation(record, advertised_quote=_quote()),)))
            for field in ("input_usd_per_million", "output_usd_per_million"):
                with self.subTest("np price", field=field):
                    self.assert_rejected_before_begin(ValueError, lambda: self.write(
                        NP, (_observation(NP, **{field: Decimal("1")}),)))
            self.assertEqual(trap.call_count, 0)

    # Item 8 ---------------------------------------------------------------
    def test_item8_rollback_preserves_seeded_history(self):
        _, _, v2, current = _ci_cases()
        with generic_registry(NP, PP):
            # Cheaper Inference seed write and history comparisons: outside the trap.
            write_snapshot(self.connection, provider=STREAM_ID, started_at=STARTED,
                           completed_at=COMPLETED, source=v2, observations=(current,))
            ci_history = get_successful_history(self.connection, STREAM_ID)
            with legacy_trap() as trap:
                self.write(NP, (_observation(NP, "a"), _observation(NP, "b")))
                np_history = get_successful_history(self.connection, _stream(NP))
            seeded_counts = self.counts()

            def denied(action, arg1, *_rest):
                if action == sqlite3.SQLITE_INSERT and arg1 == "observations":
                    return sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_OK

            def denied_commit(action, arg1, *_rest):
                if action == sqlite3.SQLITE_TRANSACTION and arg1 == "COMMIT":
                    return sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_OK
            late = (_observation(NP, "c", observed_at=LATE_OBSERVED),)
            attempts = (
                ("duplicate offering", sqlite3.IntegrityError, None,
                 (_observation(NP, "c", observed_at=LATE_OBSERVED), _observation(NP, "c", observed_at=LATE_OBSERVED))),
                ("denied observation insert", sqlite3.DatabaseError, denied, late),
                ("denied commit", sqlite3.DatabaseError, denied_commit, late),
            )
            for label, error, authorizer, rows in attempts:
                with self.subTest(label):
                    with legacy_trap() as trap:
                        if authorizer is not None:
                            self.connection.set_authorizer(authorizer)
                        try:
                            with self.assertRaises(error):
                                self.write(NP, rows, started=LATE_STARTED, completed=LATE_COMPLETED)
                        finally:
                            self.connection.set_authorizer(None)
                        self.assertFalse(self.connection.in_transaction)
                        self.assertEqual(get_successful_history(self.connection, _stream(NP)), np_history)
                        self.assertEqual(trap.call_count, 0)
                    self.assertEqual(get_successful_history(self.connection, STREAM_ID), ci_history)
                    self.assertEqual(self.counts(), seeded_counts)

    # Item 9 ---------------------------------------------------------------
    def test_item9_price_rule_isolation(self):
        with generic_registry(NP, PP), legacy_trap() as trap:
            self.write(NP, (_observation(NP, "a"),))
            self.write(PP, (_observation(PP, "a"),))
            np_before = get_successful_history(self.connection, _stream(NP))
            pp_before = get_successful_history(self.connection, _stream(PP))
            self.assertEqual(trap.call_count, 0)
        np_permitted = _record("mpw_gate1a_np", price="permitted")
        pp_forbidden = _record("mpw_gate1a_pp", price="forbidden")
        with generic_registry(np_permitted, PP), legacy_trap() as trap:
            self.assertEqual(get_successful_history(self.connection, _stream(PP)), pp_before)
            self.assertEqual(get_successful_history(self.connection, _stream(NP)), np_before)
            self.assertEqual(trap.call_count, 0)
        with generic_registry(NP, pp_forbidden), legacy_trap() as trap:
            stream = _stream(PP)
            snapshot = get_latest_successful_snapshot(self.connection, stream)
            for reader in (lambda: get_successful_history(self.connection, stream),
                           lambda: get_snapshot_observations(self.connection, snapshot.id),
                           lambda: get_current_observations(self.connection, stream),
                           lambda: get_offering_history(self.connection, provider=stream, offering_id="a")):
                with self.assertRaises(StorageError):
                    reader()
            self.assertEqual(get_successful_history(self.connection, _stream(NP)), np_before)
            self.assertEqual(trap.call_count, 0)

    # Item 10 --------------------------------------------------------------
    def test_item10_production_paths_unchanged_with_test_records_present(self):
        v1, old, v2, current = _ci_cases()
        or_source = SourceMetadata("fixture:openrouter", {"capture": "test"})
        with generic_registry(NP, PP):
            first = write_snapshot(self.connection, provider=STREAM_ID, started_at=STARTED,
                                   completed_at=COMPLETED, source=v1, observations=(old,))
            row, = get_snapshot_observations(self.connection, first.id)
            self.assertEqual(row.advertised_quote, old.advertised_quote)
            self.assertIsNotNone(row.advertised_quote)
            second = write_snapshot(self.connection, provider=STREAM_ID, started_at=LATE_STARTED,
                                    completed_at=LATE_COMPLETED, source=v2,
                                    observations=(CatalogObservation(
                                        STREAM_ID, current.offering_id, LATE_OBSERVED, current.source,
                                        None, None, current.unsupported_pricing, current.raw_offering),))
            row, = get_snapshot_observations(self.connection, second.id)
            self.assertIsNone(row.advertised_quote)
            for provider in ("openrouter", "cheaper_inference_public"):
                with self.subTest(provider=provider):
                    snap = write_snapshot(self.connection, provider=provider, started_at=STARTED,
                                          completed_at=COMPLETED, source=or_source,
                                          observations=(CatalogObservation(
                                              provider, "m", OBSERVED, or_source, Decimal("1.25"),
                                              Decimal("2.5"), {}, {"id": "m"}),))
                    row, = get_snapshot_observations(self.connection, snap.id)
                    self.assertEqual((row.input_per_million, row.output_per_million),
                                     (Decimal("1.25"), Decimal("2.5")))
            with self.assertRaises(ValueError):
                write_snapshot(self.connection, provider="openrouter", started_at=STARTED,
                               completed_at=COMPLETED, source=or_source,
                               observations=(CatalogObservation(
                                   "openrouter", "q", OBSERVED, or_source, None, None, {}, {"id": "q"},
                                   _quote()),))


if __name__ == "__main__":
    unittest.main()
