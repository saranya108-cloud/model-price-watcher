"""Value types for a catalog observation and its retained evidence.

Only input/output token prices are normalized. None means unknown, never free.
Raw JSON evidence and unsupported pricing retain their original meaning/units.
Frozen records prevent field reassignment; nested evidence is a detached snapshot,
not an immutable mapping. Consumers should treat that evidence as read-only.
"""

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any


@dataclass(frozen=True)
class SourceMetadata:
    """Caller-supplied source identifier and provenance; never fetched/verified."""

    location: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Diagnostic:
    """A stable code and JSON path, with original value when available."""

    code: str
    path: str
    message: str
    offering_id: str | None = None
    raw_value: Any = None


@dataclass(frozen=True)
class AdvertisedQuoteBasis:
    stream_id: str
    price_kind: str
    currency: str
    unit: str
    billing_dimension: str
    tier: str
    interpretation_policy: str
    conditions_key: str


@dataclass(frozen=True)
class AdvertisedTokenQuote:
    basis: AdvertisedQuoteBasis
    input_usd_per_million: Decimal | None
    output_usd_per_million: Decimal | None


@dataclass(frozen=True)
class CatalogObservation:
    """One exact catalog ID, including any variant suffix; not an endpoint.

    Zero input/output rates do not establish that all charges are zero.
    Unsupported pricing must be considered before interpreting these rates.
    """

    provider: str
    offering_id: str
    observed_at: datetime
    source: SourceMetadata
    input_usd_per_million: Decimal | None
    output_usd_per_million: Decimal | None
    unsupported_pricing: dict[str, Any]
    raw_offering: dict[str, Any]
    advertised_quote: AdvertisedTokenQuote | None = None


@dataclass(frozen=True)
class ParseResult:
    """Invalid structure/duplicate IDs reject all rows; price errors are local.

    accepted means the catalog structure is usable, not that all prices are
    known or all pricing conditions are supported. raw_catalog is exact input.
    """

    accepted: bool
    observations: tuple[CatalogObservation, ...]
    diagnostics: tuple[Diagnostic, ...]
    observed_at: datetime
    source: SourceMetadata
    raw_catalog: str
