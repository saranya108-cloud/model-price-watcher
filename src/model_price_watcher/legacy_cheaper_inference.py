"""Pure, versioned public catalog interpretation. No network or storage.

Historical v1 quotes are advertised base token rates, not charge guarantees.
Current v2 retains evidence only: numeric scope semantics are unestablished.
"""
import json
import re
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Context, Decimal, DecimalException, MAX_PREC, MAX_EMAX, MIN_EMIN, localcontext

from model_price_watcher.models import (
    AdvertisedQuoteBasis, AdvertisedTokenQuote, CatalogObservation,
    Diagnostic, ParseResult, SourceMetadata,
)

STREAM_ID = "cheaper_inference.public.standard"
SOURCE_URL = "https://api.cheaperinference.com/public/models"
CATALOG_SCOPE = "public-advertised-standard"
PRICE_KIND = "provider_advertised_catalog"
V1_INTERPRETATION_POLICY = "ci-advertised-base-token-v1"
V1_SOURCE_METADATA = {
    "catalog_scope": CATALOG_SCOPE,
    "source_contract": "cheaper-inference-public-models-v1",
    "interpretation_policy": V1_INTERPRETATION_POLICY,
    "upstream_freshness": "unknown",
}
INTERPRETATION_POLICY = "ci-advertised-evidence-only-v2"
SOURCE_METADATA = dict(V1_SOURCE_METADATA,
    source_contract="cheaper-inference-public-models-v2",
    interpretation_policy=INTERPRETATION_POLICY)
_REQUIRED = frozenset("id model_type input_per_million output_per_million discount_percent".split())
_NULLABLE_STRINGS = frozenset('''cache_read_per_million cache_write_per_million
image_output_per_million media_input_unit_price media_unit_price media_unit
reference_input_per_million reference_output_per_million reference_cache_read_per_million
reference_cache_write_per_million reference_image_output_per_million
reference_media_input_unit_price reference_media_unit_price logo_url provider_name
primary_model_id available_until input_per_million_above_threshold
output_per_million_above_threshold'''.split())
_BOOLS = frozenset('''is_visible supports_vision supports_video supports_reasoning
supports_streaming supports_image_edit is_free zero_data_retention'''.split())
_INTS = frozenset("context_length max_output_tokens input_token_price_threshold".split())
_MAPS = frozenset(("media_prices", "reference_media_prices"))
_ENUMS = {"model_type": ("text", "image", "video"),
          "image_pricing_unit": ("token", "image"),
          "reasoning_capability_mode": ("auto", "manual"),
          "zero_data_retention_route": ("seller", "rail", None)}
_V2_OPAQUE_STRINGS = frozenset(("image_pricing_unit", "zero_data_retention_route"))
_KNOWN = _REQUIRED | _NULLABLE_STRINGS | _BOOLS | _INTS | _MAPS | _ENUMS.keys() | {"aliases", "above_threshold"}
_EXCLUDED = frozenset('''id input_per_million output_per_million discount_percent
reference_input_per_million reference_output_per_million reference_cache_read_per_million
reference_cache_write_per_million reference_image_output_per_million
reference_media_input_unit_price reference_media_unit_price reference_media_prices
cache_read_per_million cache_write_per_million logo_url aliases'''.split())
_ABOVE_STRINGS = frozenset('''input_per_million cache_read_input_per_million
cache_write_input_per_million output_per_million list_input_per_million
list_cache_read_input_per_million list_cache_write_input_per_million list_output_per_million'''.split())
_NUMBER = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?")


def is_reserved_stream(provider):
    """Reserve by string content without invoking caller overrides.

    Reserved provider string subclasses are rejected for both v1 and v2;
    ordinary exact-string v1 providers retain their historical behavior.
    """
    if not isinstance(provider, str):
        return False
    reserved = (str.__eq__(provider, "cheaper_inference")
                or str.startswith(provider, "cheaper_inference."))
    if reserved and type(provider) is not str:
        raise TypeError("reserved provider must be exact str")
    return reserved


def _context():
    return Context(prec=MAX_PREC, Emax=MAX_EMAX, Emin=MIN_EMIN)


def _utc(value, *, exact_types=False):
    """v2 requires exact datetime/timezone (UTC or fixed offset); v1 normalizes subclasses."""
    # v2 must reject callbacks before even reading a subclass's tzinfo.
    # The original observation timestamp also reaches storage serialization.
    if exact_types and (type(value) is not datetime or type(value.tzinfo) is not timezone):
        raise TypeError("v2 timestamp must be exact datetime with exact timezone")
    if not isinstance(value, datetime):
        raise TypeError("timestamp must be datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be aware")
    try:
        return value.astimezone(timezone.utc)
    except (ValueError, OverflowError) as error:
        raise ValueError("timestamp not representable in UTC") from error


def _identity(value):
    if not isinstance(value, str):
        raise TypeError("identity must be string")
    if not value.strip() or "\0" in value:
        raise ValueError("identity must be nonblank and NUL-free")
    value.encode("utf-8")


def _numeric(value):
    with localcontext(_context()):
        value = Decimal(value)
    if not value.is_finite():
        raise ValueError("number must be finite")
    sign, digits, exponent = value.as_tuple()
    if not any(digits):
        return "0"
    digits = list(digits)
    while digits[-1] == 0:
        digits.pop()
        exponent += 1
    coefficient = str(digits[0])
    if len(digits) > 1:
        coefficient += "." + "".join(str(digit) for digit in digits[1:])
    return ("-" if sign else "") + coefficient + "e" + str(exponent + len(digits) - 1)


def _node(value, depth=0, active=None, *, exact_types=False):
    """Validate source depth, then encode; tag expansion has no second depth cap."""
    if active is None:
        active = set()
    if exact_types and type(value) not in (type(None), bool, str, int, Decimal, dict, list):
        raise TypeError("v2 evidence must use exact JSON evidence types")
    if value is None:
        return ["n"]
    if isinstance(value, bool):
        return ["b", value]
    if isinstance(value, str):
        value.encode("utf-8")
        return ["s", value]
    if isinstance(value, (int, Decimal)):
        return ["d", _numeric(value)]
    if not isinstance(value, (dict, list)):
        raise TypeError("unsupported evidence type")
    if depth >= 64 or id(value) in active:
        raise ValueError("excessive or cyclic evidence depth")
    active.add(id(value))
    try:
        if isinstance(value, list):
            return ["a", [_node(x, depth + 1, active, exact_types=exact_types) for x in value]]
        for k in value:
            if not isinstance(k, str) or (exact_types and type(k) is not str):
                raise TypeError("evidence keys must be strings")
            k.encode("utf-8")
        return ["o", [[k, _node(value[k], depth + 1, active, exact_types=exact_types)] for k in sorted(value)]]
    finally:
        active.remove(id(value))


def _canonical_conditions_key(row):
    if not isinstance(row, dict):
        raise TypeError("row must be dictionary")
    return "ci-basis-v1:" + json.dumps(_node({k: v for k, v in row.items() if k not in _EXCLUDED}),
                                     ensure_ascii=True, separators=(",", ":"), allow_nan=False)


def validate_public_source(source: SourceMetadata) -> None:
    if type(source) is not SourceMetadata:
        raise TypeError("source must be SourceMetadata")
    # Contract dispatch cannot trust overridden equality on strings/mappings.
    if type(source.location) is not str or type(source.metadata) is not dict:
        raise TypeError("invalid source fields")
    if any(type(k) is not str or type(v) is not str for k, v in source.metadata.items()):
        raise TypeError("source metadata must contain exact strings")
    if source.location != SOURCE_URL or source.metadata not in (V1_SOURCE_METADATA, SOURCE_METADATA):
        raise ValueError("source must declare an exact public catalog contract")


def _integer(value):
    if isinstance(value, bool) or not isinstance(value, (int, Decimal)):
        return False
    with localcontext(_context()):
        value = Decimal(value)
        return value.is_finite() and value == value.to_integral_value()


def _validate_row(row, *, evidence_only=False):
    if not isinstance(row, dict):
        raise TypeError("model must be object")
    _node(row, exact_types=evidence_only)
    required = _REQUIRED | {"endpoint", "supported_endpoints"} if evidence_only else _REQUIRED
    if not required <= row.keys():
        raise ValueError("missing required model field")
    for k in _REQUIRED:
        if not isinstance(row[k], str):
            raise TypeError("required model fields must be strings")
    _identity(row["id"])
    for k, v in row.items():
        if k in _NULLABLE_STRINGS and v is not None and not isinstance(v, str):
            raise TypeError("optional field must be string or null")
        if k in _BOOLS and not isinstance(v, bool):
            raise TypeError("flag must be boolean")
        if k in _INTS and v is not None and not _integer(v):
            raise TypeError("field must be integral or null")
        if k in _ENUMS:
            if not isinstance(v, str) and not (v is None and k == 'zero_data_retention_route'):
                raise TypeError('enum field has wrong type')
            if not (evidence_only and k in _V2_OPAQUE_STRINGS) and v not in _ENUMS[k]:
                raise ValueError("unsupported enum value")
        if k in _MAPS and (not isinstance(v, dict) or any(not isinstance(x, str) for x in v.values())):
            raise TypeError("price map must contain strings")
    if "aliases" in row and (not isinstance(row["aliases"], list) or any(not isinstance(x, str) for x in row["aliases"])):
        raise TypeError("aliases must be string array")
    if evidence_only:
        if not isinstance(row['endpoint'], str):
            raise TypeError("endpoint must be string")
        if (not isinstance(row['supported_endpoints'], list)
                or any(not isinstance(x, str) for x in row['supported_endpoints'])):
            raise TypeError("supported_endpoints must be string array")
        if 'default_pricing_scope' in row and not isinstance(row['default_pricing_scope'], dict):
            raise TypeError("default_pricing_scope must be object")
        if 'pricing_scopes' in row and (not isinstance(row['pricing_scopes'], list)
                or any(not isinstance(x, dict) for x in row['pricing_scopes'])):
            raise TypeError("pricing_scopes must be object array")
    above = row.get("above_threshold")
    if above is not None:
        if not isinstance(above, dict):
            raise TypeError("above_threshold must be object or null")
        for k, v in above.items():
            if k in _ABOVE_STRINGS and not isinstance(v, str):
                raise TypeError("conditional price must be string")
            if k == "input_token_price_threshold" and not _integer(v):
                raise TypeError("conditional threshold must be integral")
            if k == "applies_when":
                if evidence_only:
                    if not isinstance(v, str):
                        raise TypeError("threshold condition must be string")
                elif v != "prompt_tokens_above_threshold_and_supplier_charges_long_context_premium":
                    raise ValueError("invalid threshold condition")
    if not evidence_only and row.get("zero_data_retention_route") is not None:
        raise ValueError("ZDR route cannot enter Standard history")


def _money(value, field, identity, diagnostics):
    try:
        if not _NUMBER.fullmatch(value):
            raise ValueError("invalid monetary syntax")
        with localcontext(_context()):
            amount = Decimal(value)
            if not amount.is_finite() or amount < 0:
                raise ValueError("invalid monetary amount")
        return Decimal(0) if amount.is_zero() else amount
    except (DecimalException, ValueError, OverflowError):
        diagnostics.append(Diagnostic("unknown_price", field, "Unknown advertised component.", identity, value))
        return None


def _derive_v1_observation(row, *, observed_at, source):
    _validate_row(row)
    row = deepcopy(row)
    conditions = {"interpretation_policy": V1_INTERPRETATION_POLICY,
                  "retained_fields": {k: deepcopy(v) for k, v in row.items() if k not in {"id", "input_per_million", "output_per_million"}}}
    _node(conditions)
    eligible = row["model_type"] == "text" and not row.keys() - _KNOWN
    eligible = eligible and all(row.get(k) is None for k in ("media_input_unit_price", "media_unit_price", "media_unit", "image_output_per_million"))
    eligible = eligible and not row.get("media_prices") and row.get("image_pricing_unit", "token") == "token"
    threshold = row.get("input_token_price_threshold")
    if threshold is None:
        eligible = eligible and all(row.get(k) is None for k in ("input_per_million_above_threshold", "output_per_million_above_threshold")) and not row.get("above_threshold")
    elif threshold <= 0:
        eligible = False
    diagnostics = []
    if row.get("zero_data_retention") is True:
        eligible = False
        diagnostics.append(Diagnostic("ambiguous_retention_view", "zero_data_retention", "Retention view is ambiguous.", row["id"], True))
    quote = None
    if eligible:
        basis = AdvertisedQuoteBasis(STREAM_ID, PRICE_KIND, "USD", "per_million_tokens", "text_token", "base", V1_INTERPRETATION_POLICY, _canonical_conditions_key(row))
        quote = AdvertisedTokenQuote(basis, *(_money(row[k], k, row["id"], diagnostics) for k in ("input_per_million", "output_per_million")))
    return CatalogObservation(STREAM_ID, row["id"], _utc(observed_at), deepcopy(source), None, None, conditions, row, quote), tuple(diagnostics)


def _derive_public_observation(row, *, observed_at, source):
    """Dispatch only by exact metadata, never by row shape or numeric resemblance."""
    validate_public_source(source)
    if source.metadata == V1_SOURCE_METADATA:
        return _derive_v1_observation(row, observed_at=observed_at, source=source)
    _validate_row(row, evidence_only=True)
    row = deepcopy(row)
    reason = "numeric_scope_semantics_unestablished"
    conditions = {"interpretation_policy": INTERPRETATION_POLICY, "reason": reason,
                  "retained_fields": {k: deepcopy(v) for k, v in row.items()
                                      if k not in {"id", "input_per_million", "output_per_million"}}}
    _node(conditions)
    observation = CatalogObservation(STREAM_ID, row["id"], _utc(observed_at, exact_types=True),
        SourceMetadata(source.location, dict(source.metadata)), None, None, conditions, row, None)
    return observation, (Diagnostic(reason, "$", "Retained evidence has no authoritative numeric quote.", row["id"]),)


def validate_public_observation(observation: CatalogObservation, *, source: SourceMetadata) -> None:
    if not isinstance(observation, CatalogObservation):
        raise TypeError("observation must be CatalogObservation")
    if not isinstance(observation.provider, str):
        raise TypeError('provider must be string')
    if not isinstance(observation.unsupported_pricing, dict) or not isinstance(observation.raw_offering, dict):
        raise TypeError('retained evidence must be dictionaries')
    validate_public_source(source)
    validate_public_source(observation.source)
    evidence_only = source.metadata == SOURCE_METADATA
    if evidence_only and type(observation) is not CatalogObservation:
        raise TypeError("v2 observation must be exact CatalogObservation")
    _identity(observation.offering_id)
    if observation.provider != STREAM_ID or observation.source != source:
        raise ValueError("observation must belong to the public Standard source")
    if observation.input_usd_per_million is not None or observation.output_usd_per_million is not None:
        raise ValueError("advertised observations cannot contain legacy prices")
    quote = observation.advertised_quote
    # Absence is an identity invariant, never an overloaded equality decision.
    if evidence_only and quote is not None:
        raise ValueError("v2 observations cannot contain an advertised quote")
    if evidence_only:
        if type(observation.provider) is not str or type(observation.offering_id) is not str:
            raise TypeError("v2 identities must be exact strings")
        _node(observation.unsupported_pricing, exact_types=True)
    if quote is not None:
        if not isinstance(quote, AdvertisedTokenQuote) or not isinstance(quote.basis, AdvertisedQuoteBasis):
            raise TypeError("invalid quote type")
        for v in vars(quote.basis).values():
            if not isinstance(v, str):
                raise TypeError("basis fields must be strings")
        for v in (quote.input_usd_per_million, quote.output_usd_per_million):
            if v is not None:
                if not isinstance(v, Decimal):
                    raise TypeError("quote money must be Decimal")
                if not v.is_finite() or v < 0:
                    raise ValueError("invalid quote money")
    try:
        expected, _ = _derive_public_observation(observation.raw_offering, observed_at=observation.observed_at, source=source)
        evidence_equal = _node(observation.unsupported_pricing) == _node(expected.unsupported_pricing)
    except DecimalException as error:
        raise ValueError("invalid numeric evidence") from error
    if (observation.offering_id != expected.offering_id
            or (not evidence_only and quote != expected.advertised_quote) or not evidence_equal):
        raise ValueError("observation disagrees with retained source evidence")


def _unique(pairs):
    obj = {}
    for k, v in pairs:
        if k in obj:
            raise ValueError("duplicate JSON key")
        obj[k] = v
    return obj


def _reject_constant(value):
    raise ValueError("nonfinite JSON number")


def parse_catalog(raw_catalog: str, *, observed_at: datetime, source: SourceMetadata) -> ParseResult:
    if not isinstance(raw_catalog, str):
        raise TypeError("raw_catalog must be string")
    # Recognize v2 without overloaded metadata equality; preserve v1's original
    # timestamp/source validation order and permissive timestamp normalization.
    evidence_only = (type(source) is SourceMetadata and type(source.metadata) is dict
                     and all(type(k) is str and type(v) is str for k, v in source.metadata.items())
                     and source.metadata == SOURCE_METADATA)
    observed_at = _utc(observed_at, exact_types=evidence_only)
    validate_public_source(source)
    source = SourceMetadata(source.location, dict(source.metadata)) if evidence_only else deepcopy(source)
    observations, diagnostics = [], []
    try:
        with localcontext(_context()):
            catalog = json.loads(raw_catalog, parse_int=Decimal, parse_float=Decimal,
                                 object_pairs_hook=_unique, parse_constant=_reject_constant)
        _node(catalog)
        if not isinstance(catalog, dict) or set(catalog) != {"models"} or not isinstance(catalog["models"], list):
            raise ValueError("expected public models envelope")
        seen = set()
        for row in catalog["models"]:
            item, notes = _derive_public_observation(row, observed_at=observed_at, source=source)
            if item.offering_id in seen:
                raise ValueError("duplicate exact offering ID")
            seen.add(item.offering_id)
            observations.append(item)
            diagnostics.extend(notes)
    except (TypeError, ValueError, DecimalException, OverflowError, RecursionError):
        return ParseResult(False, (), (Diagnostic("invalid_catalog", "$", "Invalid public Standard catalog."),), observed_at, source, raw_catalog)
    return ParseResult(True, tuple(observations), tuple(diagnostics), observed_at, source, raw_catalog)
