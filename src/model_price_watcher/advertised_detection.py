"""Exact advertised quote comparisons over complete successful Standard history."""
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum

from model_price_watcher.models import AdvertisedQuoteBasis, AdvertisedTokenQuote, CatalogObservation
from model_price_watcher.providers.cheaper_inference import STREAM_ID, validate_public_source, validate_public_observation
from model_price_watcher.storage import SnapshotRecord, ObservationRecord
from model_price_watcher.detection import (ComponentDirection, AggregateDirection, PercentageStatus,
                                          Presence, SnapshotFrame, compare_component)


class AdvertisedComparability(str, Enum):
    COMPARABLE = 'COMPARABLE'
    BASIS_CHANGED = 'BASIS_CHANGED'
    QUOTE_UNSUPPORTED = 'QUOTE_UNSUPPORTED'
    TIME_ORDER_RESET = 'TIME_ORDER_RESET'


class AdvertisedResetReason(str, Enum):
    FIRST_OBSERVATION = 'FIRST_OBSERVATION'
    RETURN_AFTER_ABSENCE = 'RETURN_AFTER_ABSENCE'
    BASIS_CHANGED = 'BASIS_CHANGED'
    QUOTE_UNSUPPORTED = 'QUOTE_UNSUPPORTED'
    TIME_ORDER_RESET = 'TIME_ORDER_RESET'
    UNKNOWN_COMPONENT = 'UNKNOWN_COMPONENT'
    ABSENCE = 'ABSENCE'


@dataclass(frozen=True)
class AdvertisedComponentChange:
    previous: Decimal | None
    current: Decimal | None
    direction: ComponentDirection
    percentage: Decimal | None
    percentage_status: PercentageStatus


@dataclass(frozen=True)
class AdvertisedComparison:
    reason: AdvertisedComparability
    previous_snapshot_id: int
    current_snapshot_id: int
    previous_basis: AdvertisedQuoteBasis | None
    current_basis: AdvertisedQuoteBasis | None
    input: AdvertisedComponentChange | None
    output: AdvertisedComponentChange | None
    aggregate: AggregateDirection


@dataclass(frozen=True)
class AdvertisedDecreaseEvent:
    previous_snapshot_id: int
    current_snapshot_id: int
    observed_at: datetime
    previous_quote: AdvertisedTokenQuote
    current_quote: AdvertisedTokenQuote
    input: AdvertisedComponentChange
    output: AdvertisedComponentChange


@dataclass(frozen=True)
class AdvertisedOfferingDetection:
    offering_id: str
    presence: Presence
    latest_snapshot_id: int | None
    latest_observed_at: datetime | None
    current_quote: AdvertisedTokenQuote | None
    comparison: AdvertisedComparison | None
    reset_reason: AdvertisedResetReason | None
    decrease_event: AdvertisedDecreaseEvent | None


@dataclass(frozen=True)
class AdvertisedDiagnostic:
    code: AdvertisedResetReason
    offering_id: str
    snapshot_ids: tuple[int, ...]


@dataclass(frozen=True)
class AdvertisedDetectionReport:
    stream_id: str
    evaluated_at: datetime
    latest_snapshot_id: int | None
    latest_completed_at: datetime | None
    offerings: tuple[AdvertisedOfferingDetection, ...]
    diagnostics: tuple[AdvertisedDiagnostic, ...]


def _utc(value):
    if not isinstance(value, datetime):
        raise TypeError('timestamp must be datetime')
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError('timestamp must be aware')
    try:
        return value.astimezone(timezone.utc)
    except (OverflowError, ValueError) as error:
        raise ValueError('timestamp is not representable in UTC') from error


def _identity(value):
    if not isinstance(value, str):
        raise TypeError('offering ID must be string')
    if not value.strip() or '\0' in value:
        raise ValueError('offering ID must be nonblank and NUL-free')
    value.encode('utf-8')


def _validate_frames(frames):
    if isinstance(frames, (str, bytes)) or not isinstance(frames, Sequence):
        raise TypeError('frames must be a sequence')
    result, seen, previous_key = [], set(), None
    for frame in frames:
        if not isinstance(frame, SnapshotFrame) or not isinstance(frame.snapshot, SnapshotRecord):
            raise TypeError('frames must contain SnapshotFrame with SnapshotRecord')
        snapshot = frame.snapshot
        if not isinstance(snapshot.id, int) or isinstance(snapshot.id, bool):
            raise TypeError('snapshot ID must be int')
        if not isinstance(snapshot.provider, str):
            raise TypeError('stream must be string')
        if snapshot.provider != STREAM_ID:
            raise ValueError('expected public Standard stream')
        start, end = _utc(snapshot.started_at), _utc(snapshot.completed_at)
        if end < start:
            raise ValueError('invalid snapshot interval')
        key = (end, snapshot.id)
        if snapshot.id in seen or (previous_key is not None and key <= previous_key):
            raise ValueError('duplicate or unordered snapshots')
        seen.add(snapshot.id)
        previous_key = key
        validate_public_source(snapshot.source)
        if isinstance(frame.observations, (str, bytes)) or not isinstance(frame.observations, Sequence):
            raise TypeError('observations must be a sequence')
        rows, identities, times = [], set(), set()
        for item in frame.observations:
            if not isinstance(item, ObservationRecord):
                raise TypeError('expected ObservationRecord')
            if not isinstance(item.snapshot_id, int) or isinstance(item.snapshot_id, bool):
                raise TypeError('observation snapshot ID must be int')
            if item.snapshot_id != snapshot.id:
                raise ValueError('invalid observation membership')
            _identity(item.offering_id)
            if item.offering_id in identities:
                raise ValueError('duplicate offering identity')
            identities.add(item.offering_id)
            observed = _utc(item.observed_at)
            if not start <= observed <= end:
                raise ValueError('observation outside interval')
            times.add(observed)
            validate_public_observation(CatalogObservation(STREAM_ID, item.offering_id, observed, snapshot.source,
                item.input_per_million, item.output_per_million, item.conditions, item.source_record, item.advertised_quote), source=snapshot.source)
            rows.append(replace(item, observed_at=observed))
        if len(times) > 1:
            raise ValueError('inconsistent per-snapshot observation times')
        result.append(SnapshotFrame(replace(snapshot, started_at=start, completed_at=end), tuple(rows)))
    return tuple(result)


def _component(previous, current):
    change = compare_component(previous, current)
    return AdvertisedComponentChange(change.previous, change.current, change.direction, change.percentage, change.percentage_status)


def _compare(previous, current, a, b):
    qa, qb = a.advertised_quote, b.advertised_quote
    basis_a, basis_b = (None if qa is None else qa.basis), (None if qb is None else qb.basis)
    reason = AdvertisedComparability.COMPARABLE
    if current.snapshot.started_at < previous.snapshot.completed_at or b.observed_at <= a.observed_at:
        reason = AdvertisedComparability.TIME_ORDER_RESET
    elif qa is None or qb is None:
        reason = AdvertisedComparability.QUOTE_UNSUPPORTED
    elif basis_a != basis_b:
        reason = AdvertisedComparability.BASIS_CHANGED
    if reason != AdvertisedComparability.COMPARABLE:
        return AdvertisedComparison(reason, previous.snapshot.id, current.snapshot.id, basis_a, basis_b, None, None, AggregateDirection.NONCOMPARABLE)
    inp = _component(qa.input_usd_per_million, qb.input_usd_per_million)
    out = _component(qa.output_usd_per_million, qb.output_usd_per_million)
    directions = (inp.direction, out.direction)
    unknown = directions.count(ComponentDirection.UNKNOWN)
    if unknown:
        aggregate = AggregateDirection.NONCOMPARABLE if unknown == 2 else AggregateDirection.PARTIAL
    elif ComponentDirection.DECREASE in directions and ComponentDirection.INCREASE in directions:
        aggregate = AggregateDirection.MIXED
    elif ComponentDirection.DECREASE in directions:
        aggregate = AggregateDirection.DECREASE
    elif ComponentDirection.INCREASE in directions:
        aggregate = AggregateDirection.INCREASE
    else:
        aggregate = AggregateDirection.UNCHANGED
    return AdvertisedComparison(reason, previous.snapshot.id, current.snapshot.id, basis_a, basis_b, inp, out, aggregate)


def compare_advertised_observations(previous: SnapshotFrame, current: SnapshotFrame, *, offering_id: str) -> AdvertisedComparison:
    previous, current = _validate_frames((previous, current))
    _identity(offering_id)
    a = next((r for r in previous.observations if r.offering_id == offering_id), None)
    b = next((r for r in current.observations if r.offering_id == offering_id), None)
    if a is None or b is None:
        raise ValueError('offering must occur in both frames')
    return _compare(previous, current, a, b)


def analyze_advertised_history(frames: Sequence[SnapshotFrame], *, now: datetime) -> AdvertisedDetectionReport:
    frames = _validate_frames(frames)
    now = _utc(now)
    return _reduce_history(frames, now)


def _reduce_history(frames, now):
    """Internal reduction over already validated UTC frames and evaluation time."""
    if frames and now < frames[-1].snapshot.completed_at:
        raise ValueError('now precedes latest completion')
    states, diagnostics, prior_rows, prior_frame = {}, [], {}, None
    for frame in frames:
        current_rows = {r.offering_id: r for r in frame.observations}
        # Only transitions out of the immediately previous presence set emit absence.
        for identity in sorted(prior_rows.keys() | current_rows.keys()):
            row = current_rows.get(identity)
            old = states.get(identity)
            comparison, event, reset = None, None, None
            if row is None:
                reset = AdvertisedResetReason.ABSENCE
                states[identity] = AdvertisedOfferingDetection(identity, Presence.NO_LONGER_OBSERVED, None, None, None, None, reset, None)
            else:
                if old is None:
                    reset = AdvertisedResetReason.FIRST_OBSERVATION
                elif identity not in prior_rows:
                    reset = AdvertisedResetReason.RETURN_AFTER_ABSENCE
                else:
                    previous = prior_rows[identity]
                    comparison = _compare(prior_frame, frame, previous, row)
                    if comparison.reason != AdvertisedComparability.COMPARABLE:
                        reset = AdvertisedResetReason(comparison.reason.value)
                    elif comparison.aggregate in (AggregateDirection.PARTIAL, AggregateDirection.NONCOMPARABLE):
                        reset = AdvertisedResetReason.UNKNOWN_COMPONENT
                    elif comparison.aggregate == AggregateDirection.DECREASE:
                        event = AdvertisedDecreaseEvent(prior_frame.snapshot.id, frame.snapshot.id, row.observed_at,
                            previous.advertised_quote, row.advertised_quote, comparison.input, comparison.output)
                    elif comparison.aggregate == AggregateDirection.UNCHANGED:
                        event = old.decrease_event
                states[identity] = AdvertisedOfferingDetection(identity, Presence.CURRENT, frame.snapshot.id, row.observed_at,
                                                               row.advertised_quote, comparison, reset, event)
            if reset is not None:
                ids = (frame.snapshot.id,) if comparison is None else (prior_frame.snapshot.id, frame.snapshot.id)
                diagnostics.append(AdvertisedDiagnostic(reset, identity, ids))
        prior_rows, prior_frame = current_rows, frame
    latest = frames[-1].snapshot if frames else None
    return AdvertisedDetectionReport(STREAM_ID, now, None if latest is None else latest.id,
        None if latest is None else latest.completed_at, tuple(states[k] for k in sorted(states)), tuple(diagnostics))
