import json
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from model_price_watcher.models import SourceMetadata

SOURCE = SourceMetadata("https://api.cheaperinference.com/public/models", {
    "catalog_scope": "public-advertised-standard",
    "source_contract": "cheaper-inference-public-models-v1",
    "interpretation_policy": "ci-advertised-base-token-v1",
    "upstream_freshness": "unknown",
})
AT = datetime(2026, 9, 23, tzinfo=timezone.utc)

def row(**changes):
    return dict(dict(id="A", model_type="text", input_per_million="1.25",
                output_per_million="10", discount_percent="0"), **changes)

class ParserContractTests(unittest.TestCase):
    def test_exact_advertised_quote_without_legacy_prices(self):
        from model_price_watcher.providers.cheaper_inference import parse_catalog
        result = parse_catalog(json.dumps({"models": [row()]}), observed_at=AT, source=SOURCE)
        self.assertTrue(result.accepted)
        item, = result.observations
        self.assertIsNone(item.input_usd_per_million)
        self.assertIsNone(item.output_usd_per_million)
        self.assertEqual(item.advertised_quote.input_usd_per_million, Decimal("1.25"))
        self.assertEqual(item.advertised_quote.output_usd_per_million, Decimal("10"))
        self.assertEqual(item.advertised_quote.basis.conditions_key,
                         'ci-basis-v1:["o",[["model_type",["s","text"]]]]')

    def parse(self, rows):
        from model_price_watcher.providers.cheaper_inference import parse_catalog
        return parse_catalog(json.dumps({"models": rows}), observed_at=AT, source=SOURCE)

    def test_zdr_and_opaque_rows_preserve_presence(self):
        for changes in ({"zero_data_retention": True, "zero_data_retention_route": None},
                        {"new_field": 1}, {"media_unit_price": "0"},
                        {"model_type": "image"}, {"input_token_price_threshold": 0}):
            with self.subTest(changes=changes):
                result = self.parse([row(**changes), row(id="B")])
                self.assertTrue(result.accepted)
                self.assertIsNone(result.observations[0].advertised_quote)
                self.assertIsNotNone(result.observations[1].advertised_quote)
        self.assertIn("ambiguous_retention_view", [d.code for d in
                      self.parse([row(zero_data_retention=True)]).diagnostics])

    def test_structure_rejects_entire_catalog(self):
        for changes in ({"output_per_million": None}, {"input_per_million": 1},
                        {"zero_data_retention": None}, {"zero_data_retention": "false"},
                        {"zero_data_retention_route": "rail"},
                        {"input_token_price_threshold": True},
                        {"input_token_price_threshold": 1.5},
                        {"model_type": "other"}, {"aliases": [1]},
                        {"media_prices": {"image": 1}}, {"id": "\x00"}, {"id": " "},
                        {"provider_name": "\ud800"}, {"supports_vision": 1},
                        {"above_threshold": {"input_per_million": 1}}):
            with self.subTest(changes=changes):
                result = self.parse([row(id="valid"), row(**changes)])
                self.assertFalse(result.accepted)
                self.assertEqual(result.observations, ())

    def test_json_and_envelope_rejection(self):
        from model_price_watcher.providers.cheaper_inference import parse_catalog
        for raw in ('{"models":[],"models":[]}', '{"models":[],"next":null}',
                    '{"data":[]}', '{"models":[NaN]}', '{"models":[{"x":1,"x":2}]}'):
            self.assertFalse(parse_catalog(raw, observed_at=AT, source=SOURCE).accepted)
        self.assertFalse(self.parse([row(), row()]).accepted)

    def test_unknown_money_is_not_zero(self):
        for price in ("bad", " 1", "1_0", "-1", "NaN", "Infinity", "1e999999999999999999999999"):
            result = self.parse([row(input_per_million=price)])
            self.assertTrue(result.accepted)
            self.assertIsNone(result.observations[0].advertised_quote.input_usd_per_million)
        self.assertEqual(self.parse([row(input_per_million="-0")]).observations[0].advertised_quote.input_usd_per_million, 0)

    def test_integral_threshold_equivalence(self):
        a = self.parse([row(input_token_price_threshold=100)]).observations[0]
        b = self.parse([row(input_token_price_threshold=100.0)]).observations[0]
        self.assertEqual(a.advertised_quote, b.advertised_quote)

    def test_literal_canonical_keys(self):
        from model_price_watcher.providers.cheaper_inference import _canonical_conditions_key as key
        cases = [({}, 'ci-basis-v1:["o",[]]'),
                 ({"x": None}, 'ci-basis-v1:["o",[["x",["n"]]]]'),
                 ({"x": "1"}, 'ci-basis-v1:["o",[["x",["s","1"]]]]'),
                 ({"x": 1}, 'ci-basis-v1:["o",[["x",["d","1e0"]]]]'),
                 ({"x": Decimal("1.00")}, 'ci-basis-v1:["o",[["x",["d","1e0"]]]]'),
                 ({"x": Decimal("-0")}, 'ci-basis-v1:["o",[["x",["d","0"]]]]'),
                 ({"x": True}, 'ci-basis-v1:["o",[["x",["b",true]]]]'),
                 ({"x": [1, 2]}, 'ci-basis-v1:["o",[["x",["a",[["d","1e0"],["d","2e0"]]]]]]'),
                 ({"é": 1, "a": 2}, 'ci-basis-v1:["o",[["a",["d","2e0"]],["\\u00e9",["d","1e0"]]]]'),
                 ({"x": "😀\n\x00"}, 'ci-basis-v1:["o",[["x",["s","\\ud83d\\ude00\\n\\u0000"]]]]')]
        for value, expected in cases:
            self.assertEqual(key(value), expected)
        self.assertNotEqual(key({"x": [1, 2]}), key({"x": [2, 1]}))
        self.assertNotEqual(key({}), key({"x": None}))
        self.assertEqual(key({"b": 1, "a": 2}), key({"a": 2, "b": 1}))

    def test_context_large_numbers_and_canonical_exclusions(self):
        from decimal import localcontext, Inexact
        from model_price_watcher.providers.cheaper_inference import _canonical_conditions_key as key
        expected = 'ci-basis-v1:["o",[["x",["d","1.' + '1' * 4300 + 'e4300"]]]]'
        with localcontext() as ctx:
            ctx.prec = 2
            ctx.traps[Inexact] = True
            before = ctx.copy()
            self.assertEqual(key({"x": Decimal('1' * 4301)}), expected)
            self.assertEqual(key({"x": int(Decimal('1' * 4301))}), expected)
            self.assertEqual(key({"x": Decimal('1e999999')}), 'ci-basis-v1:["o",[["x",["d","1e999999"]]]]')
            self.assertEqual(ctx.flags, before.flags)
        excluded = 'id input_per_million output_per_million discount_percent reference_input_per_million reference_output_per_million reference_cache_read_per_million reference_cache_write_per_million reference_image_output_per_million reference_media_input_unit_price reference_media_unit_price reference_media_prices cache_read_per_million cache_write_per_million logo_url aliases'.split()
        for name in excluded:
            self.assertEqual(key({name: 'anything'}), 'ci-basis-v1:["o",[]]')
            self.assertNotEqual(key({"x": {name: 'anything'}}), key({"x": {}}))

    def test_depth_and_direct_validation(self):
        from dataclasses import replace
        from model_price_watcher.providers.cheaper_inference import validate_public_observation, _canonical_conditions_key
        nested = {}
        for _ in range(60):
            nested = {"x": nested}
        self.assertTrue(self.parse([row(new_field=nested)]).accepted)
        self.assertTrue(_canonical_conditions_key(nested))
        nested = {"x": {"x": nested}}
        self.assertFalse(self.parse([row(new_field=nested)]).accepted)
        item = self.parse([row()]).observations[0]
        validate_public_observation(item, source=SOURCE)
        with self.assertRaises(ValueError):
            validate_public_observation(replace(item, advertised_quote=None), source=SOURCE)
        with self.assertRaises(ValueError):
            validate_public_observation(replace(item, input_usd_per_million=Decimal(1)), source=SOURCE)

    def test_direct_wrong_types_are_type_errors(self):
        from dataclasses import replace
        from model_price_watcher.providers.cheaper_inference import validate_public_observation
        item = self.parse([row()]).observations[0]
        for changes in ({'provider': 1}, {'unsupported_pricing': []}, {'raw_offering': []},
                        {'advertised_quote': True}, {'raw_offering': dict(item.raw_offering, reasoning_capability_mode=True)}):
            with self.subTest(changes=changes), self.assertRaises(TypeError):
                validate_public_observation(replace(item, **changes), source=SOURCE)

    def test_fixtures_empty_catalog_and_exact_ids(self):
        from pathlib import Path
        from model_price_watcher.providers.cheaper_inference import parse_catalog
        fixture = Path(__file__).parent / 'fixtures'
        good = parse_catalog((fixture/'cheaper_inference_public_catalog.json').read_text(), observed_at=AT, source=SOURCE)
        self.assertTrue(good.accepted)
        self.assertEqual(good.observations[0].offering_id, 'fixture/model-A')
        bad = parse_catalog((fixture/'cheaper_inference_public_malformed.json').read_text(), observed_at=AT, source=SOURCE)
        self.assertFalse(bad.accepted)
        self.assertEqual(bad.observations, ())
        empty = self.parse([])
        self.assertTrue(empty.accepted)
        self.assertEqual(empty.observations, ())
        identities = ('A', 'a', 'A:free', ' A ', 'vendor/A')
        result = self.parse([row(id=i, aliases=['A']) for i in identities])
        self.assertEqual(tuple(r.offering_id for r in result.observations), identities)

    def test_source_contracts_and_detached_evidence(self):
        from model_price_watcher.providers.cheaper_inference import parse_catalog
        for source in (SourceMetadata(SOURCE.location+'?zdr=true', SOURCE.metadata),
                       SourceMetadata(SOURCE.location, {}),
                       SourceMetadata(SOURCE.location, dict(SOURCE.metadata, extra='x'))):
            with self.assertRaises(ValueError):
                parse_catalog('{"models":[]}', observed_at=AT, source=source)
        source = SourceMetadata(SOURCE.location, dict(SOURCE.metadata))
        result = parse_catalog(json.dumps({'models': [row()]}), observed_at=AT, source=source)
        source.metadata['upstream_freshness'] = 'changed'
        self.assertEqual(result.source.metadata['upstream_freshness'], 'unknown')
        observation = result.observations[0]
        observation.raw_offering['model_type'] = 'changed'
        self.assertEqual(observation.unsupported_pricing['retained_fields']['model_type'], 'text')

    def test_all_known_optional_types_and_threshold_rules(self):
        from model_price_watcher.providers.cheaper_inference import _NULLABLE_STRINGS, _BOOLS, _INTS, _MAPS
        # Each category is exercised at its public parser seam.
        for name in _NULLABLE_STRINGS | _BOOLS | _INTS | _MAPS:
            with self.subTest(field=name):
                self.assertFalse(self.parse([row(**{name: []})]).accepted)
        for changes in ({'input_per_million_above_threshold': '3'}, {'above_threshold': {'input_per_million': '3'}},
                        {'image_pricing_unit': 'image'}, {'media_prices': {'small': '0'}},
                        {'input_token_price_threshold': -1}):
            result = self.parse([row(**changes)])
            self.assertTrue(result.accepted)
            self.assertIsNone(result.observations[0].advertised_quote)
        result = self.parse([row(input_token_price_threshold=100,
                                above_threshold={'input_token_price_threshold': 100, 'input_per_million': '4'})])
        self.assertIsNotNone(result.observations[0].advertised_quote)

    def test_canonical_invalid_types_and_decimal_context(self):
        from decimal import localcontext, InvalidOperation, Overflow, Inexact
        from model_price_watcher.providers.cheaper_inference import _canonical_conditions_key as key, parse_catalog
        cycle = {}
        cycle['x'] = cycle
        for value in ({'x': 1.0}, {'x': Decimal('NaN')}, {'x': '\ud800'}, {1: 'x'}, cycle):
            with self.assertRaises((TypeError, ValueError)):
                key(value)
        with localcontext() as ctx:
            ctx.prec, ctx.Emax, ctx.Emin = 2, 9, -9
            for signal in ctx.traps:
                ctx.traps[signal] = True
            before = ctx.copy()
            raw = '{"models":[{"id":"A","model_type":"text","input_per_million":"123456789.123456789","output_per_million":"1e999999","discount_percent":"0","input_token_price_threshold":' + '1'*4301 + '}]}'
            result = parse_catalog(raw, observed_at=AT, source=SOURCE)
            self.assertTrue(result.accepted)
            quote = result.observations[0].advertised_quote
            self.assertEqual(quote.input_usd_per_million, Decimal('123456789.123456789'))
            self.assertIn('1.'+'1'*4300+'e4300', quote.basis.conditions_key)
            self.assertEqual((ctx.prec, ctx.Emax, ctx.Emin, ctx.flags, ctx.traps),
                             (before.prec, before.Emax, before.Emin, before.flags, before.traps))
