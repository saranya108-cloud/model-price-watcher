import json
import sqlite3
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from model_price_watcher.models import SourceMetadata
from model_price_watcher.providers.cheaper_inference import parse_catalog, STREAM_ID, SOURCE_URL, SOURCE_METADATA
from model_price_watcher.storage import open_database, write_snapshot, get_successful_history, StorageError
from model_price_watcher.detection import SnapshotFrame
from model_price_watcher.queries import LookupStatus

BASE = datetime(2026, 9, 1, tzinfo=timezone.utc)
SOURCE = SourceMetadata(SOURCE_URL, SOURCE_METADATA)


class QueryTests(unittest.TestCase):
    def setUp(self):
        self.connection = open_database(':memory:')
        self.addCleanup(self.connection.close)

    def write(self, day, price='1.25', rows=None):
        at = BASE + timedelta(days=day)
        if rows is None:
            rows = [dict(id='A', model_type='text', input_per_million=price, output_per_million='0', discount_percent='0')]
        parsed = parse_catalog(json.dumps({'models': rows}), observed_at=at, source=SOURCE)
        self.assertTrue(parsed.accepted)
        write_snapshot(self.connection, provider=STREAM_ID, started_at=at, completed_at=at, source=SOURCE, observations=parsed.observations)

    def view(self, now=BASE+timedelta(days=3), ids=('A', 'never')):
        from model_price_watcher.advertised_queries import view_selected_advertised_offerings
        return view_selected_advertised_offerings(self.connection, offering_ids=ids, now=now)

    def test_empty_and_projection_equivalence(self):
        from model_price_watcher.advertised_queries import select_advertised_offerings
        empty = self.view(ids=['B', 'A', 'A'])
        self.assertEqual(tuple(x.offering_id for x in empty.lookups), ('A', 'B'))
        self.assertTrue(all(x.status == LookupStatus.NEVER_OBSERVED and x.detection is None and x.current_observation is None for x in empty.lookups))
        self.assertIsNone(empty.latest_snapshot)
        for day, price, rows in [(1, '1.25', None), (2, '1', None), (3, '1', None),
                                 (4, '0', []), (5, '0', None),
                                 (6, '0', [dict(id='A', model_type='text', input_per_million='0', output_per_million='0', discount_percent='0', zero_data_retention=True)])]:
            self.write(day, price, rows)
            now = BASE + timedelta(days=day)
            frames = tuple(SnapshotFrame(s, r) for s, r in get_successful_history(self.connection, STREAM_ID))
            report = self.view(now=now)
            self.assertEqual(report, select_advertised_offerings(frames, offering_ids=('A', 'never'), now=now))
            self.assertEqual(report.lookups[1].status, LookupStatus.NEVER_OBSERVED)
            self.assertEqual(report.lookups[0].status, LookupStatus.NO_LONGER_OBSERVED if day == 4 else LookupStatus.CURRENT)
            self.assertEqual(report.recent_observed_advertised_decrease_ids, ('A',) if day in (2, 3) else ())
            self.assertEqual(report.zero_advertised_base_token_rate_ids, ('A',) if day == 5 else ())
        self.assertIsNone(self.view(now=BASE+timedelta(days=6)).lookups[0].current_observation.advertised_quote)

    def test_seven_day_boundary_does_not_refresh(self):
        self.write(1)
        self.write(2, '1')
        self.write(8, '1')
        boundary = BASE + timedelta(days=9)
        self.assertEqual(self.view(now=boundary-timedelta(microseconds=1)).recent_observed_advertised_decrease_ids, ('A',))
        report = self.view(now=boundary)
        self.assertEqual(report.recent_observed_advertised_decrease_ids, ())
        self.assertEqual(report.lookups[0].detection.decrease_event.observed_at, BASE+timedelta(days=2))

    def test_unselected_corruption_and_transaction_ownership(self):
        self.write(1)
        for caller in (False, True):
            if caller:
                self.connection.execute('BEGIN')
            self.assertEqual(self.view().lookups[0].status, LookupStatus.CURRENT)
            self.assertEqual(self.connection.in_transaction, caller)
            if caller:
                self.connection.rollback()
        self.connection.execute('UPDATE observations SET advertised_quote_json=NULL')
        for caller in (False, True):
            for ids in ([], ['unselected']):
                if caller:
                    self.connection.execute('BEGIN')
                with self.assertRaises(StorageError):
                    self.view(ids=ids)
                self.assertEqual(self.connection.in_transaction, caller)
                if caller:
                    self.connection.rollback()

    def test_invalid_selection_and_clock_before_begin(self):
        seen = []
        self.connection.set_trace_callback(seen.append)
        for ids, now, error in [('A', BASE, TypeError), ([True], BASE, TypeError), (['\0'], BASE, ValueError), ([], BASE.replace(tzinfo=None), ValueError)]:
            with self.assertRaises(error):
                self.view(now=now, ids=ids)
        self.assertEqual(seen, [])

    def test_pure_validation_order_and_unselected_corruption(self):
        from model_price_watcher.advertised_queries import select_advertised_offerings
        with self.assertRaises(TypeError):
            select_advertised_offerings([], offering_ids='invalid', now=BASE.replace(tzinfo=None))
        self.write(1)
        frames = tuple(SnapshotFrame(s, r) for s, r in get_successful_history(self.connection, STREAM_ID))
        bad = replace(frames[0], snapshot=replace(frames[0].snapshot, provider='wrong'))
        with self.assertRaises(ValueError):
            select_advertised_offerings([bad], offering_ids='invalid', now=BASE)
        for ids in ([], ['never']):
            with self.assertRaises(ValueError):
                select_advertised_offerings([bad], offering_ids=ids, now=BASE+timedelta(days=2))

    def test_all_result_datetimes_are_utc(self):
        from model_price_watcher.advertised_queries import select_advertised_offerings
        self.write(1)
        self.write(2, '1')
        zone = timezone(timedelta(hours=5))
        frames = tuple(SnapshotFrame(replace(s, started_at=s.started_at.astimezone(zone), completed_at=s.completed_at.astimezone(zone)),
                                     tuple(replace(r, observed_at=r.observed_at.astimezone(zone)) for r in rows))
                       for s, rows in get_successful_history(self.connection, STREAM_ID))
        report = select_advertised_offerings(frames, offering_ids=['A'], now=(BASE+timedelta(days=3)).astimezone(zone))
        item = report.lookups[0]
        for at in (report.evaluated_at, report.latest_snapshot.started_at, report.latest_snapshot.completed_at,
                   item.current_observation.observed_at, item.detection.latest_observed_at, item.detection.decrease_event.observed_at):
            self.assertIs(at.tzinfo, timezone.utc)

    def test_owned_failures_preserve_original_and_rollback_context(self):
        from model_price_watcher.advertised_queries import view_selected_advertised_offerings
        class Connection(sqlite3.Connection):
            fail = None
            rollback_error = None
            rolled_back = 0
            def execute(self, sql, *args):
                if sql == self.fail:
                    raise original
                return super().execute(sql, *args)
            def rollback(self):
                self.rolled_back += 1
                if self.rollback_error:
                    raise self.rollback_error
                return super().rollback()
        for fail in ('BEGIN', 'COMMIT', 'read'):
            for rollback_failure in (False, True):
                original = sqlite3.OperationalError('original')
                c = sqlite3.connect(':memory:', factory=Connection, isolation_level=None)
                c.fail = fail
                if rollback_failure:
                    c.rollback_error = sqlite3.OperationalError('rollback')
                with patch('model_price_watcher.advertised_queries.get_successful_history', side_effect=original if fail == 'read' else None, return_value=()):
                    with self.assertRaises(sqlite3.OperationalError) as caught:
                        view_selected_advertised_offerings(c, offering_ids=[], now=BASE)
                self.assertIs(caught.exception, original)
                self.assertEqual(c.rolled_back, 0 if fail == 'BEGIN' else 1)
                if rollback_failure and fail != 'BEGIN':
                    self.assertIs(original.__context__, c.rollback_error)
                c.close()
