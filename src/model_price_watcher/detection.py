"""Change and deal detection over successful catalog history.

Comparisons use exact offering identity and stored token prices only.
Unsupported pricing remains opaque evidence. This module never claims that a
request is free, infers promotions, or scores models.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import (
    MAX_EMAX,
    MAX_PREC,
    MIN_EMIN,
    Context,
    Decimal,
    DivisionByZero,
    Inexact,
    InvalidOperation,
    Overflow,
    Rounded,
    Underflow,
    ROUND_HALF_EVEN,
    localcontext,
)
from enum import Enum
from itertools import zip_longest
from typing import Any

from model_price_watcher.storage import (
    ObservationRecord,
    SnapshotRecord,
    get_successful_history,
)


VISIBILITY_WINDOW = timedelta(days=7)
_PERCENTAGE_PRECISION = 34
_HUNDRED = Decimal(100)
_ZERO = Decimal(0)


class ComponentDirection(str, Enum):
    UNCHANGED = "UNCHANGED"
    DECREASE = "DECREASE"
    INCREASE = "INCREASE"
    UNKNOWN = "UNKNOWN"


class AggregateDirection(str, Enum):
    UNCHANGED = "UNCHANGED"
    DECREASE = "DECREASE"
    INCREASE = "INCREASE"
    MIXED = "MIXED"
    PARTIAL = "PARTIAL"
    NONCOMPARABLE = "NONCOMPARABLE"


class Presence(str, Enum):
    CURRENT = "CURRENT"
    NO_LONGER_OBSERVED = "NO_LONGER_OBSERVED"


class BaselineReason(str, Enum):
    FIRST_OBSERVATION = "FIRST_OBSERVATION"
    RETURN_AFTER_ABSENCE = "RETURN_AFTER_ABSENCE"
    COMPARABILITY_RESTORED = "COMPARABILITY_RESTORED"
    CONDITIONS_CHANGED = "CONDITIONS_CHANGED"
    TIME_ORDER_RESET = "TIME_ORDER_RESET"


class DealStatus(str, Enum):
    NONE = "NONE"
    ACTIVE = "ACTIVE"
    EXPIRED = "EXPIRED"
    SUPPRESSED = "SUPPRESSED"


class PercentageStatus(str, Enum):
    EXACT = "EXACT"
    ROUNDED = "ROUNDED"
    UNKNOWN_PRICE = "UNKNOWN_PRICE"
    ZERO_BASE = "ZERO_BASE"
    NUMERIC_RANGE = "NUMERIC_RANGE"


class TokenPricing(str, Enum):
    ZERO_TOKEN_PRICES = "ZERO_TOKEN_PRICES"
    KNOWN_NONZERO_TOKEN_PRICING = "KNOWN_NONZERO_TOKEN_PRICING"
    INCOMPLETE_TOKEN_PRICING = "INCOMPLETE_TOKEN_PRICING"


@dataclass(frozen=True)
class ComponentChange:
    previous: Decimal | None
    current: Decimal | None
    direction: ComponentDirection
    meaningful: bool | None
    percentage: Decimal | None
    percentage_status: PercentageStatus


@dataclass(frozen=True)
class TokenComparison:
    input: ComponentChange
    output: ComponentChange
    aggregate: AggregateDirection
    conditions_changed: bool
    unsupported_present: bool
    deal_eligible: bool


@dataclass(frozen=True)
class SnapshotFrame:
    snapshot: SnapshotRecord
    observations: tuple[ObservationRecord, ...]


@dataclass(frozen=True)
class OfferingDetection:
    provider: str
    offering_id: str
    presence: Presence
    baseline_reason: BaselineReason | None
    latest_snapshot_id: int | None
    latest_observed_at: datetime | None
    current_state_start_snapshot_id: int | None
    current_state_started_at: datetime | None
    preceding_snapshot_id: int | None
    preceding_observed_at: datetime | None
    comparison: TokenComparison | None
    token_pricing: TokenPricing | None
    unsupported_pricing_present: bool
    deal_status: DealStatus


@dataclass(frozen=True)
class DetectionDiagnostic:
    code: str
    offering_id: str | None = None
    snapshot_ids: tuple[int, ...] = ()


@dataclass(frozen=True)
class DetectionReport:
    provider: str
    latest_snapshot_id: int | None
    latest_completed_at: datetime | None
    evaluated_at: datetime
    offerings: tuple[OfferingDetection, ...]
    diagnostics: tuple[DetectionDiagnostic, ...]


def compare_component(
    previous: Decimal | None,
    current: Decimal | None,
) -> ComponentChange:
    """Compare one token-price component without inheriting ambient Decimal state."""
    _require_money(previous)
    _require_money(current)
    if previous is None or current is None:
        return ComponentChange(
            previous, current, ComponentDirection.UNKNOWN, None, None,
            PercentageStatus.UNKNOWN_PRICE,
        )
    if previous == current:
        if previous == 0:
            percentage, status = None, PercentageStatus.ZERO_BASE
        else:
            percentage, status = _ZERO, PercentageStatus.EXACT
        return ComponentChange(
            previous, current, ComponentDirection.UNCHANGED, False, percentage, status,
        )
    if current < previous:
        direction = ComponentDirection.DECREASE
    else:
        direction = ComponentDirection.INCREASE
    if previous == 0 or current == 0:
        meaningful = True
    else:
        meaningful = _threshold_reached(previous, current, direction)
    percentage, status = _percentage(previous, current)
    return ComponentChange(
        previous, current, direction, meaningful, percentage, status,
    )


def compare_observations(
    previous: ObservationRecord,
    current: ObservationRecord,
) -> TokenComparison:
    """Compare two observations of one exact offering identity."""
    if not isinstance(previous, ObservationRecord) or not isinstance(current, ObservationRecord):
        raise TypeError("observations must be ObservationRecord values")
    if previous.advertised_quote is not None or current.advertised_quote is not None:
        raise ValueError('Use advertised comparison APIs for advertised quotes')
    if previous.offering_id != current.offering_id:
        raise ValueError("offering identity mismatch")
    if not isinstance(previous.conditions, dict) or not isinstance(current.conditions, dict):
        raise TypeError("conditions must be a dictionary")
    input_change = compare_component(previous.input_per_million, current.input_per_million)
    output_change = compare_component(previous.output_per_million, current.output_per_million)
    conditions_changed = not _evidence_equal(previous.conditions, current.conditions)
    unsupported_present = (
        _unsupported_present(previous.conditions)
        or _unsupported_present(current.conditions)
    )
    aggregate = _aggregate(input_change, output_change)
    deal_eligible = (
        input_change.direction is not ComponentDirection.UNKNOWN
        and output_change.direction is not ComponentDirection.UNKNOWN
        and (input_change.meaningful is True or output_change.meaningful is True)
        and input_change.direction is not ComponentDirection.INCREASE
        and output_change.direction is not ComponentDirection.INCREASE
        and not conditions_changed
        and not unsupported_present
    )
    return TokenComparison(
        input_change, output_change, aggregate,
        conditions_changed, unsupported_present, deal_eligible,
    )


def analyze_history(
    frames: Sequence[SnapshotFrame],
    *,
    provider: str,
    now: datetime,
) -> DetectionReport:
    """Reduce successful snapshot frames for one exact provider identity."""
    if not isinstance(provider, str):
        raise TypeError("provider must be a string")
    if provider == 'cheaper_inference' or provider.startswith('cheaper_inference.'):
        raise ValueError('Use advertised history APIs for this stream')
    now_utc = _require_aware(now, "now")
    if isinstance(frames, (str, bytes)) or not isinstance(frames, Sequence):
        raise TypeError("frames must be a sequence")
    states: dict[str, _OfferingState] = {}
    latest = None
    seen_snapshot_ids = set()
    previous_key = None
    for frame_index, frame in enumerate(frames):
        if not isinstance(frame, SnapshotFrame):
            raise TypeError("frames must contain SnapshotFrame values")
        snapshot = frame.snapshot
        if not isinstance(snapshot, SnapshotRecord):
            raise TypeError("snapshot must be SnapshotRecord")
        if snapshot.provider != provider:
            raise ValueError("snapshot provider must equal provider")
        if isinstance(snapshot.id, bool) or not isinstance(snapshot.id, int):
            raise TypeError("snapshot id must be an int")
        if snapshot.id in seen_snapshot_ids:
            raise ValueError("duplicate snapshot identity")
        seen_snapshot_ids.add(snapshot.id)
        started = _require_aware(snapshot.started_at, "started_at")
        completed = _require_aware(snapshot.completed_at, "completed_at")
        if completed < started:
            raise ValueError("completed_at must be at or after started_at")
        key = (completed, snapshot.id)
        if previous_key is not None and key < previous_key:
            raise ValueError("invalid frame ordering")
        previous_key = key
        if isinstance(frame.observations, (str, bytes)) or not isinstance(
            frame.observations, Sequence,
        ):
            raise TypeError("observations must be a sequence")
        seen_offerings = set()
        for item in frame.observations:
            if not isinstance(item, ObservationRecord):
                raise TypeError("observations must contain ObservationRecord values")
            if item.advertised_quote is not None:
                raise ValueError('Use advertised history APIs for advertised quotes')
            if item.snapshot_id != snapshot.id:
                raise ValueError("inconsistent observation/snapshot membership")
            if not isinstance(item.offering_id, str):
                raise TypeError("offering_id must be a string")
            if item.offering_id in seen_offerings:
                raise ValueError("duplicate offering identity")
            seen_offerings.add(item.offering_id)
            observed = _require_aware(item.observed_at, "observed_at")
            if observed < started or observed > completed:
                raise ValueError("observation time must be within the snapshot interval")
            _require_money(item.input_per_million)
            _require_money(item.output_per_million)
            if not isinstance(item.conditions, dict):
                raise TypeError("conditions must be a dictionary")
            state = states.get(item.offering_id)
            if state is None:
                state = states[item.offering_id] = _OfferingState()
            state.observe(item, frame_index)
        latest = snapshot
    if latest is not None:
        latest_completed = _require_aware(latest.completed_at, "completed_at")
        if now_utc < latest_completed:
            raise ValueError("now must not precede the latest successful snapshot")
    diagnostics = []
    offerings = []
    for offering_id in sorted(states):
        state = states[offering_id]
        offerings.append(state.finish(provider, offering_id, len(frames) - 1, now_utc))
        diagnostics.extend(state.diagnostics)
    return DetectionReport(
        provider,
        None if latest is None else latest.id,
        None if latest is None else latest.completed_at,
        now_utc,
        tuple(offerings),
        tuple(diagnostics),
    )


def detect_current(
    connection: sqlite3.Connection,
    *,
    provider: str,
    now: datetime,
) -> DetectionReport:
    """Read one provider's successful history consistently, then analyze it."""
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
        report = analyze_history(frames, provider=provider, now=now)
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


def _require_money(value: Decimal | None) -> None:
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, Decimal):
        raise TypeError("monetary value must be Decimal or None")
    if not value.is_finite():
        raise ValueError("monetary value must be finite")
    if value < 0:
        raise ValueError("monetary value must be non-negative")


def _require_aware(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError(f"{name} must be a datetime")
    try:
        offset = None if value.tzinfo is None else value.utcoffset()
    except (OverflowError, OSError, ValueError) as error:
        raise ValueError(f"{name} is not representable in UTC") from error
    if value.tzinfo is None or offset is None:
        raise ValueError(f"{name} must be timezone-aware")
    try:
        return value.astimezone(timezone.utc)
    except (OverflowError, OSError, ValueError) as error:
        raise ValueError(f"{name} is not representable in UTC") from error


def _percentage_context() -> Context:
    context = Context(
        prec=_PERCENTAGE_PRECISION,
        rounding=ROUND_HALF_EVEN,
        Emax=MAX_EMAX,
        Emin=MIN_EMIN,
        capitals=0,
        clamp=0,
    )
    context.traps[InvalidOperation] = True
    context.traps[DivisionByZero] = True
    context.traps[Overflow] = True
    context.traps[Underflow] = True
    context.clear_flags()
    return context


def _percentage(
    previous: Decimal, current: Decimal,
) -> tuple[Decimal | None, PercentageStatus]:
    if previous == 0:
        return None, PercentageStatus.ZERO_BASE
    if previous == current:
        return _ZERO, PercentageStatus.EXACT
    if current == 0:
        return _HUNDRED, PercentageStatus.EXACT
    try:
        with localcontext(_percentage_context()) as context:
            result = (previous - current) / previous * _HUNDRED
            if not result.is_finite():
                return None, PercentageStatus.NUMERIC_RANGE
            if context.flags[Inexact] or context.flags[Rounded]:
                return result, PercentageStatus.ROUNDED
            return result, PercentageStatus.EXACT
    except (InvalidOperation, DivisionByZero, Overflow, Underflow, ArithmeticError):
        return None, PercentageStatus.NUMERIC_RANGE


def _multiply_digits(digits: tuple[int, ...], factor: int) -> tuple[int, ...]:
    """Multiply a nonzero coefficient by a small integer in linear digit work."""
    result = []
    carry = 0
    for digit in reversed(digits):
        carry, remainder = divmod(digit * factor + carry, 10)
        result.append(remainder)
    while carry:
        carry, remainder = divmod(carry, 10)
        result.append(remainder)
    return tuple(reversed(result))


def _cmp_scaled(
    left_digits: tuple[int, ...], left_exp: int,
    right_digits: tuple[int, ...], right_exp: int,
) -> int:
    # Nonzero Decimal coefficients have no leading zeros. Compare magnitude
    # first, then significands with implicit trailing zeros. Exponent gaps
    # never cause allocation or iteration over the gap.
    left_sci = left_exp + len(left_digits)
    right_sci = right_exp + len(right_digits)
    if left_sci != right_sci:
        return 1 if left_sci > right_sci else -1
    for left, right in zip_longest(left_digits, right_digits, fillvalue=0):
        if left != right:
            return 1 if left > right else -1
    return 0


def _threshold_reached(
    previous: Decimal,
    current: Decimal,
    direction: ComponentDirection,
) -> bool:
    previous_parts = previous.as_tuple()
    current_parts = current.as_tuple()
    factor = 95 if direction is ComponentDirection.DECREASE else 105
    comparison = _cmp_scaled(
        current_parts.digits, current_parts.exponent + 2,
        _multiply_digits(previous_parts.digits, factor), previous_parts.exponent,
    )
    return comparison <= 0 if direction is ComponentDirection.DECREASE else comparison >= 0


def _is_number(value: Any) -> bool:
    return isinstance(value, Decimal) or (
        isinstance(value, int) and not isinstance(value, bool)
    )


def _as_decimal(value: Decimal | int) -> Decimal:
    if isinstance(value, Decimal):
        return value
    context = Context(
        prec=MAX_PREC,
        Emax=MAX_EMAX,
        Emin=MIN_EMIN,
        capitals=0,
        clamp=0,
    )
    context.traps[InvalidOperation] = True
    context.clear_flags()
    with localcontext(context):
        return Decimal(value)


def _evidence_equal(left: Any, right: Any) -> bool:
    if left is right:
        return True
    if left is None or right is None:
        return left is right
    if isinstance(left, bool) or isinstance(right, bool):
        return isinstance(left, bool) and isinstance(right, bool) and left is right
    if _is_number(left) and _is_number(right):
        return _as_decimal(left) == _as_decimal(right)
    if isinstance(left, str) and isinstance(right, str):
        return left == right
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(
            _evidence_equal(left_item, right_item)
            for left_item, right_item in zip(left, right)
        )
    if isinstance(left, dict) and isinstance(right, dict):
        if left.keys() != right.keys():
            return False
        return all(_evidence_equal(left[key], right[key]) for key in left)
    return False


def _unsupported_present(conditions: dict[str, Any]) -> bool:
    return len(conditions) != 0


def _same_state(previous: ObservationRecord, current: ObservationRecord) -> bool:
    return (
        previous.input_per_million == current.input_per_million
        and previous.output_per_million == current.output_per_million
        and _evidence_equal(previous.conditions, current.conditions)
    )


def _both_known(observation: ObservationRecord) -> bool:
    return (
        observation.input_per_million is not None
        and observation.output_per_million is not None
    )


def _aggregate(input_change: ComponentChange, output_change: ComponentChange) -> AggregateDirection:
    directions = (input_change.direction, output_change.direction)
    if directions == (ComponentDirection.UNKNOWN, ComponentDirection.UNKNOWN):
        return AggregateDirection.NONCOMPARABLE
    if ComponentDirection.UNKNOWN in directions:
        return AggregateDirection.PARTIAL
    if (
        ComponentDirection.DECREASE in directions
        and ComponentDirection.INCREASE in directions
    ):
        return AggregateDirection.MIXED
    if directions == (ComponentDirection.UNCHANGED, ComponentDirection.UNCHANGED):
        return AggregateDirection.UNCHANGED
    if ComponentDirection.DECREASE in directions:
        return AggregateDirection.DECREASE
    if ComponentDirection.INCREASE in directions:
        return AggregateDirection.INCREASE
    return AggregateDirection.UNCHANGED


def _has_decrease(comparison: TokenComparison) -> bool:
    return (
        comparison.input.direction is ComponentDirection.DECREASE
        or comparison.output.direction is ComponentDirection.DECREASE
    )


def _has_meaningful_decrease(comparison: TokenComparison) -> bool:
    return (
        comparison.input.direction is ComponentDirection.DECREASE
        and comparison.input.meaningful is True
    ) or (
        comparison.output.direction is ComponentDirection.DECREASE
        and comparison.output.meaningful is True
    )


def _token_pricing(observation: ObservationRecord) -> TokenPricing:
    amount_in = observation.input_per_million
    amount_out = observation.output_per_million
    if amount_in is None or amount_out is None:
        return TokenPricing.INCOMPLETE_TOKEN_PRICING
    if amount_in == 0 and amount_out == 0:
        return TokenPricing.ZERO_TOKEN_PRICES
    return TokenPricing.KNOWN_NONZERO_TOKEN_PRICING


def _classify_deal(
    *,
    comparison: TokenComparison | None,
    continuity: bool,
    started_at: datetime,
    now: datetime,
) -> DealStatus:
    if comparison is None:
        return DealStatus.NONE
    if comparison.deal_eligible and continuity:
        elapsed = now - started_at
        if elapsed < timedelta(0):
            return DealStatus.NONE
        if elapsed < VISIBILITY_WINDOW:
            return DealStatus.ACTIVE
        return DealStatus.EXPIRED
    if comparison.deal_eligible and not continuity:
        return DealStatus.SUPPRESSED
    if comparison.aggregate is AggregateDirection.MIXED:
        return DealStatus.SUPPRESSED
    if comparison.aggregate is AggregateDirection.PARTIAL and _has_decrease(comparison):
        return DealStatus.SUPPRESSED
    if comparison.conditions_changed and _has_decrease(comparison):
        return DealStatus.SUPPRESSED
    if comparison.unsupported_present and _has_meaningful_decrease(comparison):
        return DealStatus.SUPPRESSED
    return DealStatus.NONE


def _note_percentage_range(
    comparison: TokenComparison,
    offering_id: str,
    diagnostics: list[DetectionDiagnostic],
    snapshot_id: int,
) -> None:
    if (
        comparison.input.percentage_status is PercentageStatus.NUMERIC_RANGE
        or comparison.output.percentage_status is PercentageStatus.NUMERIC_RANGE
    ):
        diagnostics.append(DetectionDiagnostic(
            "PERCENTAGE_NUMERIC_RANGE", offering_id, (snapshot_id,),
        ))


@dataclass
class _OfferingState:
    """One offering's reduction state; frame positions expose absence lazily."""

    last_present: ObservationRecord | None = None
    current_start: ObservationRecord | None = None
    current_latest: ObservationRecord | None = None
    preceding: ObservationRecord | None = None
    comparison: TokenComparison | None = None
    continuity: bool = False
    baseline_reason: BaselineReason | None = None
    last_seen: ObservationRecord | None = None
    in_gap: bool = False
    last_frame_index: int = -1
    diagnostics: list[DetectionDiagnostic] = field(default_factory=list)

    def observe(self, observation: ObservationRecord, frame_index: int) -> None:
        offering_id = observation.offering_id
        # A skipped frame, even an empty one, breaks continuity. No traversal of
        # all historical identities is needed when an offering disappears.
        if self.last_frame_index >= 0 and self.last_frame_index != frame_index - 1:
            self.current_start = None
            self.current_latest = None
            self.preceding = None
            self.comparison = None
            self.continuity = False
            self.baseline_reason = None
            self.in_gap = True
            self.last_present = None
        self.last_frame_index = frame_index

        observed_at = observation.observed_at
        if self.last_present is not None:
            previous_time = self.last_present.observed_at
            if observed_at < previous_time:
                self.diagnostics.append(DetectionDiagnostic(
                    "OBSERVATION_TIME_REGRESSION",
                    offering_id,
                    (self.last_present.snapshot_id, observation.snapshot_id),
                ))
                self.current_start = observation
                self.current_latest = observation
                self.preceding = None
                self.comparison = None
                self.continuity = False
                self.baseline_reason = BaselineReason.TIME_ORDER_RESET
                self.last_present = observation
                self.last_seen = observation
                self.in_gap = False
                return
            if observed_at == previous_time:
                if self.current_latest is not None and _same_state(self.current_latest, observation):
                    self.current_latest = observation
                    self.last_present = observation
                    self.last_seen = observation
                    return
                if self.current_latest is not None:
                    self.comparison = compare_observations(self.current_latest, observation)
                    _note_percentage_range(
                        self.comparison, offering_id, self.diagnostics, observation.snapshot_id,
                    )
                self.diagnostics.append(DetectionDiagnostic(
                    "CHANGED_STATE_AT_EQUAL_TIME",
                    offering_id,
                    (self.last_present.snapshot_id, observation.snapshot_id),
                ))
                self.current_start = observation
                self.current_latest = observation
                self.preceding = None
                self.continuity = False
                self.baseline_reason = BaselineReason.TIME_ORDER_RESET
                self.last_present = observation
                self.last_seen = observation
                self.in_gap = False
                return

        if self.current_latest is None:
            self.current_start = observation
            self.current_latest = observation
            self.preceding = None
            self.comparison = None
            self.continuity = False
            self.baseline_reason = (
                BaselineReason.RETURN_AFTER_ABSENCE
                if self.in_gap and self.last_seen is not None
                else BaselineReason.FIRST_OBSERVATION
            )
            self.last_present = observation
            self.last_seen = observation
            self.in_gap = False
            return

        if _same_state(self.current_latest, observation):
            self.current_latest = observation
            self.last_present = observation
            self.last_seen = observation
            return

        new_comparison = compare_observations(self.current_latest, observation)
        _note_percentage_range(
            new_comparison, offering_id, self.diagnostics, observation.snapshot_id,
        )
        previous_known = _both_known(self.current_latest)
        current_known = _both_known(observation)
        if new_comparison.conditions_changed:
            self.baseline_reason = BaselineReason.CONDITIONS_CHANGED
            self.preceding = self.current_latest
            self.comparison = new_comparison
            self.continuity = False
        elif current_known and not previous_known:
            self.baseline_reason = BaselineReason.COMPARABILITY_RESTORED
            self.preceding = None
            self.comparison = new_comparison
            self.continuity = False
        else:
            self.baseline_reason = None
            self.preceding = self.current_latest
            self.comparison = new_comparison
            self.continuity = bool(new_comparison.deal_eligible and current_known)

        self.current_start = observation
        self.current_latest = observation
        self.last_present = observation
        self.last_seen = observation
        self.in_gap = False

    def finish(
        self, provider: str, offering_id: str, latest_frame_index: int, now: datetime,
    ) -> OfferingDetection:
        if self.last_frame_index != latest_frame_index:
            return OfferingDetection(
                provider,
                offering_id,
                Presence.NO_LONGER_OBSERVED,
                None,
                None if self.last_seen is None else self.last_seen.snapshot_id,
                None if self.last_seen is None else self.last_seen.observed_at,
                None,
                None,
                None,
                None,
                None,
                None,
                False,
                DealStatus.NONE,
            )

        started_at = self.current_start.observed_at
        return OfferingDetection(
            provider,
            offering_id,
            Presence.CURRENT,
            self.baseline_reason,
            self.current_latest.snapshot_id,
            self.current_latest.observed_at,
            self.current_start.snapshot_id,
            started_at,
            None if self.preceding is None else self.preceding.snapshot_id,
            None if self.preceding is None else self.preceding.observed_at,
            self.comparison,
            _token_pricing(self.current_latest),
            _unsupported_present(self.current_latest.conditions),
            _classify_deal(
                comparison=self.comparison,
                continuity=self.continuity,
                started_at=started_at,
                now=now,
            ),
        )
