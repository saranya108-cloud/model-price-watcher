"""Read-only selection of advertised public Standard price evidence."""
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone

from model_price_watcher.advertised_detection import (
    AdvertisedOfferingDetection, AdvertisedDiagnostic, _validate_frames, _reduce_history,
)
from model_price_watcher.detection import SnapshotFrame, Presence
from model_price_watcher.queries import LookupStatus
from model_price_watcher.storage import SnapshotRecord, ObservationRecord, get_successful_history
from model_price_watcher.providers.cheaper_inference import STREAM_ID, CATALOG_SCOPE, PRICE_KIND


@dataclass(frozen=True)
class SelectedAdvertisedLookup:
    offering_id: str
    status: LookupStatus
    current_observation: ObservationRecord | None
    detection: AdvertisedOfferingDetection | None


@dataclass(frozen=True)
class SelectedAdvertisedReport:
    stream_id: str
    catalog_scope: str
    price_kind: str
    evaluated_at: datetime
    latest_snapshot: SnapshotRecord | None
    lookups: tuple[SelectedAdvertisedLookup, ...]
    diagnostics: tuple[AdvertisedDiagnostic, ...]

    @property
    def recent_observed_advertised_decrease_ids(self):
        return tuple(item.offering_id for item in self.lookups
                     if item.status == LookupStatus.CURRENT
                     and item.detection is not None and item.detection.decrease_event is not None
                     and timedelta(0) <= self.evaluated_at - item.detection.decrease_event.observed_at < timedelta(days=7))

    @property
    def zero_advertised_base_token_rate_ids(self):
        return tuple(item.offering_id for item in self.lookups
                     if item.status == LookupStatus.CURRENT and item.detection is not None
                     and item.detection.current_quote is not None
                     and item.detection.current_quote.input_usd_per_million == 0
                     and item.detection.current_quote.output_usd_per_million == 0)


def _selection(offering_ids):
    if isinstance(offering_ids, (str, bytes)) or not isinstance(offering_ids, Sequence):
        raise TypeError('offering_ids must be sequence')
    for identity in offering_ids:
        if not isinstance(identity, str):
            raise TypeError('offering ID must be string')
        if not identity.strip() or '\0' in identity:
            raise ValueError('offering ID must be nonblank and NUL-free')
        identity.encode('utf-8')
    return tuple(sorted(set(offering_ids)))


def _now(now):
    if not isinstance(now, datetime):
        raise TypeError('now must be datetime')
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError('now must be aware')
    try:
        return now.astimezone(timezone.utc)
    except (OverflowError, ValueError) as error:
        raise ValueError('now must be representable in UTC') from error


def select_advertised_offerings(frames: Sequence[SnapshotFrame], *, offering_ids: Sequence[str], now: datetime) -> SelectedAdvertisedReport:
    frames = _validate_frames(frames)
    selected = _selection(offering_ids)
    reduced = _reduce_history(frames, _now(now))
    latest = frames[-1].snapshot if frames else None
    if latest is not None:
        latest = replace(latest, started_at=latest.started_at.astimezone(timezone.utc), completed_at=latest.completed_at.astimezone(timezone.utc))
    current = {r.offering_id: replace(r, observed_at=r.observed_at.astimezone(timezone.utc)) for r in frames[-1].observations} if frames else {}
    detections = {r.offering_id: r for r in reduced.offerings}
    lookups = []
    for identity in selected:
        observation, detection = current.get(identity), detections.get(identity)
        if observation is not None:
            if detection is None or detection.presence != Presence.CURRENT or detection.latest_snapshot_id != latest.id or observation.snapshot_id != latest.id or detection.latest_observed_at != observation.observed_at:
                raise RuntimeError('inconsistent current advertised evidence')
            status = LookupStatus.CURRENT
        elif detection is not None:
            if detection.presence != Presence.NO_LONGER_OBSERVED:
                raise RuntimeError('inconsistent absent advertised evidence')
            status = LookupStatus.NO_LONGER_OBSERVED
        else:
            status = LookupStatus.NEVER_OBSERVED
        lookups.append(SelectedAdvertisedLookup(identity, status, observation, detection))
    selected_set = set(selected)
    return SelectedAdvertisedReport(STREAM_ID, CATALOG_SCOPE, PRICE_KIND, reduced.evaluated_at, latest,
        tuple(lookups), tuple(d for d in reduced.diagnostics if d.offering_id in selected_set))


def view_selected_advertised_offerings(connection: sqlite3.Connection, *, offering_ids: Sequence[str], now: datetime) -> SelectedAdvertisedReport:
    """Participate in a caller's transaction, otherwise own one read transaction.

    Rollback failure retains the original exception and rollback context; discard
    that indeterminate connection. This function never migrates or repairs it.
    """
    if not isinstance(connection, sqlite3.Connection):
        raise TypeError('connection must be sqlite3.Connection')
    selected = _selection(offering_ids)
    now = _now(now)
    owned = False
    if not connection.in_transaction:
        connection.execute('BEGIN')
        owned = True
    try:
        frames = tuple(SnapshotFrame(s, r) for s, r in get_successful_history(connection, STREAM_ID))
        report = select_advertised_offerings(frames, offering_ids=selected, now=now)
        if owned:
            connection.execute('COMMIT')
        return report
    except BaseException as error:
        if owned:
            try:
                if connection.in_transaction:
                    connection.rollback()
            except BaseException:
                raise error
        raise
