import json
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from model_price_watcher.models import SourceMetadata
from model_price_watcher.providers.cheaper_inference import parse_catalog, STREAM_ID, SOURCE_URL, SOURCE_METADATA
from model_price_watcher.storage import SnapshotRecord, ObservationRecord
from model_price_watcher.detection import SnapshotFrame, ComponentDirection, AggregateDirection, PercentageStatus, Presence

BASE = datetime(2026, 9, 1, tzinfo=timezone.utc)
SOURCE = SourceMetadata(SOURCE_URL, SOURCE_METADATA)

def frame(n, price='1.25', output='10', empty=False, **changes):
    at = BASE + timedelta(days=n)
    row = dict(id='A', model_type='text', input_per_million=price, output_per_million=output, discount_percent='0')
    row.update(changes)
    parsed = parse_catalog(json.dumps({'models': [] if empty else [row]}), observed_at=at, source=SOURCE)
    assert parsed.accepted
    records = tuple(ObservationRecord(n, r.offering_id, r.observed_at, None, None,
                                     r.unsupported_pricing, r.raw_offering, r.advertised_quote) for r in parsed.observations)
    return SnapshotFrame(SnapshotRecord(n, STREAM_ID, at, at, SOURCE), records)


class AdvertisedDetectionTests(unittest.TestCase):
    def analyze(self, frames):
        from model_price_watcher.advertised_detection import analyze_advertised_history
        return analyze_advertised_history(frames, now=BASE + timedelta(days=20))

    def test_complete_partial_result(self):
        from model_price_watcher.advertised_detection import (AdvertisedComparability as C, AdvertisedResetReason as R,
            AdvertisedComponentChange, AdvertisedComparison, AdvertisedOfferingDetection, AdvertisedDiagnostic, AdvertisedDetectionReport)
        a, b = frame(1, output='bad'), frame(2, '1', 'bad')
        basis = a.observations[0].advertised_quote.basis
        comparison = AdvertisedComparison(C.COMPARABLE, 1, 2, basis, basis,
            AdvertisedComponentChange(Decimal('1.25'), Decimal('1'), ComponentDirection.DECREASE, Decimal(20), PercentageStatus.EXACT),
            AdvertisedComponentChange(None, None, ComponentDirection.UNKNOWN, None, PercentageStatus.UNKNOWN_PRICE), AggregateDirection.PARTIAL)
        expected = AdvertisedDetectionReport(STREAM_ID, BASE + timedelta(days=20), 2, b.snapshot.completed_at,
            (AdvertisedOfferingDetection('A', Presence.CURRENT, 2, b.snapshot.completed_at, b.observations[0].advertised_quote, comparison, R.UNKNOWN_COMPONENT, None),),
            (AdvertisedDiagnostic(R.FIRST_OBSERVATION, 'A', (1,)), AdvertisedDiagnostic(R.UNKNOWN_COMPONENT, 'A', (1, 2))))
        self.assertEqual(self.analyze([a, b]), expected)

    def test_transition_precedence_and_events(self):
        from model_price_watcher.advertised_detection import AdvertisedResetReason as R, AdvertisedComparability as C
        cases = [
            ([frame(1)], R.FIRST_OBSERVATION, None, None),
            ([frame(1, zero_data_retention=True)], R.FIRST_OBSERVATION, None, None),
            ([frame(1, output='bad')], R.FIRST_OBSERVATION, None, None),
            ([frame(1), frame(2, empty=True), frame(3, empty=True)], R.ABSENCE, None, None),
            ([frame(1), frame(2, empty=True), frame(3, '0', zero_data_retention=True)], R.RETURN_AFTER_ABSENCE, None, None),
            ([frame(1), frame(2, '1', zero_data_retention=True)], R.QUOTE_UNSUPPORTED, C.QUOTE_UNSUPPORTED, AggregateDirection.NONCOMPARABLE),
            ([frame(1), frame(2, '1', provider_name='changed')], R.BASIS_CHANGED, C.BASIS_CHANGED, AggregateDirection.NONCOMPARABLE),
            ([frame(1, 'bad', 'bad'), frame(2, 'bad', 'bad')], R.UNKNOWN_COMPONENT, C.COMPARABLE, AggregateDirection.NONCOMPARABLE),
            ([frame(1), frame(2, '1', '11')], None, C.COMPARABLE, AggregateDirection.MIXED),
            ([frame(1), frame(2, '2')], None, C.COMPARABLE, AggregateDirection.INCREASE),
        ]
        for frames, reset, reason, aggregate in cases:
            with self.subTest(reset=reset, reason=reason):
                report = self.analyze(frames)
                item, = report.offerings
                self.assertEqual(item.reset_reason, reset)
                self.assertIsNone(item.decrease_event)
                if reason is None:
                    self.assertIsNone(item.comparison)
                else:
                    self.assertEqual(item.comparison.reason, reason)
                    self.assertEqual(item.comparison.aggregate, aggregate)
                    if reason != C.COMPARABLE:
                        self.assertIsNone(item.comparison.input)
                        self.assertIsNone(item.comparison.output)
                if reset == R.ABSENCE:
                    self.assertEqual(item.presence, Presence.NO_LONGER_OBSERVED)
                    self.assertEqual((item.latest_snapshot_id, item.latest_observed_at, item.current_quote), (None, None, None))
                    self.assertEqual(len(report.diagnostics), 2)
        a, b, c = frame(1), frame(2, '1.24'), frame(3, '1.24', discount_percent='99')
        event = self.analyze([a, b]).offerings[0].decrease_event
        self.assertIsNotNone(event)
        self.assertEqual(self.analyze([a, b, c]).offerings[0].decrease_event, event)
        self.assertEqual(event.observed_at, b.observations[0].observed_at)
        self.assertEqual(self.analyze([a, b, frame(3, '1')]).offerings[0].decrease_event.current_snapshot_id, 3)

    def test_time_order_reset_and_validation(self):
        from model_price_watcher.advertised_detection import compare_advertised_observations, AdvertisedComparability as C
        a, b = frame(1), frame(2, '1')
        overlap = replace(b, snapshot=replace(b.snapshot, started_at=BASE))
        result = compare_advertised_observations(a, overlap, offering_id='A')
        self.assertEqual(result.reason, C.TIME_ORDER_RESET)
        self.assertIsNone(result.input)
        self.assertEqual(compare_advertised_observations(a, replace(b, snapshot=replace(b.snapshot, started_at=a.snapshot.completed_at)), offering_id='A').reason, C.COMPARABLE)
        for frames in ([a, a], [b, a], [replace(a, snapshot=replace(a.snapshot, provider='openrouter'))],
                       [replace(a, observations=(replace(a.observations[0], advertised_quote=None),))]):
            with self.assertRaises(ValueError):
                self.analyze(frames)
        for frames in ('bad', [True], [replace(a, snapshot=replace(a.snapshot, id=True))]):
            with self.assertRaises(TypeError):
                self.analyze(frames)
        with self.assertRaises(ValueError):
            compare_advertised_observations(a, frame(2, empty=True), offering_id='A')

    def test_utc_and_unknown_restoration(self):
        from model_price_watcher.advertised_detection import analyze_advertised_history, AdvertisedResetReason as R
        frames = [frame(1), frame(2, 'bad'), frame(3, '1')]
        item = self.analyze(frames).offerings[0]
        self.assertEqual(item.reset_reason, R.UNKNOWN_COMPONENT)
        self.assertIsNone(item.decrease_event)
        result = analyze_advertised_history([], now=BASE.astimezone(timezone(timedelta(hours=3))))
        self.assertIs(result.evaluated_at.tzinfo, timezone.utc)
        self.assertEqual((result.latest_snapshot_id, result.latest_completed_at, result.offerings, result.diagnostics), (None, None, (), ()))

    def test_complete_noncomparable_and_baseline_results(self):
        from model_price_watcher.advertised_detection import (AdvertisedComparability as C, AdvertisedResetReason as R,
            AdvertisedComparison, AdvertisedOfferingDetection, AdvertisedDiagnostic, AdvertisedDetectionReport,
            compare_advertised_observations)
        a = frame(1)
        choices = [(frame(2, '1', zero_data_retention=True), C.QUOTE_UNSUPPORTED, R.QUOTE_UNSUPPORTED),
                   (frame(2, '1', provider_name='changed'), C.BASIS_CHANGED, R.BASIS_CHANGED),
                   (replace(frame(2, '1'), snapshot=replace(frame(2).snapshot, started_at=BASE)), C.TIME_ORDER_RESET, R.TIME_ORDER_RESET)]
        for b, reason, reset in choices:
            with self.subTest(reason=reason):
                qa, qb = a.observations[0].advertised_quote, b.observations[0].advertised_quote
                comparison = AdvertisedComparison(reason, 1, 2, qa.basis, None if qb is None else qb.basis, None, None, AggregateDirection.NONCOMPARABLE)
                expected = AdvertisedDetectionReport(STREAM_ID, BASE+timedelta(days=20), 2, b.snapshot.completed_at,
                    (AdvertisedOfferingDetection('A', Presence.CURRENT, 2, b.observations[0].observed_at, qb, comparison, reset, None),),
                    (AdvertisedDiagnostic(R.FIRST_OBSERVATION, 'A', (1,)), AdvertisedDiagnostic(reset, 'A', (1, 2))))
                self.assertEqual(compare_advertised_observations(a, b, offering_id='A'), comparison)
                self.assertEqual(self.analyze([a, b]), expected)
        for a in (frame(1), frame(1, 'bad'), frame(1, zero_data_retention=True)):
            expected = AdvertisedDetectionReport(STREAM_ID, BASE+timedelta(days=20), 1, a.snapshot.completed_at,
                (AdvertisedOfferingDetection('A', Presence.CURRENT, 1, a.observations[0].observed_at, a.observations[0].advertised_quote, None, R.FIRST_OBSERVATION, None),),
                (AdvertisedDiagnostic(R.FIRST_OBSERVATION, 'A', (1,)),))
            self.assertEqual(self.analyze([a]), expected)
            absent = replace(expected, latest_snapshot_id=3, latest_completed_at=frame(3).snapshot.completed_at,
                offerings=(AdvertisedOfferingDetection('A', Presence.NO_LONGER_OBSERVED, None, None, None, None, R.ABSENCE, None),),
                diagnostics=expected.diagnostics+(AdvertisedDiagnostic(R.ABSENCE, 'A', (2,)),))
            self.assertEqual(self.analyze([a, frame(2, empty=True), frame(3, empty=True)]), absent)
            returning = frame(4, '0', zero_data_retention=True)
            restored = replace(absent, latest_snapshot_id=4, latest_completed_at=returning.snapshot.completed_at,
                offerings=(AdvertisedOfferingDetection('A', Presence.CURRENT, 4, returning.observations[0].observed_at, None, None, R.RETURN_AFTER_ABSENCE, None),),
                diagnostics=absent.diagnostics+(AdvertisedDiagnostic(R.RETURN_AFTER_ABSENCE, 'A', (4,)),))
            self.assertEqual(self.analyze([a, frame(2, empty=True), frame(3, empty=True), returning]), restored)

    def test_complete_numeric_results_and_event_retention(self):
        from model_price_watcher.advertised_detection import (AdvertisedComparability as C, AdvertisedResetReason as R,
            AdvertisedComponentChange as Change, AdvertisedComparison, AdvertisedDecreaseEvent,
            AdvertisedOfferingDetection, AdvertisedDiagnostic, AdvertisedDetectionReport)
        a = frame(1)
        cases = [('1', '10', AggregateDirection.DECREASE, ComponentDirection.DECREASE, Decimal(20), ComponentDirection.UNCHANGED, Decimal(0)),
                 ('2.5', '10', AggregateDirection.INCREASE, ComponentDirection.INCREASE, Decimal(-100), ComponentDirection.UNCHANGED, Decimal(0)),
                 ('1', '20', AggregateDirection.MIXED, ComponentDirection.DECREASE, Decimal(20), ComponentDirection.INCREASE, Decimal(-100)),
                 ('1.25', '10', AggregateDirection.UNCHANGED, ComponentDirection.UNCHANGED, Decimal(0), ComponentDirection.UNCHANGED, Decimal(0))]
        for inp, out, aggregate, di, pi, do, po in cases:
            b = frame(2, inp, out)
            qa, qb = a.observations[0].advertised_quote, b.observations[0].advertised_quote
            ci = Change(Decimal('1.25'), Decimal(inp), di, pi, PercentageStatus.EXACT)
            co = Change(Decimal('10'), Decimal(out), do, po, PercentageStatus.EXACT)
            comparison = AdvertisedComparison(C.COMPARABLE, 1, 2, qa.basis, qb.basis, ci, co, aggregate)
            event = AdvertisedDecreaseEvent(1, 2, b.snapshot.completed_at, qa, qb, ci, co) if aggregate == AggregateDirection.DECREASE else None
            expected = AdvertisedDetectionReport(STREAM_ID, BASE+timedelta(days=20), 2, b.snapshot.completed_at,
                (AdvertisedOfferingDetection('A', Presence.CURRENT, 2, b.snapshot.completed_at, qb, comparison, None, event),),
                (AdvertisedDiagnostic(R.FIRST_OBSERVATION, 'A', (1,)),))
            self.assertEqual(self.analyze([a, b]), expected)
        # 34-digit repeating percentage and a zero baseline retain their statuses.
        for first, second, status in [('3', '2', PercentageStatus.ROUNDED), ('0', '1', PercentageStatus.ZERO_BASE)]:
            item = self.analyze([frame(1, first), frame(2, second)]).offerings[0]
            self.assertEqual(item.comparison.input.percentage_status, status)

    def test_unknowns_clear_event_and_diagnostics_order(self):
        from model_price_watcher.advertised_detection import AdvertisedResetReason as R
        item = self.analyze([frame(1), frame(2, '1'), frame(3, 'bad'), frame(4, '1')]).offerings[0]
        self.assertIsNone(item.decrease_event)
        self.assertEqual(item.reset_reason, R.UNKNOWN_COMPONENT)
        a = frame(1)
        other = frame(1, id='B')
        combined = replace(a, observations=other.observations+a.observations)
        report = self.analyze([combined, frame(2, empty=True)])
        self.assertEqual([(d.code, d.offering_id, d.snapshot_ids) for d in report.diagnostics],
                         [(R.FIRST_OBSERVATION, 'A', (1,)), (R.FIRST_OBSERVATION, 'B', (1,)),
                          (R.ABSENCE, 'A', (2,)), (R.ABSENCE, 'B', (2,))])

    def test_equal_observation_time_resets_and_hostile_decimal_context_is_unchanged(self):
        from decimal import localcontext
        from model_price_watcher.advertised_detection import compare_advertised_observations, AdvertisedComparability as C
        a = frame(1)
        b = replace(a, snapshot=replace(a.snapshot, id=2), observations=(replace(a.observations[0], snapshot_id=2),))
        self.assertEqual(compare_advertised_observations(a, b, offering_id='A').reason, C.TIME_ORDER_RESET)
        with localcontext() as ctx:
            ctx.prec, ctx.Emax, ctx.Emin = 2, 9, -9
            for signal in ctx.traps:
                ctx.traps[signal] = True
            before = ctx.copy()
            a, b = frame(1, '1' * 4301), frame(2, '1')
            comparison = compare_advertised_observations(a, b, offering_id='A')
            self.assertEqual(comparison.input.direction, ComponentDirection.DECREASE)
            self.assertEqual((ctx.prec, ctx.Emax, ctx.Emin, ctx.flags, ctx.traps),
                             (before.prec, before.Emax, before.Emin, before.flags, before.traps))
