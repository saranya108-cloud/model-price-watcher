"""Pure parsing of supplied OpenRouter catalog JSON (no endpoint expansion).

prompt/completion are USD per token; scale exactly to USD per million tokens.
All other pricing fields remain unsupported evidence, without unit conversion
or any inference about discounts, availability, endpoint prices, or free use.
"""

import json
import re
from copy import deepcopy
from datetime import datetime
from decimal import Decimal, InvalidOperation, localcontext

from model_price_watcher.models import (
    CatalogObservation,
    Diagnostic,
    ParseResult,
    SourceMetadata,
)


_NUMBER = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?")
# Preserve Decimal's explicit non-finite spellings, including NaN payloads.
_NON_FINITE = re.compile(r"[+-]?(?:s?nan\d*|inf(?:inity)?)", re.IGNORECASE)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_constant(value):
    raise ValueError(f"Non-JSON numeric constant: {value}")


def _money(value, *, path, offering_id, diagnostics):
    """Validate a scalar monetary value, retaining the original in diagnostics."""
    if value is None:
        return None
    code = None
    amount = None
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        code = "malformed_price"
    elif isinstance(value, str) and _NUMBER.fullmatch(value) is None:
        if _NON_FINITE.fullmatch(value.strip().replace("_", "")) is not None:
            code = "non_finite_price"
        else:
            code = "malformed_price"
    else:
        try:
            with localcontext() as context:
                context.traps[InvalidOperation] = True
                amount = Decimal(value)
        except (InvalidOperation, ValueError, OverflowError):
            code = "malformed_price"
        else:
            if not amount.is_finite():
                code = "non_finite_price"
            elif amount < 0:
                code = "negative_price"
    if code is not None:
        diagnostics.append(Diagnostic(
            code, path, "Expected a finite, non-negative monetary value.",
            offering_id, value,
        ))
        return None
    return amount


def _per_million(amount):
    if amount is None:
        return None
    # Move the decimal point without multiplication, rounding, or dependence
    # on the caller's Decimal precision/Emax/Emin settings.
    sign, digits, exponent = amount.as_tuple()
    with localcontext() as context:
        context.traps[InvalidOperation] = True
        normalized = Decimal((sign, digits, exponent + 6))
    if not normalized.is_finite():
        raise ValueError("Normalized monetary value must be finite")
    return normalized


def parse_catalog(
    raw_catalog: str, *, observed_at: datetime, source: SourceMetadata,
) -> ParseResult:
    """Parse supplied JSON with deterministic, caller-supplied provenance.

    Missing/null prompt or completion is unknown. Missing pricing is unknown;
    a present non-object pricing field is invalid structure. Invalid monetary
    values become unknown with diagnostics. Any invalid structure or duplicate
    exact ID rejects the entire catalog, so no partial catalog escapes.

    Invalid JSON (including duplicate keys and bare NaN/Infinity) is rejected.
    JSON numeric fractions are decoded directly as Decimal, never via float.
    """
    if not isinstance(raw_catalog, str):
        raise TypeError("raw_catalog must be JSON text")
    if not isinstance(observed_at, datetime):
        raise TypeError("observed_at must be a datetime")
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise ValueError("observed_at must be timezone-aware")
    if not isinstance(source, SourceMetadata):
        raise TypeError("source must be SourceMetadata")
    source = deepcopy(source)
    diagnostics = []

    def result(accepted, observations=()):
        return ParseResult(
            accepted, tuple(observations), tuple(diagnostics),
            observed_at, source, raw_catalog,
        )

    try:
        with localcontext() as context:
            context.traps[InvalidOperation] = True
            catalog = json.loads(
                raw_catalog, parse_float=Decimal,
                object_pairs_hook=_unique_object, parse_constant=_reject_constant,
            )
    except (ValueError, InvalidOperation, OverflowError, RecursionError) as error:
        diagnostics.append(Diagnostic("invalid_json", "$", str(error)))
        return result(False)

    if not isinstance(catalog, dict) or not isinstance(catalog.get("data"), list):
        diagnostics.append(Diagnostic(
            "invalid_structure", "$", "Expected an object with a data array.",
        ))
        return result(False)

    seen = set()
    for index, row in enumerate(catalog["data"]):
        path = f"$.data[{index}]"
        if not isinstance(row, dict):
            diagnostics.append(Diagnostic(
                "invalid_structure", path, "Expected an offering object.", raw_value=row,
            ))
            continue
        identity = row.get("id")
        if not isinstance(identity, str) or not identity.strip():
            diagnostics.append(Diagnostic(
                "invalid_structure", path + ".id", "Expected a non-empty string ID.",
                raw_value=identity,
            ))
        elif identity in seen:
            diagnostics.append(Diagnostic(
                "duplicate_offering", path + ".id", "Duplicate exact catalog ID.",
                identity, identity,
            ))
        else:
            seen.add(identity)
        if "pricing" in row and not isinstance(row["pricing"], dict):
            diagnostics.append(Diagnostic(
                "invalid_structure", path + ".pricing", "Expected a pricing object.",
                identity if isinstance(identity, str) else None, row["pricing"],
            ))
    if diagnostics:
        return result(False)

    observations = []
    for index, row in enumerate(catalog["data"]):
        identity = row["id"]
        path = f"$.data[{index}].pricing"
        pricing = row.get("pricing", {})
        normalized = {}
        for key in ("prompt", "completion"):
            amount = _money(
                pricing.get(key), path=path + "." + key,
                offering_id=identity, diagnostics=diagnostics,
            )
            try:
                normalized[key] = _per_million(amount)
            except (InvalidOperation, ValueError, OverflowError):
                # Values beyond Decimal's representable exponent range cannot
                # be normalized exactly; never round or substitute zero.
                normalized[key] = None
                diagnostics.append(Diagnostic(
                    "malformed_price", path + "." + key,
                    "Price exceeds Decimal normalization range.", identity, pricing[key],
                ))
        unsupported = {k: v for k, v in pricing.items() if k not in normalized}
        for key, value in unsupported.items():
            field_path = path + "[" + json.dumps(key) + "]"
            diagnostics.append(Diagnostic(
                "unsupported_pricing", field_path,
                "Pricing field retained without normalization or interpretation.",
                identity, value,
            ))
            # Structured conditions are opaque evidence. Scalar price fields
            # can be validated without assuming their billing units.
            if not isinstance(value, (dict, list)):
                _money(value, path=field_path, offering_id=identity, diagnostics=diagnostics)
        observations.append(CatalogObservation(
            provider="openrouter", offering_id=identity,
            observed_at=observed_at, source=source,
            input_usd_per_million=normalized["prompt"],
            output_usd_per_million=normalized["completion"],
            unsupported_pricing=unsupported, raw_offering=row,
        ))
    return result(True, observations)
