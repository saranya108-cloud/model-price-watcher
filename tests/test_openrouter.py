"""Synthetic fixtures exercise catalog normalization without network access."""

import json
import unittest
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path

from model_price_watcher.models import SourceMetadata
from model_price_watcher.providers.openrouter import parse_catalog


FIXTURES = Path(__file__).parent / "fixtures"
OBSERVED_AT = datetime(2026, 9, 19, 12, 0, tzinfo=timezone.utc)


class OpenRouterTests(unittest.TestCase):
    def setUp(self):
        self.source = SourceMetadata("fixture:openrouter", {"capture": "test"})

    def parse(self, text):
        result = parse_catalog(text, observed_at=OBSERVED_AT, source=self.source)
        for row in result.observations:
            for amount in (row.input_usd_per_million, row.output_usd_per_million):
                if amount is not None:
                    self.assertTrue(amount.is_finite())
        return result

    def fixture(self, name="openrouter_catalog.json"):
        return (FIXTURES / name).read_text(encoding="utf-8")

    def test_exact_decimal_scaling_independent_of_context(self):
        with localcontext() as context:
            context.prec = 5
            result = self.parse(self.fixture())
        self.assertTrue(result.accepted)
        first = result.observations[0]
        self.assertEqual(first.input_usd_per_million, Decimal("1.2345678901234567890123456789"))
        self.assertEqual(first.output_usd_per_million, Decimal("4.5"))
        self.assertEqual(result.observations[3].input_usd_per_million, Decimal("1"))
        self.assertEqual(result.observations[3].output_usd_per_million, Decimal("2"))

    def test_zero_unknown_and_exact_offering_identity(self):
        rows = self.parse(self.fixture()).observations
        self.assertEqual([row.offering_id for row in rows], [
            "example/model", "example/model:free", "example/model:extended",
            "Example/Model", "example/unknown",
        ])
        self.assertEqual(rows[1].input_usd_per_million, Decimal(0))
        self.assertEqual(rows[1].output_usd_per_million, Decimal(0))
        for row in (rows[2], rows[4]):
            self.assertIsNone(row.input_usd_per_million)
            self.assertIsNone(row.output_usd_per_million)

    def test_evidence_unsupported_conditions_and_metadata(self):
        text = self.fixture()
        result = self.parse(text)
        row = result.observations[0]
        self.assertEqual(result.raw_catalog, text)
        self.assertEqual(row.raw_offering["canonical_slug"], "example/model-2026")
        self.assertEqual(row.unsupported_pricing, {
            "input_cache_read": "0.0000002", "request": "0.01",
            "image": "0.02", "discount": {"batch_only": True},
        })
        self.assertEqual(row.provider, "openrouter")
        self.assertEqual(row.observed_at, OBSERVED_AT)
        self.assertEqual(row.source, self.source)
        self.assertEqual(result.observed_at, OBSERVED_AT)
        self.assertEqual(result.source, self.source)
        self.assertIn("unsupported_pricing", {d.code for d in result.diagnostics})
        self.assertEqual(self.parse(text), result)
        self.source.metadata["capture"] = "changed"
        self.assertEqual(result.source.metadata["capture"], "test")

    def test_malformed_negative_and_nonfinite_prices(self):
        result = self.parse(self.fixture("openrouter_malformed.json"))
        self.assertTrue(result.accepted)
        for row in result.observations:
            self.assertIsNone(row.input_usd_per_million)
            self.assertIsNone(row.output_usd_per_million)
        monetary = [d for d in result.diagnostics if d.code != "unsupported_pricing"]
        self.assertEqual(
            [(d.code, d.path, d.raw_value, d.offering_id) for d in monetary],
            [
                ("negative_price", "$.data[0].pricing.prompt", "-0.001", "example/negative"),
                ("non_finite_price", "$.data[0].pricing.completion", "-Infinity", "example/negative"),
                ("non_finite_price", "$.data[1].pricing.prompt", "NaN", "example/nonfinite"),
                ("non_finite_price", "$.data[1].pricing.completion", "Infinity", "example/nonfinite"),
                ("malformed_price", "$.data[2].pricing.prompt", "not-money", "example/malformed"),
                ("malformed_price", "$.data[2].pricing.completion", True, "example/malformed"),
                ("malformed_price", "$.data[3].pricing.prompt", {}, "example/container"),
                ("malformed_price", "$.data[3].pricing.completion", [], "example/container"),
                ("negative_price", '$.data[4].pricing["request"]', "-1", "example/unsupported"),
                ("non_finite_price", '$.data[4].pricing["image"]', "NaN", "example/unsupported"),
            ],
        )

    def test_price_failures_are_independent_of_invalid_operation_trap(self):
        cases = [
            ("1e999999999999999994", "malformed_price"),
            ("1e9999999999999999999", "malformed_price"),
            ("not-money", "malformed_price"),
            ("NaN", "non_finite_price"),
            ("Infinity", "non_finite_price"),
            ("-Infinity", "non_finite_price"),
        ]
        for value, code in cases:
            results = []
            for trapped in (True, False):
                with self.subTest(value=value, trapped=trapped), localcontext() as context:
                    context.traps[InvalidOperation] = trapped
                    context.clear_flags()
                    result = self.parse(json.dumps({"data": [{
                        "id": "example/context", "pricing": {"prompt": value, "completion": "0.000002"},
                    }]}))
                    self.assertTrue(result.accepted)
                    self.assertIsNone(result.observations[0].input_usd_per_million)
                    self.assertEqual(result.observations[0].output_usd_per_million, Decimal("2"))
                    self.assertEqual(
                        [(d.code, d.path, d.raw_value, d.offering_id) for d in result.diagnostics],
                        [(code, "$.data[0].pricing.prompt", value, "example/context")],
                    )
                    self.assertEqual(context.traps[InvalidOperation], trapped)
                    self.assertFalse(context.flags[InvalidOperation])
                    results.append(result)
            if len(results) == 2:
                self.assertEqual(results[0], results[1])

    def test_invalid_structure_rejects_entire_catalog(self):
        invalid = [None, [], {}, {"data": {}}, {"data": [None]},
                   {"data": [{}]}, {"data": [{"id": ""}]},
                   {"data": [{"id": 4}]}, {"data": [{"id": "   "}]},
                   {"data": [{"id": "x", "pricing": []}]},
                   {"data": [{"id": "x", "pricing": None}]},
                   {"data": [{"id": "valid"}, {"id": "bad", "pricing": 5}]}]
        for payload in invalid:
            with self.subTest(payload=payload):
                result = self.parse(json.dumps(payload))
                self.assertFalse(result.accepted)
                self.assertEqual(result.observations, ())
                self.assertIn("invalid_structure", {d.code for d in result.diagnostics})

    def test_duplicate_identity_rejected_without_aliasing_variants(self):
        result = self.parse('{"data":[{"id":"same"},{"id":"same"}]}')
        self.assertFalse(result.accepted)
        self.assertEqual(result.observations, ())
        self.assertEqual(result.diagnostics[0].code, "duplicate_offering")
        self.assertEqual(result.diagnostics[0].path, "$.data[1].id")

    def test_bad_json_and_duplicate_keys_rejected(self):
        for text in ('{', '{"data":[],"data":[]}',
                     '{"data":[{"id":"x","pricing":{"prompt":"0","prompt":"1"}}]}',
                     '{"data":[],"extra":NaN}'):
            with self.subTest(text=text):
                result = self.parse(text)
                self.assertFalse(result.accepted)
                self.assertEqual(result.observations, ())
                self.assertEqual(result.diagnostics[0].code, "invalid_json")
                self.assertEqual(result.raw_catalog, text)

    def test_empty_catalog_is_valid(self):
        result = self.parse('{"data":[]}')
        self.assertTrue(result.accepted)
        self.assertEqual(result.observations, ())
        self.assertEqual(result.diagnostics, ())

    def test_requires_explicit_aware_observation_time(self):
        with self.assertRaises(ValueError):
            parse_catalog(self.fixture(), observed_at=datetime(2026, 9, 19), source=self.source)
        with self.assertRaises(TypeError):
            parse_catalog(self.fixture(), source=self.source)

    def test_price_grammar_and_extreme_exponents(self):
        for value in ("", " 1", "1_000", "1,000", False):
            with self.subTest(value=value):
                result = self.parse(json.dumps({"data": [{"id": "x", "pricing": {"prompt": value}}]}))
                self.assertIsNone(result.observations[0].input_usd_per_million)
                self.assertEqual(result.diagnostics[0].code, "malformed_price")
        result = self.parse('{"data":[{"id":"x","pricing":{"prompt":"1e999999","completion":"1e-999999"}}]}')
        self.assertEqual(result.observations[0].input_usd_per_million, Decimal("1e1000005"))
        self.assertEqual(result.observations[0].output_usd_per_million, Decimal("1e-999993"))


if __name__ == "__main__":
    unittest.main()
