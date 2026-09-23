"""Focused detection semantics without networking, CLI, or schema changes."""

import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import (
    MAX_EMAX,
    MIN_EMIN,
    ROUND_DOWN,
    Decimal,
    InvalidOperation,
    Overflow,
    localcontext,
)
from pathlib import Path

from model_price_watcher.detection import (
    VISIBILITY_WINDOW,
    AggregateDirection,
    BaselineReason,
    ComponentDirection,
    DealStatus,
    DetectionReport,
    OfferingDetection,
    PercentageStatus,
    Presence,
    SnapshotFrame,
    TokenPricing,
    analyze_history,
    compare_component,
    compare_observations,
    detect_current,
)
from model_price_watcher.models import CatalogObservation, SourceMetadata
from model_price_watcher.storage import (
    ObservationRecord,
    SnapshotRecord,
    get_offering_history,
    get_successful_snapshots,
    open_database,
    write_snapshot,
)


SOURCE = SourceMetadata("fixture:detection", {"capture": "test"})
UTC = timezone.utc
DAY = datetime(2026, 9, 1, 12, tzinfo=UTC)


class AdvertisedIsolationTests(unittest.TestCase):
    def test_legacy_history_rejects_reserved_stream_even_empty(self):
        for provider in ('cheaper_inference', 'cheaper_inference.public.standard', 'cheaper_inference.other'):
            with self.assertRaises(ValueError):
                analyze_history([], provider=provider, now=DAY)

    def test_quote_injection_fails_pair_and_history_for_legacy_provider(self):
        from dataclasses import replace
        item = ObservationRecord(1, 'A', DAY, Decimal(1), Decimal(2), {}, {}, object())
        snapshot = SnapshotRecord(1, 'openrouter', DAY, DAY, SOURCE)
        with self.assertRaises(ValueError):
            analyze_history([SnapshotFrame(snapshot, (item,))], provider='openrouter', now=DAY)
        with self.assertRaises(ValueError):
            compare_observations(item, replace(item, snapshot_id=2))


def _context_snapshot(context):
    return (
        context.prec,
        context.Emax,
        context.Emin,
        context.rounding,
        context.capitals,
        context.clamp,
        tuple(context.traps[signal] for signal in context.traps),
        tuple(context.flags[signal] for signal in context.flags),
    )


def _at(days, hour=12, minute=0):
    return DAY + timedelta(days=days, hours=hour - 12, minutes=minute)


def _observation(
    snapshot_id,
    offering_id,
    observed_at,
    input_per_million,
    output_per_million,
    conditions=None,
    source_record=None,
):
    return ObservationRecord(
        snapshot_id,
        offering_id,
        observed_at,
        input_per_million,
        output_per_million,
        {} if conditions is None else conditions,
        {"id": offering_id} if source_record is None else source_record,
    )


def _snapshot(snapshot_id, observed_at, provider="openrouter"):
    return SnapshotRecord(snapshot_id, provider, observed_at, observed_at, SOURCE)


def _frame(snapshot_id, observed_at, rows, provider="openrouter"):
    snapshot = _snapshot(snapshot_id, observed_at, provider)
    observations = tuple(
        _observation(snapshot_id, offering_id, observed_at, amount_in, amount_out, conditions, source_record)
        for offering_id, amount_in, amount_out, conditions, source_record in (
            row if len(row) == 5 else (*row, None, None)
            for row in rows
        )
    )
    return SnapshotFrame(snapshot, observations)


def _analyze(frames, now=None, provider="openrouter"):
    if now is None:
        now = frames[-1].snapshot.completed_at if frames else DAY
    return analyze_history(frames, provider=provider, now=now)


class ComponentArithmeticTests(unittest.TestCase):
    def test_coefficients_beyond_integer_string_limit(self):
        previous = Decimal("1." + "0" * 5000)
        cases = (
            ("0.95" + "0" * 4998, ComponentDirection.DECREASE, True),
            ("0.95" + "0" * 4997 + "1", ComponentDirection.DECREASE, False),
            ("0.94" + "9" * 4998, ComponentDirection.DECREASE, True),
            ("1.05" + "0" * 4998, ComponentDirection.INCREASE, True),
            ("1.04" + "9" * 4998, ComponentDirection.INCREASE, False),
            ("1.05" + "0" * 4997 + "1", ComponentDirection.INCREASE, True),
        )
        for text, direction, meaningful in cases:
            with self.subTest(direction=direction, meaningful=meaningful, tail=text[-1]):
                current = Decimal(text)
                self.assertGreater(len(current.as_tuple().digits), 4300)
                change = compare_component(previous, current)
                self.assertEqual(change.direction, direction)
                self.assertEqual(change.meaningful, meaningful)

    def test_threshold_boundaries_and_direction(self):
        previous = Decimal("100")
        cases = [
            ("below decrease", Decimal("95.01"), ComponentDirection.DECREASE, False),
            ("exact decrease", Decimal("95"), ComponentDirection.DECREASE, True),
            ("above decrease", Decimal("94"), ComponentDirection.DECREASE, True),
            ("below increase", Decimal("104.99"), ComponentDirection.INCREASE, False),
            ("exact increase", Decimal("105"), ComponentDirection.INCREASE, True),
            ("above increase", Decimal("106"), ComponentDirection.INCREASE, True),
        ]
        for label, current, direction, meaningful in cases:
            with self.subTest(label=label):
                change = compare_component(previous, current)
                self.assertEqual(change.previous, previous)
                self.assertEqual(change.current, current)
                self.assertEqual(change.direction, direction)
                self.assertEqual(change.meaningful, meaningful)
                self.assertIsInstance(change.percentage, Decimal)

    def test_long_mantissa_exact_five_percent(self):
        coefficient = 123456789012345678901234567900
        previous = Decimal(coefficient)
        current = Decimal(coefficient * 95 // 100)
        change = compare_component(previous, current)
        self.assertEqual(change.previous, previous)
        self.assertEqual(change.current, current)
        self.assertEqual(change.direction, ComponentDirection.DECREASE)
        self.assertTrue(change.meaningful)

    def test_zero_unknown_and_signed_zero(self):
        positive = Decimal("10")
        zero = Decimal("0")
        signed_zero = Decimal("-0")
        cases = [
            (None, None, ComponentDirection.UNKNOWN, None, PercentageStatus.UNKNOWN_PRICE),
            (None, positive, ComponentDirection.UNKNOWN, None, PercentageStatus.UNKNOWN_PRICE),
            (positive, None, ComponentDirection.UNKNOWN, None, PercentageStatus.UNKNOWN_PRICE),
            (positive, positive, ComponentDirection.UNCHANGED, False, PercentageStatus.EXACT),
            (Decimal("1.00"), Decimal("1"), ComponentDirection.UNCHANGED, False, PercentageStatus.EXACT),
            (zero, zero, ComponentDirection.UNCHANGED, False, PercentageStatus.ZERO_BASE),
            (signed_zero, zero, ComponentDirection.UNCHANGED, False, PercentageStatus.ZERO_BASE),
            (positive, zero, ComponentDirection.DECREASE, True, PercentageStatus.EXACT),
            (zero, positive, ComponentDirection.INCREASE, True, PercentageStatus.ZERO_BASE),
            (signed_zero, positive, ComponentDirection.INCREASE, True, PercentageStatus.ZERO_BASE),
        ]
        for previous, current, direction, meaningful, status in cases:
            with self.subTest(previous=previous, current=current):
                change = compare_component(previous, current)
                self.assertEqual(change.direction, direction)
                self.assertEqual(change.meaningful, meaningful)
                self.assertEqual(change.percentage_status, status)
                if status is PercentageStatus.EXACT and previous == current and previous not in (None, zero, signed_zero):
                    self.assertEqual(change.percentage, Decimal("0"))
                if previous == positive and current == zero:
                    self.assertEqual(change.percentage, Decimal("100"))
                if status is PercentageStatus.ZERO_BASE:
                    self.assertIsNone(change.percentage)

    def test_percentage_rounding_and_exactness(self):
        exact = compare_component(Decimal("10"), Decimal("5"))
        self.assertEqual(exact.percentage, Decimal("50"))
        self.assertEqual(exact.percentage_status, PercentageStatus.EXACT)
        increase = compare_component(Decimal("10"), Decimal("15"))
        self.assertEqual(increase.percentage, Decimal("-50"))
        repeating = compare_component(Decimal("3"), Decimal("2"))
        self.assertEqual(repeating.percentage_status, PercentageStatus.ROUNDED)
        self.assertIsNotNone(repeating.percentage)

    def test_numeric_range_preserves_threshold(self):
        previous = Decimal((0, (1,), MIN_EMIN))
        current = Decimal((0, (1,), MAX_EMAX))
        change = compare_component(previous, current)
        self.assertEqual(change.direction, ComponentDirection.INCREASE)
        self.assertTrue(change.meaningful)
        self.assertIsNone(change.percentage)
        self.assertEqual(change.percentage_status, PercentageStatus.NUMERIC_RANGE)

    def test_huge_exponent_exact_threshold(self):
        previous = Decimal((0, (1, 0, 0), 500000))
        current = Decimal((0, (9, 5), 500000))
        change = compare_component(previous, current)
        self.assertEqual(change.direction, ComponentDirection.DECREASE)
        self.assertTrue(change.meaningful)
        just_above = Decimal((0, (9, 5, 0, 1), 499998))
        missed = compare_component(previous, just_above)
        self.assertEqual(missed.direction, ComponentDirection.DECREASE)
        self.assertFalse(missed.meaningful)

    def test_decimal_context_independence(self):
        previous = Decimal("100")
        current = Decimal("95")
        with localcontext() as context:
            context.prec = 2
            context.Emax = 8
            context.Emin = -8
            context.rounding = ROUND_DOWN
            context.capitals = 1
            context.clamp = 1
            for signal in context.traps:
                context.traps[signal] = True
            context.clear_flags()
            context.flags[InvalidOperation] = True
            context.flags[Overflow] = True
            before = _context_snapshot(context)
            change = compare_component(previous, current)
            self.assertTrue(change.meaningful)
            self.assertEqual(change.percentage, Decimal("5"))
            self.assertEqual(_context_snapshot(context), before)
            with self.assertRaises(ValueError):
                compare_component(Decimal("-1"), current)
            self.assertEqual(_context_snapshot(context), before)
            with self.assertRaises(TypeError):
                compare_component(1.25, current)
            self.assertEqual(_context_snapshot(context), before)

    def test_invalid_money_rejected(self):
        with self.assertRaises(TypeError):
            compare_component(True, Decimal("1"))
        with self.assertRaises(ValueError):
            compare_component(Decimal("NaN"), Decimal("1"))
        with self.assertRaises(ValueError):
            compare_component(Decimal("1"), Decimal("-0.01"))


class ObservationComparisonTests(unittest.TestCase):
    def record(self, offering="model", inp="10", out="20", conditions=None, snapshot_id=1):
        return _observation(
            snapshot_id, offering, DAY,
            None if inp is None else Decimal(inp),
            None if out is None else Decimal(out),
            conditions,
        )

    def test_mixed_and_partial_and_deal_eligibility(self):
        mixed = compare_observations(
            self.record(),
            self.record(inp="8", out="20.02"),
        )
        self.assertEqual(mixed.aggregate, AggregateDirection.MIXED)
        self.assertTrue(mixed.input.meaningful)
        self.assertFalse(mixed.output.meaningful)
        self.assertEqual(mixed.output.direction, ComponentDirection.INCREASE)
        self.assertFalse(mixed.deal_eligible)

        two_decreases = compare_observations(
            self.record(),
            self.record(inp="8", out="19"),
        )
        self.assertEqual(two_decreases.aggregate, AggregateDirection.DECREASE)
        self.assertTrue(two_decreases.deal_eligible)

        partial = compare_observations(
            self.record(out=None),
            self.record(inp="8", out=None),
        )
        self.assertEqual(partial.aggregate, AggregateDirection.PARTIAL)
        self.assertFalse(partial.deal_eligible)

        unknown = compare_observations(
            self.record(inp=None, out=None),
            self.record(inp=None, out=None),
        )
        self.assertEqual(unknown.aggregate, AggregateDirection.NONCOMPARABLE)

    def test_unsupported_evidence_equality(self):
        left = self.record(conditions={"b": Decimal("1"), "a": True, "list": [Decimal("2"), False]})
        right = self.record(conditions={"a": True, "list": [2, False], "b": 1})
        same = compare_observations(left, right)
        self.assertFalse(same.conditions_changed)
        self.assertTrue(same.unsupported_present)
        self.assertFalse(same.deal_eligible)

        boolean_vs_number = compare_observations(
            self.record(inp="9", conditions={"flag": True}),
            self.record(inp="9", conditions={"flag": 1}),
        )
        self.assertTrue(boolean_vs_number.conditions_changed)

        string_vs_number = compare_observations(
            self.record(conditions={"n": "1"}),
            self.record(conditions={"n": 1}),
        )
        self.assertTrue(string_vs_number.conditions_changed)

        array_order = compare_observations(
            self.record(conditions={"n": [1, 2]}),
            self.record(conditions={"n": [2, 1]}),
        )
        self.assertTrue(array_order.conditions_changed)

        added = compare_observations(
            self.record(inp="9"),
            self.record(inp="9", conditions={"cache": Decimal("0.1")}),
        )
        self.assertTrue(added.conditions_changed)
        self.assertFalse(added.deal_eligible)

        removed = compare_observations(
            self.record(inp="9", conditions={"cache": Decimal("0.1")}),
            self.record(inp="9"),
        )
        self.assertTrue(removed.conditions_changed)

    def test_identity_mismatch(self):
        with self.assertRaises(ValueError):
            compare_observations(self.record("A"), self.record("B"))


class HistoryReductionTests(unittest.TestCase):
    def test_identity_lookup_work_is_bounded_by_observations(self):
        class CountedIdentity(str):
            comparisons = 0
            __hash__ = str.__hash__

            def __eq__(self, other):
                type(self).comparisons += 1
                return super().__eq__(other)

        for frame_count, offering_count in ((12, 64), (24, 128)):
            with self.subTest(frames=frame_count, offerings=offering_count):
                frames = [
                    _frame(index + 1, _at(index), [
                        (CountedIdentity(f"model-{offering:04d}"), Decimal(10), Decimal(20))
                        for offering in range(offering_count)
                    ])
                    for index in range(frame_count)
                ]
                CountedIdentity.comparisons = 0
                report = _analyze(frames)
                # A linear search per offering per snapshot exceeds this bound.
                self.assertLessEqual(
                    CountedIdentity.comparisons, 4 * frame_count * offering_count,
                )
                self.assertEqual(len(report.offerings), offering_count)
                self.assertTrue(all(
                    row.current_state_start_snapshot_id == 1
                    and row.latest_snapshot_id == frame_count
                    for row in report.offerings
                ))

    def test_diagnostics_keep_offering_then_chronological_order(self):
        frames = [
            _frame(index, DAY, [
                (identity, Decimal(10 - index), Decimal(20))
                for identity in ("z", "A")
            ])
            for index in (1, 2, 3)
        ]
        report = _analyze(frames)
        self.assertEqual(
            [(item.offering_id, item.snapshot_ids) for item in report.diagnostics],
            [("A", (1, 2)), ("A", (2, 3)), ("z", (1, 2)), ("z", (2, 3))],
        )

    def offering(
        self,
        snapshot_id,
        observed_at,
        inp,
        out,
        offering_id="example/model",
        conditions=None,
        source_record=None,
    ):
        return _frame(
            snapshot_id,
            observed_at,
            ((offering_id, inp, out, conditions, source_record),),
        )

    def test_first_observation_is_not_a_deal(self):
        frames = [
            self.offering(1, _at(0), Decimal("10"), Decimal("20")),
            self.offering(2, _at(1), Decimal("0"), Decimal("0")),
        ]
        report = _analyze([frames[0]])
        row = report.offerings[0]
        self.assertEqual(row.baseline_reason, BaselineReason.FIRST_OBSERVATION)
        self.assertIsNone(row.comparison)
        self.assertEqual(row.deal_status, DealStatus.NONE)
        self.assertEqual(row.token_pricing, TokenPricing.KNOWN_NONZERO_TOKEN_PRICING)
        self.assertFalse(hasattr(row, "request_is_free"))
        zero = _analyze([frames[1]]).offerings[0]
        self.assertEqual(zero.token_pricing, TokenPricing.ZERO_TOKEN_PRICES)
        self.assertEqual(zero.deal_status, DealStatus.NONE)
        self.assertFalse(hasattr(zero, "request_is_free"))
        self.assertFalse(hasattr(report, "request_is_free"))

    def test_repetition_preserves_start_and_baseline(self):
        start = _at(1)
        frames = [
            self.offering(1, _at(0), Decimal("10"), Decimal("20")),
            self.offering(2, start, Decimal("9"), Decimal("20")),
            self.offering(3, _at(2), Decimal("9"), Decimal("20")),
            self.offering(4, _at(3), Decimal("9.00"), Decimal("20.0")),
        ]
        row = _analyze(frames, now=_at(3)).offerings[0]
        self.assertEqual(row.deal_status, DealStatus.ACTIVE)
        self.assertEqual(row.current_state_started_at, start)
        self.assertEqual(row.current_state_start_snapshot_id, 2)
        self.assertEqual(row.preceding_snapshot_id, 1)
        self.assertEqual(row.latest_snapshot_id, 4)
        self.assertEqual(row.comparison.input.previous, Decimal("10"))
        self.assertEqual(row.comparison.input.current, Decimal("9.00"))

    def test_distinct_later_state_replaces_baseline(self):
        frames = [
            self.offering(1, _at(0), Decimal("10"), Decimal("20")),
            self.offering(2, _at(1), Decimal("9.6"), Decimal("20")),
            self.offering(3, _at(2), Decimal("9.5"), Decimal("20")),
        ]
        row = _analyze(frames).offerings[0]
        self.assertEqual(row.deal_status, DealStatus.NONE)
        self.assertEqual(row.preceding_snapshot_id, 2)
        self.assertEqual(row.comparison.input.previous, Decimal("9.6"))
        self.assertEqual(row.comparison.input.current, Decimal("9.5"))
        self.assertFalse(row.comparison.input.meaningful)

        revert = _analyze(frames + [
            self.offering(4, _at(3), Decimal("10"), Decimal("20")),
        ]).offerings[0]
        self.assertEqual(revert.comparison.aggregate, AggregateDirection.INCREASE)
        self.assertEqual(revert.deal_status, DealStatus.NONE)

    def test_sub_five_percent_reductions_do_not_accumulate(self):
        frames = [
            self.offering(1, _at(0), Decimal("100"), Decimal("100")),
            self.offering(2, _at(1), Decimal("97"), Decimal("100")),
            self.offering(3, _at(2), Decimal("94"), Decimal("100")),
        ]
        row = _analyze(frames).offerings[0]
        self.assertEqual(row.comparison.input.previous, Decimal("97"))
        self.assertFalse(row.comparison.deal_eligible)
        self.assertEqual(row.deal_status, DealStatus.NONE)

    def test_mixed_change_suppresses_deal(self):
        frames = [
            self.offering(1, _at(0), Decimal("10"), Decimal("20")),
            self.offering(2, _at(1), Decimal("8"), Decimal("20.02")),
        ]
        row = _analyze(frames).offerings[0]
        self.assertEqual(row.comparison.aggregate, AggregateDirection.MIXED)
        self.assertEqual(row.deal_status, DealStatus.SUPPRESSED)

    def test_gaps_reset_continuity(self):
        present = Decimal("10")
        cheaper = Decimal("8")
        disappeared = [
            self.offering(1, _at(0), present, present),
            _frame(2, _at(1), ()),
            self.offering(3, _at(2), cheaper, cheaper),
        ]
        returned = _analyze(disappeared).offerings[0]
        self.assertEqual(returned.baseline_reason, BaselineReason.RETURN_AFTER_ABSENCE)
        self.assertEqual(returned.deal_status, DealStatus.NONE)
        self.assertIsNone(returned.comparison)

        unknown = [
            self.offering(1, _at(0), present, present),
            self.offering(2, _at(1), None, present),
            self.offering(3, _at(2), cheaper, present),
        ]
        restored = _analyze(unknown).offerings[0]
        self.assertEqual(restored.baseline_reason, BaselineReason.COMPARABILITY_RESTORED)
        self.assertEqual(restored.deal_status, DealStatus.NONE)
        self.assertFalse(restored.comparison.deal_eligible)

        absent = _analyze(disappeared[:2]).offerings[0]
        self.assertEqual(absent.presence, Presence.NO_LONGER_OBSERVED)
        self.assertEqual(absent.deal_status, DealStatus.NONE)
        self.assertIsNone(absent.token_pricing)
        self.assertIsNone(absent.comparison)

    def test_conditions_change_resets_and_suppresses(self):
        frames = [
            self.offering(1, _at(0), Decimal("10"), Decimal("20")),
            self.offering(2, _at(1), Decimal("8"), Decimal("20"), conditions={"cache": Decimal("1")}),
            self.offering(3, _at(2), Decimal("8"), Decimal("20"), conditions={"cache": Decimal("1")}),
        ]
        row = _analyze(frames).offerings[0]
        self.assertEqual(row.baseline_reason, BaselineReason.CONDITIONS_CHANGED)
        self.assertTrue(row.comparison.conditions_changed)
        self.assertEqual(row.deal_status, DealStatus.SUPPRESSED)
        self.assertEqual(row.current_state_start_snapshot_id, 2)
        self.assertTrue(row.unsupported_pricing_present)

        stable = [
            self.offering(1, _at(0), Decimal("10"), Decimal("20"), conditions={"cache": True}),
            self.offering(2, _at(1), Decimal("8"), Decimal("20"), conditions={"cache": True}),
        ]
        blocked = _analyze(stable).offerings[0]
        self.assertFalse(blocked.comparison.conditions_changed)
        self.assertTrue(blocked.comparison.unsupported_present)
        self.assertEqual(blocked.deal_status, DealStatus.SUPPRESSED)

    def test_metadata_and_raw_record_do_not_reset_state(self):
        frames = [
            SnapshotFrame(
                SnapshotRecord(1, "openrouter", _at(0), _at(0), SourceMetadata("loc-a", {"k": 1})),
                (_observation(
                    1, "example/model", _at(0), Decimal("10"), Decimal("20"),
                    source_record={"id": "example/model", "name": "A"},
                ),),
            ),
            SnapshotFrame(
                SnapshotRecord(2, "openrouter", _at(1), _at(1), SourceMetadata("loc-b", {"k": 2})),
                (_observation(
                    2, "example/model", _at(1), Decimal("8"), Decimal("20"),
                    source_record={"id": "example/model", "name": "B"},
                ),),
            ),
            SnapshotFrame(
                SnapshotRecord(3, "openrouter", _at(2), _at(2), SourceMetadata("loc-c", {"k": 3})),
                (_observation(
                    3, "example/model", _at(2), Decimal("8"), Decimal("20"),
                    source_record={"id": "example/model", "name": "C"},
                ),),
            ),
        ]
        row = _analyze(frames).offerings[0]
        self.assertEqual(row.deal_status, DealStatus.ACTIVE)
        self.assertEqual(row.current_state_start_snapshot_id, 2)
        self.assertEqual(row.preceding_snapshot_id, 1)

    def test_time_order_resets(self):
        same_time = [
            self.offering(1, _at(0), Decimal("10"), Decimal("20")),
            self.offering(2, _at(0), Decimal("8"), Decimal("20")),
        ]
        changed = _analyze(same_time).offerings[0]
        self.assertEqual(changed.baseline_reason, BaselineReason.TIME_ORDER_RESET)
        self.assertEqual(changed.deal_status, DealStatus.SUPPRESSED)
        self.assertEqual(changed.comparison.input.direction, ComponentDirection.DECREASE)
        self.assertIn("CHANGED_STATE_AT_EQUAL_TIME", {item.code for item in _analyze(same_time).diagnostics})

        repeated_same_time = [
            self.offering(1, _at(0), Decimal("8"), Decimal("20")),
            self.offering(2, _at(0), Decimal("8"), Decimal("20")),
        ]
        repeated = _analyze(repeated_same_time).offerings[0]
        self.assertEqual(repeated.baseline_reason, BaselineReason.FIRST_OBSERVATION)
        self.assertEqual(repeated.deal_status, DealStatus.NONE)
        self.assertEqual(repeated.latest_snapshot_id, 2)

        regression = [
            self.offering(1, _at(1), Decimal("10"), Decimal("20")),
            SnapshotFrame(
                SnapshotRecord(2, "openrouter", _at(0), _at(2), SOURCE),
                (_observation(2, "example/model", _at(0), Decimal("8"), Decimal("20")),),
            ),
        ]
        reset = _analyze(regression, now=_at(2)).offerings[0]
        self.assertEqual(reset.baseline_reason, BaselineReason.TIME_ORDER_RESET)
        self.assertEqual(reset.deal_status, DealStatus.NONE)
        self.assertIsNone(reset.comparison)
        self.assertIn("OBSERVATION_TIME_REGRESSION", {item.code for item in _analyze(regression, now=_at(2)).diagnostics})

    def test_visibility_window(self):
        start = _at(0)
        frames = [
            self.offering(1, start, Decimal("10"), Decimal("20")),
            self.offering(2, start + timedelta(hours=1), Decimal("8"), Decimal("20")),
        ]
        state_start = start + timedelta(hours=1)
        just_before = _analyze(frames, now=state_start + VISIBILITY_WINDOW - timedelta(microseconds=1)).offerings[0]
        exact = _analyze(frames, now=state_start + VISIBILITY_WINDOW).offerings[0]
        after = _analyze(frames, now=state_start + VISIBILITY_WINDOW + timedelta(microseconds=1)).offerings[0]
        self.assertEqual(just_before.deal_status, DealStatus.ACTIVE)
        self.assertEqual(exact.deal_status, DealStatus.EXPIRED)
        self.assertEqual(after.deal_status, DealStatus.EXPIRED)
        self.assertEqual(VISIBILITY_WINDOW, timedelta(hours=168))
        offset = timezone(timedelta(hours=3))
        now_offset = (state_start + timedelta(days=1)).astimezone(offset)
        shifted = _analyze(frames, now=now_offset).offerings[0]
        self.assertEqual(shifted.deal_status, DealStatus.ACTIVE)
        with self.assertRaises(ValueError):
            _analyze(frames, now=frames[-1].snapshot.completed_at - timedelta(microseconds=1))
        with self.assertRaises(ValueError):
            _analyze(frames, now=datetime(2026, 9, 3, 12))

    def test_identity_isolation_and_ordering(self):
        frames = [
            SnapshotFrame(
                _snapshot(1, _at(0)),
                (
                    _observation(1, "b-model", _at(0), Decimal("10"), Decimal("10")),
                    _observation(1, "A-model", _at(0), Decimal("10"), Decimal("10")),
                    _observation(1, "a-model", _at(0), Decimal("10"), Decimal("10")),
                    _observation(1, "example/model:free", _at(0), Decimal("10"), Decimal("10")),
                ),
            ),
            SnapshotFrame(
                _snapshot(2, _at(1)),
                (
                    _observation(2, "b-model", _at(1), Decimal("8"), Decimal("10")),
                    _observation(2, "A-model", _at(1), Decimal("10"), Decimal("10")),
                    _observation(2, "a-model", _at(1), Decimal("10"), Decimal("10")),
                    _observation(2, "example/model:free", _at(1), Decimal("10"), Decimal("10")),
                ),
            ),
        ]
        report = _analyze(frames)
        self.assertEqual(
            [row.offering_id for row in report.offerings],
            ["A-model", "a-model", "b-model", "example/model:free"],
        )
        deals = {row.offering_id: row.deal_status for row in report.offerings}
        self.assertEqual(deals["b-model"], DealStatus.ACTIVE)
        self.assertEqual(deals["A-model"], DealStatus.NONE)
        self.assertEqual(deals["example/model:free"], DealStatus.NONE)
        other = [
            self.offering(1, _at(0), Decimal("10"), Decimal("20"), offering_id="shared"),
            SnapshotFrame(
                _snapshot(2, _at(1), provider="Other"),
                (_observation(2, "shared", _at(1), Decimal("8"), Decimal("20")),),
            ),
        ]
        with self.assertRaises(ValueError):
            _analyze(other)
        isolated = _analyze([other[0]])
        self.assertEqual(isolated.offerings[0].deal_status, DealStatus.NONE)

    def test_unknown_provider_and_empty_latest(self):
        empty = _analyze([])
        self.assertIsNone(empty.latest_snapshot_id)
        self.assertEqual(empty.offerings, ())
        frames = [
            self.offering(1, _at(0), Decimal("10"), Decimal("20")),
            _frame(2, _at(1), ()),
        ]
        report = _analyze(frames)
        self.assertEqual(report.latest_snapshot_id, 2)
        self.assertEqual(report.offerings[0].presence, Presence.NO_LONGER_OBSERVED)

    def test_invalid_frames(self):
        frame = self.offering(1, _at(0), Decimal("1"), Decimal("1"))
        with self.assertRaises(ValueError):
            _analyze([frame, frame])
        later_first = [
            self.offering(2, _at(1), Decimal("1"), Decimal("1")),
            self.offering(1, _at(0), Decimal("1"), Decimal("1")),
        ]
        with self.assertRaises(ValueError):
            _analyze(later_first)


class DatabaseWrapperTests(unittest.TestCase):
    def test_large_decimal_end_to_end(self):
        previous = Decimal("1." + "1" * 5000)
        with localcontext() as context:
            context.prec = 6000
            decreased = previous * Decimal("0.95")
            increased = previous * Decimal("1.05")
        self.write(tuple(
            self.observation(identity, DAY, previous, Decimal(20))
            for identity in ("decrease", "increase")
        ), DAY, DAY)
        self.write((
            self.observation("decrease", _at(1), decreased, Decimal(20)),
            self.observation("increase", _at(1), increased, Decimal(20)),
        ), _at(1), _at(1))
        restored = get_offering_history(
            self.connection, provider="openrouter", offering_id="decrease",
        )
        self.assertTrue(all(len(row.input_per_million.as_tuple().digits) > 4300 for row in restored))
        report = detect_current(self.connection, provider="openrouter", now=_at(1))
        decrease, increase = report.offerings
        self.assertTrue(decrease.comparison.input.meaningful)
        self.assertTrue(increase.comparison.input.meaningful)
        self.assertEqual(decrease.comparison.input.direction, ComponentDirection.DECREASE)
        self.assertEqual(increase.comparison.input.direction, ComponentDirection.INCREASE)
        self.assertEqual(decrease.deal_status, DealStatus.ACTIVE)
        self.assertEqual(increase.deal_status, DealStatus.NONE)

    def test_detection_uses_one_history_select_without_writes(self):
        for index in range(16):
            self.write((
                self.observation("keep", _at(index), Decimal(10), Decimal(20)),
            ), _at(index), _at(index))
        statements = []
        before = self.connection.total_changes
        self.connection.set_trace_callback(statements.append)
        try:
            report = detect_current(self.connection, provider="openrouter", now=_at(15))
        finally:
            self.connection.set_trace_callback(None)
        self.assertEqual(sum(sql.lstrip().upper().startswith("SELECT") for sql in statements), 1)
        self.assertEqual(self.connection.total_changes, before)
        self.assertEqual(report.offerings[0].current_state_started_at, DAY)
        self.assertFalse(self.connection.in_transaction)

    def test_failed_analysis_preserves_transaction_ownership(self):
        self.write((), DAY, DAY)
        for caller_owned in (False, True):
            with self.subTest(caller_owned=caller_owned):
                if caller_owned:
                    self.connection.execute("BEGIN")
                with self.assertRaises(ValueError):
                    detect_current(self.connection, provider="openrouter", now=DAY - timedelta(seconds=1))
                self.assertEqual(self.connection.in_transaction, caller_owned)
                if caller_owned:
                    self.connection.rollback()

    def setUp(self):
        self._tempdir = tempfile.TemporaryDirectory()
        self.path = str(Path(self._tempdir.name) / "history.sqlite")
        self.connection = open_database(self.path)
        self.source = SOURCE

    def tearDown(self):
        try:
            self.connection.close()
        except sqlite3.Error:
            pass
        self._tempdir.cleanup()

    def observation(self, offering_id, observed_at, inp, out, **changes):
        values = dict(
            provider="openrouter",
            offering_id=offering_id,
            observed_at=observed_at,
            source=self.source,
            input_usd_per_million=inp,
            output_usd_per_million=out,
            unsupported_pricing={},
            raw_offering={"id": offering_id},
        )
        values.update(changes)
        return CatalogObservation(**values)

    def write(self, rows, started_at, completed_at):
        return write_snapshot(
            self.connection,
            provider="openrouter",
            started_at=started_at,
            completed_at=completed_at,
            source=self.source,
            observations=rows,
        )

    def test_detect_current_uses_successful_snapshots_including_empty(self):
        first = self.write(
            (self.observation("keep", _at(0), Decimal("10"), Decimal("20")),),
            _at(0), _at(0),
        )
        empty = self.write((), _at(1), _at(1))
        returned = self.write(
            (self.observation("keep", _at(2), Decimal("8"), Decimal("20")),),
            _at(2), _at(2),
        )
        snapshots = get_successful_snapshots(self.connection, "openrouter")
        self.assertEqual([row.id for row in snapshots], [first.id, empty.id, returned.id])
        history = get_offering_history(
            self.connection, provider="openrouter", offering_id="keep",
        )
        self.assertEqual([row.snapshot_id for row in history], [first.id, returned.id])
        report = detect_current(self.connection, provider="openrouter", now=_at(2))
        self.assertEqual(report.latest_snapshot_id, returned.id)
        self.assertEqual(report.offerings[0].baseline_reason, BaselineReason.RETURN_AFTER_ABSENCE)
        self.assertEqual(report.offerings[0].deal_status, DealStatus.NONE)
        self.assertFalse(self.connection.in_transaction)
        before = self.connection.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
        detect_current(self.connection, provider="openrouter", now=_at(2))
        after = self.connection.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
        self.assertEqual(before, after)

    def test_owned_and_caller_transactions(self):
        self.write(
            (self.observation("keep", _at(0), Decimal("10"), Decimal("20")),),
            _at(0), _at(0),
        )
        self.assertFalse(self.connection.in_transaction)
        report = detect_current(self.connection, provider="openrouter", now=_at(0))
        self.assertIsInstance(report, DetectionReport)
        self.assertIsInstance(report.offerings[0], OfferingDetection)
        self.assertFalse(self.connection.in_transaction)

        self.connection.execute("BEGIN")
        self.assertTrue(self.connection.in_transaction)
        joined = detect_current(self.connection, provider="openrouter", now=_at(0))
        self.assertEqual(joined.offerings[0].offering_id, "keep")
        self.assertTrue(self.connection.in_transaction)
        self.connection.execute("COMMIT")
        self.assertFalse(self.connection.in_transaction)

    def test_unknown_provider_and_output_immutability(self):
        report = detect_current(self.connection, provider="missing", now=_at(0))
        self.assertIsNone(report.latest_snapshot_id)
        self.assertEqual(report.offerings, ())
        self.assertEqual(report.diagnostics, ())
        self.assertFalse(hasattr(report, "request_is_free"))


if __name__ == "__main__":
    unittest.main()
