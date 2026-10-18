"""Compatibility shim for the historical Cheaper Inference module path.

The Revision 3 implementation now lives in the core module
``model_price_watcher.legacy_cheaper_inference``. This module re-exports every
module-level name of the 2fb4be6 provider module, bound to the identical
objects, for the identified consumers (tests, tools/, advertised_* modules,
cheaper_inference_acquisition and the Revision 3 v1 differential harness).

Re-exported functions keep the legacy module as their defining module and
global namespace: patching a helper here does not change what those functions
call internally.

CHANGE CONTROL: ``legacy_cheaper_inference.py`` must remain byte-identical to
``2fb4be6:src/model_price_watcher/providers/cheaper_inference.py`` until a
reviewed gate says otherwise. Any change to it, or to ``contracts.py``,
requires storage-intensity review.
"""

from model_price_watcher.legacy_cheaper_inference import (  # noqa: F401
    json,
    re,
    deepcopy,
    datetime,
    timezone,
    Context,
    Decimal,
    DecimalException,
    MAX_PREC,
    MAX_EMAX,
    MIN_EMIN,
    localcontext,
    AdvertisedQuoteBasis,
    AdvertisedTokenQuote,
    CatalogObservation,
    Diagnostic,
    ParseResult,
    SourceMetadata,
    STREAM_ID,
    SOURCE_URL,
    CATALOG_SCOPE,
    PRICE_KIND,
    V1_INTERPRETATION_POLICY,
    V1_SOURCE_METADATA,
    INTERPRETATION_POLICY,
    SOURCE_METADATA,
    _REQUIRED,
    _NULLABLE_STRINGS,
    _BOOLS,
    _INTS,
    _MAPS,
    _ENUMS,
    _V2_OPAQUE_STRINGS,
    _KNOWN,
    _EXCLUDED,
    _ABOVE_STRINGS,
    _NUMBER,
    is_reserved_stream,
    _context,
    _utc,
    _identity,
    _numeric,
    _node,
    _canonical_conditions_key,
    validate_public_source,
    _integer,
    _validate_row,
    _money,
    _derive_v1_observation,
    _derive_public_observation,
    validate_public_observation,
    _unique,
    _reject_constant,
    parse_catalog,
)
