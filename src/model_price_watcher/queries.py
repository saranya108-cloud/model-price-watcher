"""Read-only price evidence for caller-selected exact offering identities.

Selection is a projection after complete history reduction. Detection owns all
price interpretation; this module neither recommends offerings nor establishes
free execution. Frozen records are shallow: nested stored evidence should be
treated as read-only, but is not deeply immutable.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from model_price_watcher.detection import (
    DealStatus,
    DetectionDiagnostic,
    OfferingDetection,
    Presence,
    SnapshotFrame,
    TokenPricing,
    analyze_history,
)
from model_price_watcher.storage import (
    ObservationRecord,
    SnapshotRecord,
    get_successful_history,
)


class LookupStatus(str, Enum):
    CURRENT = "CURRENT"
    NO_LONGER_OBSERVED = "NO_LONGER_OBSERVED"
    NEVER_OBSERVED = "NEVER_OBSERVED"


@dataclass(frozen=True)
class SelectedOfferingLookup:
    """An exact ID within the report's provider.

    NEVER_OBSERVED means absent from stored successful history only, not an
    invalid or nonexistent provider offering. Only CURRENT has a current row.
    """

    offering_id: str
    status: LookupStatus
    current_observation: ObservationRecord | None
    detection: OfferingDetection | None


@dataclass(frozen=True)
class SelectedOfferingReport:
    """Selected evidence; latest_snapshot=None means no successful history."""

    provider: str
    evaluated_at: datetime
    latest_snapshot: SnapshotRecord | None
    lookups: tuple[SelectedOfferingLookup, ...]
    diagnostics: tuple[DetectionDiagnostic, ...]

    @property
    def active_observed_decrease_ids(self) -> tuple[str, ...]:
        """Active decreases observed in stored history, not declared promotions."""
        return tuple(
            lookup.offering_id for lookup in self.lookups
            if lookup.status is LookupStatus.CURRENT
            and lookup.detection is not None
            and lookup.detection.deal_status is DealStatus.ACTIVE
        )

    @property
    def zero_token_price_ids(self) -> tuple[str, ...]:
        """Both normalized token prices are zero; this does not imply free use."""
        return tuple(
            lookup.offering_id for lookup in self.lookups
            if lookup.status is LookupStatus.CURRENT
            and lookup.detection is not None
            and lookup.detection.token_pricing is TokenPricing.ZERO_TOKEN_PRICES
        )


def select_offerings(
    frames: Sequence[SnapshotFrame],
    *,
    provider: str,
    offering_ids: Sequence[str],
    now: datetime,
) -> SelectedOfferingReport:
    """Reduce complete successful history, then project exact selected IDs.

    Empty selection selects nothing. Exact duplicates collapse and results use
    ordinary case-sensitive string order. Whitespace in nonblank IDs matters.
    Invalid history/time propagates from detection, even for empty selections.
    """
    _require_identity(provider, "provider")
    if isinstance(offering_ids, (str, bytes)) or not isinstance(offering_ids, Sequence):
        raise TypeError("offering_ids must be a sequence")
    selected = set()
    for offering_id in offering_ids:
        _require_identity(offering_id, "offering_id")
        selected.add(offering_id)

    reduced = analyze_history(frames, provider=provider, now=now)
    latest = frames[-1].snapshot if frames else None
    if (
        reduced.provider != provider
        or reduced.latest_snapshot_id != (None if latest is None else latest.id)
        or reduced.latest_completed_at != (None if latest is None else latest.completed_at)
    ):
        raise RuntimeError("inconsistent detection/latest snapshot evidence")
    detections = {item.offering_id: item for item in reduced.offerings}
    current = {
        item.offering_id: item for item in frames[-1].observations
    } if latest is not None else {}
    lookups = []
    for offering_id in sorted(selected):
        observation = current.get(offering_id)
        detection = detections.get(offering_id)
        if detection is not None and detection.provider != provider:
            raise RuntimeError("inconsistent detection provider evidence")
        if observation is not None:
            if (
                latest is None
                or observation.snapshot_id != latest.id
                or detection is None
                or detection.presence is not Presence.CURRENT
                or detection.latest_snapshot_id != latest.id
                or detection.latest_observed_at != observation.observed_at
            ):
                raise RuntimeError("inconsistent current observation/detection evidence")
            status = LookupStatus.CURRENT
        elif detection is not None:
            if latest is None or detection.presence is not Presence.NO_LONGER_OBSERVED:
                raise RuntimeError("inconsistent absent observation/detection evidence")
            status = LookupStatus.NO_LONGER_OBSERVED
        else:
            status = LookupStatus.NEVER_OBSERVED
        lookups.append(SelectedOfferingLookup(offering_id, status, observation, detection))
    return SelectedOfferingReport(
        provider, reduced.evaluated_at, latest, tuple(lookups),
        tuple(item for item in reduced.diagnostics
              if item.offering_id is None or item.offering_id in selected),
    )


def view_selected_offerings(
    connection: sqlite3.Connection,
    *,
    provider: str,
    offering_ids: Sequence[str],
    now: datetime,
) -> SelectedOfferingReport:
    """Read, analyze, and project in one transaction on an existing connection.

    Caller-owned transactions remain under caller ownership on success or
    failure. Only a transaction started here is committed or rolled back.
    Rollback failure preserves the original exception; connection state may
    then be indeterminate, as with the storage and detection wrappers.
    """
    if not isinstance(connection, sqlite3.Connection):
        raise TypeError("connection must be a sqlite3.Connection")
    owned = False
    if not connection.in_transaction:
        connection.execute("BEGIN")
        owned = True
    try:
        frames = tuple(
            SnapshotFrame(snapshot, observations)
            for snapshot, observations in get_successful_history(connection, provider)
        )
        report = select_offerings(
            frames, provider=provider, offering_ids=offering_ids, now=now,
        )
        if owned:
            connection.execute("COMMIT")
        return report
    except BaseException as error:
        if owned:
            try:
                if connection.in_transaction:
                    connection.rollback()
            except BaseException:
                raise error
        raise


def _require_identity(value: str, name: str) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    if not value.strip():
        raise ValueError(f"{name} must be nonblank")
