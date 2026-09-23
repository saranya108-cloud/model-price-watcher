import json
import sqlite3
import traceback
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch
from model_price_watcher.acquisition import AcquisitionError
from model_price_watcher.transport import HttpResponse, TransportError
from model_price_watcher.storage import open_database, get_successful_history, write_snapshot
from model_price_watcher.providers.cheaper_inference import STREAM_ID

BASE = datetime(2026, 9, 1, tzinfo=timezone.utc)
BODY = b'{"models":[{"id":"A","model_type":"text","input_per_million":"1.25","output_per_million":"10","discount_percent":"0"}]}'
HEADERS = (('Content-Type', 'application/json'),)


class AcquisitionContractTests(unittest.TestCase):
    def setUp(self):
        self.connection = open_database(':memory:')
        self.addCleanup(self.connection.close)

    def scan(self, response=None, day=0, **kwargs):
        from model_price_watcher.cheaper_inference_acquisition import scan_cheaper_inference
        self.transport = Mock()
        self.transport.get.return_value = response if response is not None else HttpResponse(200, BODY, HEADERS)
        self.clock = Mock(side_effect=[BASE+timedelta(days=day, seconds=i) for i in range(3)])
        return scan_cheaper_inference(self.connection, transport=self.transport, clock=self.clock, **kwargs)

    def test_one_keyless_request_and_one_write(self):
        with patch('model_price_watcher.cheaper_inference_acquisition.write_snapshot', wraps=write_snapshot) as writer:
            result = self.scan()
        self.assertEqual(result.observation_count, 1)
        self.assertEqual(writer.call_count, 1)
        self.transport.get.assert_called_once_with('https://api.cheaperinference.com/public/models',
            headers={'Accept': 'application/json', 'Accept-Encoding': 'identity'}, timeout_seconds=30.0, max_response_bytes=16*1024*1024)
        self.assertEqual(self.clock.call_count, 3)
        self.assertFalse(self.connection.in_transaction)

    def test_bad_responses_preserve_history_and_sanitize_errors(self):
        self.scan()
        before = get_successful_history(self.connection, STREAM_ID)
        cases = [HttpResponse(500, b'BODY_SENTINEL', HEADERS),
                 HttpResponse(200, b'\xffBODY_SENTINEL', HEADERS),
                 HttpResponse(200, BODY, ()),
                 HttpResponse(200, BODY, HEADERS+HEADERS),
                 HttpResponse(200, BODY, HEADERS+(('Content-Encoding', 'gzip'),)),
                 HttpResponse(200, BODY, HEADERS+(('Content-Length', '999999'),)),
                 HttpResponse(200, b'{"models":[]}', HEADERS),
                 HttpResponse(200, b'{"models":[{"id":"BODY_SENTINEL"}]}', HEADERS),
                 HttpResponse(200, BODY[:-1], HEADERS)]
        for response in cases:
            with self.subTest(response=response):
                with patch('model_price_watcher.cheaper_inference_acquisition.write_snapshot', wraps=write_snapshot) as writer:
                    with self.assertRaises(AcquisitionError) as caught:
                        self.scan(response, day=1)
                self.assertEqual(writer.call_count, 0)
                self.assertNotIn('BODY_SENTINEL', ''.join(traceback.format_exception(caught.exception)))
                self.assertEqual(get_successful_history(self.connection, STREAM_ID), before)
        with self.assertRaises(AcquisitionError):
            self.scan(max_response_bytes=10)

    def test_refusal_before_clock_or_transport(self):
        from model_price_watcher.cheaper_inference_acquisition import scan_cheaper_inference
        for operation in ('transaction', 'schema', 'foreign_keys'):
            c = open_database(':memory:')
            if operation == 'transaction':
                c.execute('BEGIN')
            elif operation == 'schema':
                c.execute('PRAGMA user_version=1')
            else:
                c.execute('PRAGMA foreign_keys=OFF')
            clock, transport = Mock(), Mock()
            with self.assertRaises((RuntimeError, ValueError)):
                scan_cheaper_inference(c, transport=transport, clock=clock)
            clock.assert_not_called()
            transport.get.assert_not_called()
            c.close()

    def test_ambiguous_zdr_and_conflicting_route(self):
        rows = json.loads(BODY)['models']
        rows[0]['zero_data_retention'] = True
        rows.append(dict(rows[0], id='B', zero_data_retention=False))
        self.scan(HttpResponse(200, json.dumps({'models': rows}).encode(), HEADERS))
        _, stored = get_successful_history(self.connection, STREAM_ID)[0]
        self.assertIsNone(stored[0].advertised_quote)
        self.assertIsNotNone(stored[1].advertised_quote)
        rows[0]['zero_data_retention_route'] = 'rail'
        with self.assertRaises(AcquisitionError):
            self.scan(HttpResponse(200, json.dumps({'models': rows}).encode(), HEADERS), day=1)
        self.assertEqual(len(get_successful_history(self.connection, STREAM_ID)), 1)

    def test_network_clock_and_storage_failures(self):
        from model_price_watcher.cheaper_inference_acquisition import scan_cheaper_inference
        transport = Mock()
        transport.get.side_effect = TransportError('network_error')
        with self.assertRaises(AcquisitionError) as caught:
            scan_cheaper_inference(self.connection, transport=transport, clock=lambda: BASE)
        self.assertIsNone(caught.exception.__context__)
        transport.get.side_effect = None
        transport.get.return_value = HttpResponse(200, BODY, HEADERS)
        for times in ([BASE, BASE-timedelta(seconds=1)], [BASE, BASE, BASE-timedelta(seconds=1)]):
            with self.assertRaises(ValueError):
                scan_cheaper_inference(self.connection, transport=transport, clock=Mock(side_effect=times))
        original = sqlite3.OperationalError('write failure')
        with patch('model_price_watcher.cheaper_inference_acquisition.write_snapshot', side_effect=original):
            with self.assertRaises(sqlite3.OperationalError) as caught:
                self.scan()
        self.assertIs(caught.exception, original)
        self.assertEqual(get_successful_history(self.connection, STREAM_ID), ())

    def test_scan_to_selection_failure_absence_and_return(self):
        from model_price_watcher.advertised_queries import view_selected_advertised_offerings
        from model_price_watcher.advertised_detection import AdvertisedResetReason
        from model_price_watcher.queries import LookupStatus
        def response(price, identities=('A', 'B')):
            model = json.loads(BODY)['models'][0]
            return HttpResponse(200, json.dumps({'models': [dict(model, id=i, input_per_million=price) for i in identities]}).encode(), HEADERS)
        def view():
            return view_selected_advertised_offerings(self.connection, offering_ids=['A', 'B'], now=BASE+timedelta(days=5))
        self.scan(response('1.25'))
        self.scan(response('1'), day=1)
        before = view()
        self.assertEqual(before.recent_observed_advertised_decrease_ids, ('A', 'B'))
        with self.assertRaises(AcquisitionError):
            self.scan(HttpResponse(200, b'{"models":[]}', HEADERS), day=2)
        self.assertEqual(view(), before)
        self.scan(response('1', ('B',)), day=3)
        self.assertEqual(view().lookups[0].status, LookupStatus.NO_LONGER_OBSERVED)
        self.scan(response('0.5'), day=4)
        restored = view().lookups[0]
        self.assertEqual(restored.detection.reset_reason, AdvertisedResetReason.RETURN_AFTER_ABSENCE)
        self.assertIsNone(restored.detection.decrease_event)

    def test_invalid_limits_and_clock_types_never_request(self):
        from model_price_watcher.cheaper_inference_acquisition import scan_cheaper_inference
        for kwargs in ({'timeout_seconds': 0}, {'timeout_seconds': True}, {'max_response_bytes': 0},
                       {'max_response_bytes': True}, {'clock': None}):
            clock, transport = Mock(), Mock()
            options = dict(clock=clock, transport=transport)
            options.update(kwargs)
            with self.assertRaises((TypeError, ValueError)):
                scan_cheaper_inference(self.connection, **options)
            clock.assert_not_called()
            transport.get.assert_not_called()
        for value in (1, BASE.replace(tzinfo=None)):
            transport = Mock()
            with self.assertRaises((TypeError, ValueError)):
                scan_cheaper_inference(self.connection, transport=transport, clock=lambda: value)
            transport.get.assert_not_called()

    def test_structurally_invalid_custom_responses(self):
        cases = [HttpResponse(True, BODY, HEADERS), HttpResponse(200, 'text', HEADERS),
                 HttpResponse(200, BODY, None), HttpResponse(200, BODY, (('Content-Type',),)),
                 HttpResponse(200, BODY, HEADERS+(('Content-Length', '1,1'),)),
                 HttpResponse(200, BODY, HEADERS+(('Content-Length', 'x'),)),
                 HttpResponse(200, BODY, HEADERS+(('Content-Length', str(len(BODY))), ('Transfer-Encoding', 'chunked')))]
        for response in cases:
            with self.assertRaises(AcquisitionError):
                self.scan(response)
        self.assertEqual(get_successful_history(self.connection, STREAM_ID), ())
