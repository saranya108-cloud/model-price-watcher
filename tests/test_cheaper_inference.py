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
    def test_v1_complete_rejection_results_golden(self):
        """Independent HEAD baseline: atomic rejection, diagnostics and raw/source."""
        import hashlib
        from dataclasses import asdict
        changes = ({'zero_data_retention_route': 'seller'}, {'zero_data_retention_route': 'rail'},
                   {'zero_data_retention_route': 'discounted'}, {'zero_data_retention_route': 'future-route'},
                   {'image_pricing_unit': 'second'}, {'image_pricing_unit': 'future-unit'},
                   {'above_threshold': {'applies_when': 'prompt_tokens_above_threshold'}},
                   {'above_threshold': {'applies_when': 'unknown-condition'}},
                   {'model_type': 'future-model'}, {'input_per_million': 1}, {'id': '\x00'})
        results = [self.parse([row(id='good'), row(**change)]) for change in changes]
        self.assertTrue(all(not r.accepted and not r.observations for r in results))
        encoded = json.dumps([asdict(r) for r in results], default=str, sort_keys=True,
                             ensure_ascii=True, separators=(',', ':')).encode()
        self.assertEqual(hashlib.sha256(encoded).hexdigest(),
                         '4ab3c9d7ce408d34a5f56d17b3d53f514fb27e6c1217ac4088d6ff6af1ee80f6')

    def test_v1_complete_result_golden(self):
        """Pinned before adaptation: includes raw/source/conditions/quotes/diagnostics."""
        import hashlib
        from dataclasses import asdict
        base = dict(model_type='text', input_per_million='1.2500',
                    output_per_million='10', discount_percent='0')
        changes = ({}, {'zero_data_retention': True}, {'new_field': {'x': [1, None]}},
                   {'model_type': 'image'},
                   {'input_token_price_threshold': 100, 'above_threshold': {'input_per_million': '4'}},
                   {'media_prices': {'image': '1'}}, {'output_per_million': 'bad'},
                   {'input_per_million': '?', 'output_per_million': '?'})
        result = self.parse([dict(base, id='golden-'+str(i), **c) for i, c in enumerate(changes)])
        encoded = json.dumps(asdict(result), default=str, sort_keys=True,
                             ensure_ascii=True, separators=(',', ':')).encode()
        self.assertEqual(hashlib.sha256(encoded).hexdigest(),
                         'e20878984b8c2ebdc100776cf9902c6752d2ca0ac7a4c78a771bf5547cffdbec')

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


V2_SOURCE = SourceMetadata(SOURCE.location, dict(SOURCE.metadata,
    source_contract='cheaper-inference-public-models-v2',
    interpretation_policy='ci-advertised-evidence-only-v2'))


class EvidenceOnlyV2Tests(unittest.TestCase):
    def test_v2_custom_timezone_rejected_without_executing_mutation(self):
        from dataclasses import replace
        from datetime import timedelta, tzinfo
        from model_price_watcher.providers.cheaper_inference import parse_catalog, validate_public_observation
        quote = self.parse([row()], SOURCE).observations[0].advertised_quote
        calls = []
        class MutatingZone(tzinfo):
            def utcoffset(self, value):
                calls.append('utcoffset')
                object.__setattr__(item, 'advertised_quote', quote)
                return timedelta(0)
            def dst(self, value):
                calls.append('dst')
                object.__setattr__(item, 'advertised_quote', quote)
                return timedelta(0)
            def tzname(self, value):
                calls.append('tzname')
                object.__setattr__(item, 'advertised_quote', quote)
                return 'hostile'
        stamp = datetime(2026, 9, 23, tzinfo=MutatingZone())
        item = replace(self.parse([row()]).observations[0], observed_at=stamp)
        with self.assertRaises(TypeError):
            validate_public_observation(item, source=V2_SOURCE)
        with self.assertRaises(TypeError):
            parse_catalog('{"models":[]}', observed_at=stamp, source=V2_SOURCE)
        self.assertEqual(calls, [])
        self.assertIsNone(item.advertised_quote)

    def test_v2_datetime_subclass_rejected_before_attribute_or_method_callbacks(self):
        from dataclasses import replace
        from model_price_watcher.providers.cheaper_inference import parse_catalog, validate_public_observation
        quote = self.parse([row()], SOURCE).observations[0].advertised_quote
        calls = []
        class MutatingDatetime(datetime):
            def __getattribute__(self, name):
                if name in ('tzinfo', 'utcoffset', 'astimezone'):
                    calls.append(name)
                    object.__setattr__(item, 'advertised_quote', quote)
                return super().__getattribute__(name)
        stamp = MutatingDatetime(2026, 9, 23, tzinfo=timezone.utc)
        item = replace(self.parse([row()]).observations[0], observed_at=stamp)
        with self.assertRaises(TypeError):
            validate_public_observation(item, source=V2_SOURCE)
        with self.assertRaises(TypeError):
            parse_catalog('{"models":[]}', observed_at=stamp, source=V2_SOURCE)
        self.assertEqual(calls, [])
        self.assertIsNone(item.advertised_quote)

    def test_v2_parser_produces_exact_builtin_utc_timestamps(self):
        from datetime import timedelta
        from model_price_watcher.providers.cheaper_inference import parse_catalog, validate_public_observation
        raw = json.dumps({'models': [row(endpoint='/v1/responses', supported_endpoints=[])]})
        for stamp in (AT, datetime(2026, 9, 23, 5, 30, tzinfo=timezone(timedelta(hours=5, minutes=30)))):
            with self.subTest(stamp=stamp):
                result = parse_catalog(raw, observed_at=stamp, source=V2_SOURCE)
                self.assertTrue(result.accepted)
                for value in (result.observed_at, result.observations[0].observed_at):
                    self.assertIs(type(value), datetime)
                    self.assertIs(type(value.tzinfo), timezone)
                    self.assertIs(value.tzinfo, timezone.utc)
                    self.assertEqual(value, AT)
                item, = result.observations
                validate_public_observation(item, source=V2_SOURCE)
                self.assertIsNone(item.advertised_quote)
        with self.assertRaises((TypeError, ValueError)):
            parse_catalog(raw, observed_at=datetime(2026, 9, 23), source=V2_SOURCE)

    def parse(self, rows, source=V2_SOURCE):
        from model_price_watcher.providers.cheaper_inference import parse_catalog
        # v2 routing metadata is structural, never a routing/price selection input.
        if source == V2_SOURCE:
            rows = [dict({'endpoint': '/v1/chat/completions', 'supported_endpoints': []}, **r) for r in rows]
        # Exact accepted v2 metadata must be admitted before row interpretation.
        if source == V2_SOURCE:
            try:
                return parse_catalog(json.dumps({'models': rows}), observed_at=AT, source=source)
            except ValueError as error:
                self.fail('exact v2 source rejected: '+str(error))
        return parse_catalog(json.dumps({'models': rows}), observed_at=AT, source=source)

    def test_exact_contract_dispatch_and_no_shape_inference(self):
        from model_price_watcher.providers.cheaper_inference import SOURCE_METADATA, V1_SOURCE_METADATA
        self.assertEqual(SOURCE_METADATA, V2_SOURCE.metadata)
        self.assertEqual(V1_SOURCE_METADATA, SOURCE.metadata)
        self.assertIsNotNone(self.parse([row()], SOURCE).observations[0].advertised_quote)
        self.assertIsNone(self.parse([row()]).observations[0].advertised_quote)
        # A v2-looking row never causes fallback from declared v1.
        self.assertFalse(self.parse([row(zero_data_retention_route='none')], SOURCE).accepted)
        for contract, policy in (('v1', 'evidence-only-v2'), ('v2', 'base-token-v1'),
                                 ('v3', 'evidence-only-v2')):
            mixed = SourceMetadata(SOURCE.location, dict(SOURCE.metadata,
                source_contract='cheaper-inference-public-models-'+contract,
                interpretation_policy='ci-advertised-'+policy))
            with self.subTest(contract=contract, policy=policy), self.assertRaises(ValueError):
                self.parse([row()], mixed)
        for invalid in (SourceMetadata(SOURCE.location+'?zdr=true', V2_SOURCE.metadata),
                        SourceMetadata(SOURCE.location, dict(V2_SOURCE.metadata, extra=True))):
            with self.assertRaises((TypeError, ValueError)):
                self.parse([], invalid)

    def test_every_v2_row_has_no_numeric_interpretation(self):
        from model_price_watcher.providers.cheaper_inference import validate_public_observation
        cases = ({}, {'input_per_million': 'bad'},
                 {'input_per_million': '?', 'output_per_million': '?'},
                 {'zero_data_retention': True, 'zero_data_retention_route': 'rail_only'},
                 {'zero_data_retention_route': 'discounted'}, {'zero_data_retention_route': 'none'},
                 {'zero_data_retention_route': None}, {'image_pricing_unit': 'second'},
                 {'above_threshold': {'applies_when': 'prompt_tokens_above_threshold'}},
                 {'new_field': {'unknown_semantics': [1, None]}},
                 {'default_pricing_scope': {'serviceTier': 'priority', 'zdr': True},
                  'pricing_scopes': [{'scope': {'serviceTier': 'flex'},
                                      'rates': {'input_tokens': '0'}, 'expiresAt': 'unknown'}]})
        for changes in cases:
            with self.subTest(changes=changes):
                result = self.parse([row(**changes)])
                self.assertTrue(result.accepted)
                item, = result.observations
                self.assertEqual((item.advertised_quote, item.input_usd_per_million,
                                  item.output_usd_per_million), (None, None, None))
                self.assertEqual(item.unsupported_pricing, {
                    'interpretation_policy': 'ci-advertised-evidence-only-v2',
                    'reason': 'numeric_scope_semantics_unestablished',
                    'retained_fields': {k: v for k, v in dict(row(endpoint='/v1/chat/completions', supported_endpoints=[]), **changes).items()
                                        if k not in {'id', 'input_per_million', 'output_per_million'}}})
                self.assertEqual([d.code for d in result.diagnostics],
                                 ['numeric_scope_semantics_unestablished'])
                self.assertEqual(item.raw_offering, dict(row(endpoint='/v1/chat/completions', supported_endpoints=[]), **changes))
                validate_public_observation(item, source=V2_SOURCE)

    def test_required_routing_types_and_atomic_structure_rejection(self):
        from model_price_watcher.providers.cheaper_inference import parse_catalog
        for changes in ({'endpoint': 'future endpoint', 'supported_endpoints': []},
                        {'endpoint': '', 'supported_endpoints': ['unfamiliar', '/v1/messages']}):
            self.assertTrue(self.parse([row(**changes)]).accepted)
        for missing in ('endpoint', 'supported_endpoints'):
            model = row(endpoint='/v1/responses', supported_endpoints=[])
            del model[missing]
            self.assertFalse(parse_catalog(json.dumps({'models': [model]}), observed_at=AT, source=V2_SOURCE).accepted)
        for changes in ({'endpoint': None}, {'endpoint': 1}, {'supported_endpoints': 'x'},
                        {'supported_endpoints': [1]}, {'supported_endpoints': None},
                        {'default_pricing_scope': []}, {'pricing_scopes': {}},
                        {'pricing_scopes': [1]}, {'zero_data_retention_route': 1},
                        {'zero_data_retention_route': []}, {'image_pricing_unit': None},
                        {'image_pricing_unit': {}}, {'media_unit': 1},
                        {'above_threshold': {'applies_when': 1}},
                        {'above_threshold': {'applies_when': None}}, {'input_per_million': 1},
                        {'zero_data_retention': None}, {'model_type': 'other'}):
            with self.subTest(changes=changes):
                model = row(endpoint='/v1/responses', supported_endpoints=[])
                model.update(changes)
                result = self.parse([row(id='good'), model])
                self.assertFalse(result.accepted)
                self.assertEqual(result.observations, ())

    def test_unfamiliar_string_meanings_remain_exact_evidence(self):
        cases = ([{'zero_data_retention_route': value} for value in
                  ('discounted', 'rail_only', 'none', 'seller', 'rail', 'future-route')]
                 + [{'above_threshold': {'applies_when': value}} for value in
                    ('prompt_tokens_above_threshold', 'future condition with different meaning')]
                 + [{'image_pricing_unit': value} for value in ('second', 'future-unit')]
                 + [{'media_unit': 'future-media-unit'}])
        for changes in cases:
            with self.subTest(changes=changes):
                result = self.parse([row(**changes)])
                self.assertTrue(result.accepted)
                item, = result.observations
                self.assertIsNone(item.advertised_quote)
                for key, value in changes.items():
                    self.assertEqual(item.raw_offering[key], value)
                    self.assertEqual(item.unsupported_pricing['retained_fields'][key], value)

    def test_scope_contents_remain_opaque_and_ordered(self):
        scopes = [{'future': {'unit': 'credits', 'rates': [None, True, '1']}},
                  {'scope': None, 'missing_authority': ['unknown', {'x': 3}]}]
        cases = ({}, {'default_pricing_scope': {}}, {'pricing_scopes': []},
                 {'default_pricing_scope': {'unexpected': [False, None, {'nested': 'opaque'}]}},
                 {'pricing_scopes': scopes}, {'pricing_scopes': list(reversed(scopes))},
                 {'pricing_scopes': [{}], 'new_row_field': {'unfamiliar': [1, None]}})
        for changes in cases:
            with self.subTest(changes=changes):
                result = self.parse([row(**changes)])
                self.assertTrue(result.accepted)
                item, = result.observations
                self.assertIsNone(item.advertised_quote)
                for key, value in changes.items():
                    self.assertEqual(item.raw_offering[key], value)

    def test_v2_equality_overrides_cannot_forge_source_or_evidence(self):
        from dataclasses import replace
        from model_price_watcher.providers.cheaper_inference import validate_public_source, validate_public_observation
        class EqualString(str):
            def __eq__(self, other): return True
            def __ne__(self, other): return False
        class EqualDict(dict):
            def __eq__(self, other): return True
            def __ne__(self, other): return False
        class EqualSource(SourceMetadata):
            def __eq__(self, other): return True
            def __ne__(self, other): return False
        invalid_sources = (SourceMetadata(EqualString('https://wrong.invalid'), dict(V2_SOURCE.metadata)),
                           SourceMetadata(SOURCE.location, EqualDict(V2_SOURCE.metadata)),
                           SourceMetadata(SOURCE.location, dict(V2_SOURCE.metadata,
                                          source_contract=EqualString('unknown-contract'))),
                           EqualSource(SOURCE.location, dict(V2_SOURCE.metadata)))
        for invalid in invalid_sources:
            with self.subTest(source=invalid), self.assertRaises((TypeError, ValueError)):
                validate_public_source(invalid)
        item = self.parse([row()]).observations[0]
        for change in ({'unsupported_pricing': dict(item.unsupported_pricing, reason=EqualString('forged'))},
                       {'unsupported_pricing': EqualDict(item.unsupported_pricing)},
                       {'raw_offering': dict(item.raw_offering, zero_data_retention_route=EqualString('seller'))}):
            with self.subTest(change=change), self.assertRaises((TypeError, ValueError)):
                validate_public_observation(replace(item, **change), source=V2_SOURCE)

    def test_v2_keeps_json_identity_depth_and_documented_type_guards(self):
        from model_price_watcher.providers.cheaper_inference import (
            parse_catalog, _NULLABLE_STRINGS, _BOOLS, _INTS, _MAPS)
        for field in _NULLABLE_STRINGS | _BOOLS | _INTS | _MAPS:
            with self.subTest(field=field):
                self.assertFalse(self.parse([row(**{field: []})]).accepted)
        for changes in ({'id': ''}, {'id': ' '}, {'id': '\x00'}, {'id': '\ud800'},
                        {'unknown': '\ud800'}, {'above_threshold': []},
                        {'aliases': [1]}, {'reasoning_capability_mode': 'future'}):
            with self.subTest(changes=changes):
                self.assertFalse(self.parse([row(**changes)]).accepted)
        for raw in ('not json', '{"models":[],"models":[]}', '{"models":[],"extra":null}',
                    '{"models":[null]}', '{"models":[[]]}', '{"models":[NaN]}',
                    '{"models":[Infinity]}', '{"models":[{"x":1,"x":2}]}'):
            with self.subTest(raw=raw):
                self.assertFalse(parse_catalog(raw, observed_at=AT, source=V2_SOURCE).accepted)
        self.assertFalse(self.parse([row(), row()]).accepted)
        nested = {}
        for _ in range(64):
            nested = {'x': nested}
        self.assertFalse(self.parse([row(unknown=nested)]).accepted)

    def test_retained_attempt_01_exact_fixture_all_97_rows(self):
        import hashlib
        from pathlib import Path
        from decimal import Decimal
        from model_price_watcher.providers.cheaper_inference import parse_catalog
        data = (Path(__file__).parent/'fixtures/cheaper_inference_public_catalog_g1b_live_01.json').read_bytes()
        self.assertEqual(len(data), 732879)
        self.assertEqual(hashlib.sha256(data).hexdigest(),
                         'eacd19c8b34bb51928180ceb7c2831315f70de9515e4d6ca9900c71b9e77e0f1')
        result = parse_catalog(data.decode('utf-8'), observed_at=AT, source=V2_SOURCE)
        self.assertTrue(result.accepted)
        raw_rows = json.loads(data, parse_int=Decimal, parse_float=Decimal)['models']
        self.assertEqual(len(result.observations), 97)
        self.assertEqual(len(result.diagnostics), 97)
        for item, raw in zip(result.observations, raw_rows):
            self.assertEqual(item.raw_offering, raw)
            self.assertEqual(item.offering_id, raw['id'])
            self.assertEqual(item.observed_at, AT)
            self.assertEqual(item.source, V2_SOURCE)
            self.assertIsNone(item.advertised_quote)
        self.assertFalse(parse_catalog(data.decode('utf-8'), observed_at=AT, source=SOURCE).accepted)

    def test_v2_forged_quotes_legacy_prices_and_reason_rejected(self):
        from dataclasses import replace
        from model_price_watcher.models import AdvertisedTokenQuote
        from model_price_watcher.providers.cheaper_inference import validate_public_observation
        class NoneLikeQuote:
            def __eq__(self, other): return True
            def __ne__(self, other): return False
            def __bool__(self): return False
        class EqualQuote(AdvertisedTokenQuote):
            def __eq__(self, other): return True
            def __ne__(self, other): return False
        class EqualString(str):
            def __eq__(self, other): return True
            def __ne__(self, other): return False
        item = self.parse([row()]).observations[0]
        quote = self.parse([row()], SOURCE).observations[0].advertised_quote
        for q in (quote, replace(quote, output_usd_per_million=None),
                  replace(quote, input_usd_per_million=None, output_usd_per_million=None),
                  replace(quote, input_usd_per_million=Decimal(0), output_usd_per_million=Decimal(0)),
                  replace(quote, basis=replace(quote.basis, interpretation_policy=V2_SOURCE.metadata['interpretation_policy'])),
                  replace(quote, basis=replace(quote.basis, unit=EqualString('forged-unit'))),
                  EqualQuote(quote.basis, Decimal('1.25'), Decimal('10')),
                  EqualQuote(quote.basis, Decimal('1.25'), None), EqualQuote(quote.basis, None, None),
                  NoneLikeQuote(), False, object()):
            with self.subTest(quote=q), self.assertRaises(ValueError):
                validate_public_observation(replace(item, advertised_quote=q), source=V2_SOURCE)
        for changes in ({'input_usd_per_million': Decimal(1)},
                        {'output_usd_per_million': Decimal(1)},
                        {'unsupported_pricing': dict(item.unsupported_pricing, reason='other')},
                        {'source': SOURCE}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                validate_public_observation(replace(item, **changes), source=V2_SOURCE)
