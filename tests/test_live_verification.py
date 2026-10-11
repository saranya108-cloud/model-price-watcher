"""Offline G1 evidence harness contracts; no real provider requests."""
import importlib.util
from contextlib import closing
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from dataclasses import replace
from decimal import Decimal
from unittest.mock import patch

RUNNER = Path(__file__).resolve().parents[1] / 'tools/verify_cheaper_inference_live.py'
BASE = datetime(2026, 10, 10, tzinfo=timezone.utc)


def body(rows=None):
    base = dict(model_type='text', input_per_million='1.2500',
                output_per_million='10', discount_percent='0',
                endpoint='/v1/chat/completions', supported_endpoints=[])
    rows = rows or [dict(id='z'), dict(id='a'), dict(id='partial', output_per_million='unknown'),
                    dict(id='unknown', input_per_million='?', output_per_million='?'),
                    dict(id='evidence', new_field=True)]
    return json.dumps({'models': [dict(base, **r) for r in rows]}).encode()


class FakeResponse:
    def __init__(self, data, headers, status=200):
        self.data, self.headers, self.status = data, headers, status
        self.offset = self.reads = 0

    def getheaders(self):
        return self.headers

    def read(self, count):
        self.reads += 1
        result = self.data[self.offset:self.offset + count]
        self.offset += len(result)
        return result

    def close(self):
        pass


def fake_connection(response, requests, failure=None):
    class Connection:
        def __init__(self, host, port, *, timeout):
            self.host, self.port, self.timeout = host, port, timeout

        def request(self, method, target, *, headers):
            requests.append((self.host, self.port, self.timeout, method, target, headers))
            if failure:
                raise failure

        def getresponse(self):
            return response

        def close(self):
            pass
    return Connection


def load_runner():
    spec = importlib.util.spec_from_file_location('g1_runner', RUNNER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class SourceTests(unittest.TestCase):
    def test_source_description_is_read_only_and_complete(self):
        self.assertTrue(RUNNER.is_file(), 'G1 runner is not implemented')
        runner = load_runner()
        out = io.StringIO()
        with patch('socket.socket', side_effect=AssertionError('network')), \
             patch('subprocess.Popen', side_effect=AssertionError('Git/process')):
            self.assertEqual(runner.main(['--describe-sources'], stdout=out), 0)
        inventory = json.loads(out.getvalue())['sources']
        names = {row['module'] for row in inventory}
        self.assertIn(None, names)
        self.assertEqual(names - {None}, {name for name in sys.modules
                         if name == 'model_price_watcher' or name.startswith('model_price_watcher.')})
        self.assertEqual(next(r for r in inventory if r['module'] is None)['path'], str(RUNNER))

    def test_origin_missing_source_and_drift(self):
        runner = load_runner()
        before = runner.source_inventory()
        module = sys.modules['model_price_watcher.models']
        with patch.object(module, '__file__', '/tmp/wrong.py'):
            with self.assertRaises(runner.Refusal):
                runner.source_inventory()
        with patch.object(module, '__file__', str(RUNNER.parent / 'missing.py')):
            with self.assertRaises(runner.Refusal):
                runner.source_inventory()
        changed = [dict(r) for r in before]
        changed[0]['sha256'] = '0' * 64
        self.assertFalse(runner.compare_sources(before, changed)['clean'])
        with patch.dict(sys.modules, {'model_price_watcher.late': module}):
            after = runner.source_inventory()
        self.assertEqual(runner.compare_sources(before, after)['added'], ['model_price_watcher.late'])
        moved = [dict(r) for r in before]
        moved[0]['path'] += '.other'
        self.assertEqual(runner.compare_sources(before, moved)['changed_origins'], [None])

    def test_real_description_without_env_creates_no_cache_or_git_dependency(self):
        # Run the actual file in an isolated disposable checkout with no .git.
        # A blocker imports before the runner and denies network/process creation.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'tools').mkdir()
            (root / 'src/model_price_watcher/providers').mkdir(parents=True)
            (root / 'tools/verify_cheaper_inference_live.py').write_bytes(RUNNER.read_bytes())
            for source in (RUNNER.parents[1] / 'src/model_price_watcher').rglob('*.py'):
                target = root / 'src/model_price_watcher' / source.relative_to(RUNNER.parents[1] / 'src/model_price_watcher')
                target.write_bytes(source.read_bytes())
            env = dict(os.environ, PYTHONPATH=str(root / 'src'))
            env.pop('PYTHONDONTWRITEBYTECODE', None)
            command = ('import runpy,sys,socket,subprocess,http.client; '
                       'socket.socket.connect=lambda *a,**k: (_ for _ in ()).throw(AssertionError("network")); '
                       'subprocess.Popen=lambda *a,**k: (_ for _ in ()).throw(AssertionError("process")); '
                       'sys.argv=[sys.argv[1],"--describe-sources"]; runpy.run_path(sys.argv[0],run_name="__main__")')
            before = {str(p.relative_to(root)): p.read_bytes() for p in root.rglob('*') if p.is_file()}
            directories = {str(p.relative_to(root)) for p in root.rglob('*') if p.is_dir()}
            result = subprocess.run([sys.executable, '-c', command, str(root / 'tools/verify_cheaper_inference_live.py')],
                                    env=env, cwd=root, capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('model_price_watcher.providers.openrouter', {r['module'] for r in json.loads(result.stdout)['sources']})
            after = {str(p.relative_to(root)): p.read_bytes() for p in root.rglob('*') if p.is_file()}
            self.assertEqual(before, after)
            self.assertEqual(directories, {str(p.relative_to(root)) for p in root.rglob('*') if p.is_dir()})
            self.assertFalse(list(root.rglob('__pycache__')))
            self.assertFalse(list(root.rglob('*.sqlite')))


class RunTests(unittest.TestCase):
    def setUp(self):
        self.runner = load_runner()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.run_dir = Path(self.temp.name) / 'new ?#% é'
        self.requests = []

    def execute(self, data=None, headers=None, status=200, failure=None, **kwargs):
        self.response = FakeResponse(data if data is not None else body(),
                                     headers or [('Content-Type', 'application/json')], status)
        clock = iter(BASE + timedelta(seconds=i) for i in range(20))
        return self.runner.execute_run(self.run_dir,
            connection_factory=fake_connection(self.response, self.requests, failure),
            clock=lambda: next(clock), stdout=io.StringIO(), stderr=io.StringIO(), **kwargs)

    def artifact(self, name):
        return json.loads((self.run_dir / name).read_text())

    def test_success_roundtrip_categories_samples_and_fixed_request(self):
        run = self.execute()
        self.assertEqual(run['exit_code'], 0)
        self.assertEqual(run['persistence'], 'established')
        self.assertEqual(run['request_count'], 1)
        self.assertEqual(self.requests, [('api.cheaperinference.com', None, 30.0, 'GET', '/public/models',
                         {'Accept': 'application/json', 'Accept-Encoding': 'identity'})])
        self.assertEqual(self.response.offset, len(body()))
        self.assertEqual(self.response.reads, 2)
        self.assertEqual(run['http']['body_bytes'], len(body()))
        self.assertEqual(run['http']['body_sha256'], hashlib.sha256(body()).hexdigest())
        summary = self.artifact('catalog-summary.json')
        self.assertEqual(summary['categories'], dict(complete=0, partial=0, unknown=0, evidence_only=5))
        self.assertEqual(summary['accepted_observations'], 5)
        selected = self.artifact('selected-results.json')
        self.assertEqual(selected['sample_ids'], ['a'])
        self.assertEqual(selected['report']['lookups'][0]['status'], 'NEVER_OBSERVED')
        self.assertTrue(self.artifact('verification.json')['round_trip'])
        self.assertEqual(set(p.name for p in self.run_dir.iterdir()),
                         {'run.json', 'catalog.sqlite', 'catalog-summary.json',
                          'selected-results.json', 'verification.json', 'manifest.sha256'})
        with closing(self.runner.safe_readback(self.run_dir / 'catalog.sqlite')) as c:
            self.assertEqual(c.execute('SELECT count(*) FROM snapshots').fetchone()[0], 1)
            self.assertEqual(c.execute('SELECT count(*) FROM observations').fetchone()[0], 5)

    def test_preflight_path_refusals_never_request(self):
        for path in (Path('relative'), RUNNER.parent / 'forbidden', Path(self.temp.name)):
            with self.subTest(path=path), self.assertRaises(self.runner.Refusal):
                self.runner.execute_run(path, connection_factory=fake_connection(None, self.requests))
        self.assertEqual(self.requests, [])

    def test_bad_source_preflight_never_creates_run(self):
        with patch.object(self.runner, 'source_inventory', side_effect=self.runner.Refusal('source')):
            with self.assertRaises(self.runner.Refusal):
                self.execute()
        self.assertFalse(self.run_dir.exists())
        self.assertEqual(self.requests, [])

    def test_network_failure_body_unavailable_and_no_snapshot(self):
        run = self.execute(failure=OSError('synthetic'))
        self.assertEqual(run['exit_code'], 4)
        self.assertEqual(run['persistence'], 'absent')
        self.assertIsNone(run['http']['body_bytes'])
        self.assertIsNone(run['http']['body_sha256'])
        self.assertEqual(len(self.requests), 1)
        with closing(self.runner.safe_readback(self.run_dir / 'catalog.sqlite')) as c:
            self.assertEqual(c.execute('SELECT count(*) FROM snapshots').fetchone()[0], 0)

    def test_catalog_rejected_body_retained_only_at_parser_boundary(self):
        rejected = b'{"models":[{"id":"bad"}]}'
        run = self.execute(data=rejected)
        self.assertEqual(run['failure'], 'catalog_rejected')
        self.assertEqual((self.run_dir / 'rejected-body.bin').read_bytes(), rejected)
        self.assertEqual(self.artifact('catalog-summary.json')['accepted_observations'], 0)
        self.assertIsNone(self.artifact('verification.json')['findings']['C_conditions_observed'])

    def test_failed_rejected_body_recording_marks_package_incomplete(self):
        original = Path.write_bytes
        def failure(path, data):
            if path.name == 'rejected-body.bin':
                raise OSError('synthetic disk failure')
            return original(path, data)
        with patch.object(Path, 'write_bytes', failure):
            run = self.execute(data=b'{"models":[{"id":"bad"}]}')
        self.assertFalse(run['evidence_complete'])
        self.assertEqual(run['scanner_failure'], 'catalog_rejected')
        self.assertEqual(run['persistence'], 'absent')

    def test_redirect_is_not_followed_and_error_body_not_retained(self):
        run = self.execute(status=302)
        self.assertEqual(run['exit_code'], 3)
        self.assertEqual(len(self.requests), 1)
        self.assertFalse((self.run_dir / 'rejected-body.bin').exists())

    def test_duplicate_headers_order_and_transfer_visible_without_changed_semantics(self):
        headers = [('Content-Type', 'application/json'), ('Transfer-Encoding', 'chunked'),
                   ('Content-Length', '10'), ('Content-Length', '10')]
        run = self.execute(headers=headers)
        self.assertEqual(run['http']['headers'], [list(h) for h in headers])
        self.assertEqual(run['failure'], 'invalid_framing')
        self.assertIsNone(run['http']['body_bytes'])
        self.assertEqual(self.response.reads, 0)

    def test_prewrite_generic_failure_is_uncertain(self):
        with patch('model_price_watcher.cheaper_inference_acquisition.write_snapshot',
                   side_effect=ValueError('not evidence of rollback')):
            run = self.execute()
        self.assertEqual(run['persistence'], 'not_established')
        self.assertEqual(run['exit_code'], 7)

    def test_postcommit_failure_and_interruption_are_uncertain(self):
        from model_price_watcher.storage import write_snapshot
        for error in (RuntimeError('after COMMIT'), KeyboardInterrupt()):
            with tempfile.TemporaryDirectory() as temporary:
                self.run_dir = Path(temporary) / 'run'
                def after_commit(*args, **kwargs):
                    write_snapshot(*args, **kwargs)
                    raise error
                with patch('model_price_watcher.cheaper_inference_acquisition.write_snapshot', after_commit):
                    run = self.execute()
                self.assertEqual(run['persistence'], 'not_established')
                self.assertEqual(run['exit_code'], 130 if isinstance(error, KeyboardInterrupt) else 7)
                if isinstance(error, KeyboardInterrupt):
                    self.assertFalse(run['evidence_complete'])
                with closing(self.runner.safe_readback(self.run_dir / 'catalog.sqlite')) as c:
                    self.assertEqual(c.execute('SELECT count(*) FROM snapshots').fetchone()[0], 1)

    def test_verification_failure_keeps_uncertainty(self):
        with patch.object(self.runner, 'verify_scan', side_effect=RuntimeError('verification')):
            run = self.execute()
        self.assertEqual(run['persistence'], 'not_established')

    def test_roundtrip_distinguishes_retained_boolean_from_numeric_one(self):
        from model_price_watcher import storage
        original = storage.get_successful_history
        def changed_type(*args, **kwargs):
            history = []
            for snapshot, observations in original(*args, **kwargs):
                changed = []
                for observation in observations:
                    if observation.offering_id == 'evidence':
                        raw = dict(observation.source_record, new_field=Decimal(1))
                        conditions = dict(observation.conditions,
                            retained_fields=dict(observation.conditions['retained_fields'], new_field=Decimal(1)))
                        observation = replace(observation, source_record=raw, conditions=conditions)
                    changed.append(observation)
                history.append((snapshot, tuple(changed)))
            return tuple(history)
        with patch.object(storage, 'get_successful_history', changed_type):
            run = self.execute()
        self.assertEqual(run['persistence'], 'not_established')
        self.assertEqual(run['exit_code'], 7)

    def test_evidence_failure_after_verification_retains_persistence(self):
        original = self.runner.write_json
        def fail_selected(path, value):
            if path.name == 'selected-results.json':
                raise OSError('synthetic output failure')
            return original(path, value)
        with patch.object(self.runner, 'write_json', fail_selected):
            run = self.execute()
        self.assertEqual(run['persistence'], 'established')
        self.assertEqual(run['exit_code'], 8)
        self.assertFalse(run['evidence_complete'])

    def test_reconciliation_is_importable_read_only_and_source_bound(self):
        run = self.execute()
        before = {p.name: p.read_bytes() for p in self.run_dir.iterdir()}
        with patch('socket.socket', side_effect=AssertionError('network')):
            finding = self.runner.reconcile_run(self.run_dir, expected_sources=run['sources_before'])
        self.assertEqual(finding['persistence'], 'established')
        self.assertTrue(finding['package_reconciled'])
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.run_dir.iterdir()})
        (self.run_dir / 'catalog.sqlite-journal').write_bytes(b'untouched')
        before = {p.name: p.read_bytes() for p in self.run_dir.iterdir()}
        finding = self.runner.reconcile_run(self.run_dir, expected_sources=run['sources_before'])
        self.assertEqual(finding['persistence'], 'not_established')
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.run_dir.iterdir()})

    def test_reconciliation_detects_rehashed_summary_and_selection_disagreement(self):
        for name, mutate in (
            ('catalog-summary.json', lambda data: data['categories'].update(complete=999)),
            ('selected-results.json', lambda data: data.update(sample_ids=['invented'])),
        ):
            with tempfile.TemporaryDirectory() as temporary:
                self.run_dir = Path(temporary) / 'run'
                run = self.execute()
                artifact = self.artifact(name)
                mutate(artifact)
                self.runner.write_json(self.run_dir / name, artifact)
                manifest = self.run_dir / 'manifest.sha256'
                manifest.write_text(''.join(hashlib.sha256((self.run_dir / entry.split('  ')[1]).read_bytes()).hexdigest()
                    + '  ' + entry.split('  ')[1] + '\n' for entry in manifest.read_text().splitlines()))
                before = {p.name: p.read_bytes() for p in self.run_dir.iterdir()}
                finding = self.runner.reconcile_run(self.run_dir, expected_sources=run['sources_before'])
                self.assertFalse(finding['package_reconciled'])
                self.assertEqual(before, {p.name: p.read_bytes() for p in self.run_dir.iterdir()})

    def test_reconciliation_manifest_mismatch_preserves_independent_persistence_fact(self):
        run = self.execute()
        with (self.run_dir / 'selected-results.json').open('ab') as out:
            out.write(b'\n')
        finding = self.runner.reconcile_run(self.run_dir, expected_sources=run['sources_before'])
        self.assertEqual(finding['persistence'], 'established')
        self.assertFalse(finding['package_reconciled'])

    def test_pre_request_record_does_not_claim_absence_if_process_lost_later(self):
        from model_price_watcher.cheaper_inference_acquisition import scan_cheaper_inference
        def inspect_record(*args, **kwargs):
            record = self.artifact('run.json')
            self.assertEqual(record['persistence'], 'not_established')
            self.assertEqual(record['request_count'], None)
            return scan_cheaper_inference(*args, **kwargs)
        with patch('model_price_watcher.cheaper_inference_acquisition.scan_cheaper_inference', inspect_record):
            self.assertEqual(self.execute()['exit_code'], 0)

    def test_verification_acquisition_exception_does_not_claim_prewrite_absence(self):
        from model_price_watcher.acquisition import AcquisitionError
        with patch.object(self.runner, 'verify_scan', side_effect=AcquisitionError('catalog_rejected')):
            run = self.execute()
        self.assertEqual(run['persistence'], 'not_established')
        self.assertFalse((self.run_dir / 'rejected-body.bin').exists())

    def test_one_budget_wrapper_blocks_second_application_request(self):
        from model_price_watcher.transport import HttpsTransport
        record = {}
        response = FakeResponse(body(), [('Content-Type', 'application/json')])
        transport = self.runner.RecordingTransport(HttpsTransport(
            connection_factory=fake_connection(response, self.requests)), record)
        arguments = dict(headers={'Accept': 'application/json'}, timeout_seconds=30.0, max_response_bytes=16_777_216)
        transport.get('https://api.cheaperinference.com/public/models', **arguments)
        with self.assertRaises(self.runner.Refusal):
            transport.get('https://api.cheaperinference.com/public/models', **arguments)
        self.assertEqual(len(self.requests), 1)

    def test_no_complete_numeric_quote_records_C_and_A_conditions(self):
        run = self.execute(data=body([dict(id='p', input_per_million='?')]))
        self.assertEqual(run['exit_code'], 0)
        findings = self.artifact('verification.json')['findings']
        self.assertEqual(findings, dict(A_conditions_observed=True, B_conditions_observed=False, C_conditions_observed=True))

    def test_absent_sentinel_cannot_collide_with_catalog(self):
        self.execute(data=body([dict(id='__g1_absent_sentinel__')]))
        self.assertEqual(self.artifact('selected-results.json')['absent_sentinel'], '__g1_absent_sentinel___')

    def test_offline_second_parse_no_second_write_and_exact_retained_prices(self):
        from model_price_watcher.providers.cheaper_inference import parse_catalog
        from model_price_watcher.storage import write_snapshot
        with patch('model_price_watcher.providers.cheaper_inference.parse_catalog', wraps=parse_catalog) as parser, \
             patch('model_price_watcher.cheaper_inference_acquisition.write_snapshot', wraps=write_snapshot) as writer:
            run = self.execute(data=body([dict(id='precise', input_per_million='1.12345678901234567890123456789012345')]))
        self.assertEqual(parser.call_count, 1)  # runner reparse; scanner holds its original import
        self.assertEqual(writer.call_count, 1)
        self.assertEqual(len(self.requests), 1)
        report = self.artifact('selected-results.json')['report']
        quote = next(r['current_observation']['advertised_quote'] for r in report['lookups'] if r['offering_id'] == 'precise')
        self.assertIsNone(quote)
        observation = next(r['current_observation'] for r in report['lookups'] if r['offering_id'] == 'precise')
        self.assertEqual(observation['source_record']['input_per_million'], '1.12345678901234567890123456789012345')
        self.assertNotIn('commit_time', run)

    def test_retained_attempt_01_offline_runner_compatibility(self):
        # This is a fake connection replay in a disposable directory, never Attempt 02.
        data = (RUNNER.parents[1]/'tests/fixtures/cheaper_inference_public_catalog_g1b_live_01.json').read_bytes()
        run = self.execute(data=data)
        self.assertEqual(run['exit_code'], 0)
        self.assertEqual(run['persistence'], 'established')
        self.assertEqual(len(self.requests), 1)
        summary = self.artifact('catalog-summary.json')
        self.assertEqual(summary['accepted_observations'], 97)
        self.assertEqual(summary['categories'], dict(complete=0, partial=0, unknown=0, evidence_only=97))
        self.assertEqual(self.artifact('verification.json')['findings'],
                         dict(A_conditions_observed=True, B_conditions_observed=False, C_conditions_observed=True))
        with closing(self.runner.safe_readback(self.run_dir/'catalog.sqlite')) as connection:
            from model_price_watcher.storage import get_successful_history
            from model_price_watcher.providers.cheaper_inference import STREAM_ID
            (snapshot, records), = get_successful_history(connection, STREAM_ID)
        self.assertEqual(snapshot.source.metadata['interpretation_policy'], 'ci-advertised-evidence-only-v2')
        self.assertTrue(all(r.advertised_quote is None for r in records))
        before = {p.name: p.read_bytes() for p in self.run_dir.iterdir()}
        finding = self.runner.reconcile_run(self.run_dir, expected_sources=run['sources_before'])
        self.assertEqual(finding['persistence'], 'established')
        self.assertTrue(finding['package_reconciled'])
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.run_dir.iterdir()})

    def test_unsupported_media_encoding_utf8_and_pagination_not_retained(self):
        cases = [(body(), [('Content-Type', 'text/html')]),
                 (body(), [('Content-Type', 'application/json'), ('Content-Encoding', 'gzip')]),
                 (b'\xff', [('Content-Type', 'application/json')]),
                 (b'{"models":[],"next_page":"more"}', [('Content-Type', 'application/json')])]
        for data, headers in cases:
            with tempfile.TemporaryDirectory() as temporary:
                self.run_dir = Path(temporary) / 'run'
                before = len(self.requests)
                run = self.execute(data=data, headers=headers)
                self.assertEqual(run['persistence'], 'absent')
                self.assertEqual(len(self.requests) - before, 1)
                self.assertEqual((self.run_dir / 'rejected-body.bin').exists(), run['failure'] == 'catalog_rejected')

    def test_source_drift_at_completion_prevents_clean_binding(self):
        real = self.runner.source_inventory
        before = real()
        after = [dict(r) for r in before]
        after[0]['sha256'] = '0' * 64
        with patch.object(self.runner, 'source_inventory', side_effect=[before, after]):
            run = self.execute()
        self.assertEqual(run['persistence'], 'established')
        self.assertEqual(run['exit_code'], 7)
        self.assertFalse(run['evidence_complete'])
        self.assertFalse(self.artifact('verification.json')['source_binding']['clean'])
        self.assertEqual(len(self.requests), 1)

    def test_actual_source_byte_change_is_detected(self):
        real_read = Path.read_bytes
        before = self.runner.source_inventory()
        def altered_read(path):
            data = real_read(path)
            return data + b'\n# synthetic change\n' if path == RUNNER else data
        with patch.object(Path, 'read_bytes', altered_read):
            after = self.runner.source_inventory()
        self.assertEqual(self.runner.compare_sources(before, after)['changed_bytes'], [None])

    def test_commit_and_rollback_failure_do_not_establish_absence(self):
        import model_price_watcher.storage as storage
        class UncertainConnection(sqlite3.Connection):
            def execute(self, sql, *args, **kwargs):
                if sql == 'BEGIN IMMEDIATE':
                    self.writing = True
                if sql == 'COMMIT' and self.writing:
                    raise sqlite3.OperationalError('synthetic commit failure')
                return super().execute(sql, *args, **kwargs)
            def rollback(self):
                raise sqlite3.OperationalError('synthetic rollback failure')
        def opener(path):
            # Reuse F initialization and sanctioned schema on the disposable DB.
            storage.open_database(path).close()
            c = sqlite3.connect(path, isolation_level=None, factory=UncertainConnection)
            c.row_factory = sqlite3.Row
            c.execute('PRAGMA foreign_keys=ON')
            c.writing = False
            return c
        original = storage.open_database
        def independent_opener(path):
            with patch.object(storage, 'open_database', original):
                return opener(path)
        with patch.object(storage, 'open_database', independent_opener):
            run = self.execute()
        self.assertEqual(run['persistence'], 'not_established')
        self.assertEqual(run['exit_code'], 7)

    def test_readonly_reconciliation_absence_mismatch_and_postcommit_confirmation(self):
        from model_price_watcher.storage import write_snapshot
        from model_price_watcher.providers.cheaper_inference import parse_catalog, SOURCE_URL, SOURCE_METADATA
        from model_price_watcher.models import SourceMetadata
        captured = {}
        def committed(*args, **kwargs):
            captured['snapshot'] = write_snapshot(*args, **kwargs)
            captured['observations'] = kwargs['observations']
            raise RuntimeError('postcommit')
        with patch('model_price_watcher.cheaper_inference_acquisition.write_snapshot', committed):
            run = self.execute()
        before = {p.name: p.read_bytes() for p in self.run_dir.iterdir()}
        finding = self.runner.reconcile_run(self.run_dir, expected_sources=run['sources_before'],
            expected_snapshot=self.runner.json_value(captured['snapshot']),
            expected_observation_sha256=self.runner.semantic_digest(captured['observations']))
        self.assertEqual(finding['persistence'], 'established')
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.run_dir.iterdir()})
        bad = self.runner.reconcile_run(self.run_dir, expected_sources=run['sources_before'],
            expected_snapshot=self.runner.json_value(captured['snapshot']), expected_observation_sha256='0' * 64)
        self.assertEqual(bad['persistence'], 'not_established')
        with tempfile.TemporaryDirectory() as temporary:
            self.run_dir = Path(temporary) / 'absent'
            run = self.execute(status=500)
            absent = self.runner.reconcile_run(self.run_dir, expected_sources=run['sources_before'])
            self.assertEqual(absent['persistence'], 'absent')

    def test_output_failure_keeps_established_fact(self):
        class BrokenOutput:
            def write(self, value):
                raise OSError('synthetic output failure')
        response = FakeResponse(body(), [('Content-Type', 'application/json')])
        run = self.runner.execute_run(self.run_dir,
            connection_factory=fake_connection(response, self.requests), clock=lambda: BASE,
            stdout=BrokenOutput(), stderr=io.StringIO())
        self.assertEqual(run['persistence'], 'established')
        self.assertEqual(run['exit_code'], 8)

    def test_clock_earlier_than_existing_completion_refuses_before_request(self):
        from model_price_watcher import storage
        from model_price_watcher.models import SourceMetadata
        from model_price_watcher.providers.cheaper_inference import parse_catalog, SOURCE_URL, SOURCE_METADATA, STREAM_ID
        original = storage.open_database
        def unexpected_nonempty(path):
            c = original(path)
            source = SourceMetadata(SOURCE_URL, dict(SOURCE_METADATA))
            parsed = parse_catalog(body().decode(), observed_at=BASE + timedelta(days=1), source=source)
            storage.write_snapshot(c, provider=STREAM_ID, started_at=BASE + timedelta(days=1),
                completed_at=BASE + timedelta(days=1, seconds=1), source=source, observations=parsed.observations)
            return c
        with patch.object(storage, 'open_database', unexpected_nonempty):
            run = self.execute()
        self.assertEqual(run['exit_code'], 7)
        self.assertIn('chronology', run['failure'])
        self.assertNotIn('corrupt', run['failure'])
        self.assertEqual(self.requests, [])
        self.assertEqual(run['persistence'], 'absent')  # this attempted scan never began

    def test_recording_failure_is_isolated_and_marks_evidence_incomplete(self):
        response = FakeResponse(body(), [('Content-Type', 'application/json')])
        calls = 0
        original = response.getheaders
        def headers():
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError('recording only')
            return original()
        response.getheaders = headers
        run = self.runner.execute_run(self.run_dir,
            connection_factory=fake_connection(response, self.requests), clock=lambda: BASE,
            stdout=io.StringIO(), stderr=io.StringIO())
        self.assertEqual(run['persistence'], 'established')
        self.assertFalse(run['http']['headers_complete'])
        self.assertIsNone(run['http']['headers'])
        self.assertFalse(run['evidence_complete'])
        self.assertEqual(response.reads, 2)
        self.assertEqual(len(self.requests), 1)

    def test_transport_semantics_match_without_recording_wrapper(self):
        from model_price_watcher.transport import HttpsTransport
        args = dict(headers={'Accept': 'application/json', 'Accept-Encoding': 'identity'},
                    timeout_seconds=30.0, max_response_bytes=16_777_216)
        headers = [('Content-Type', 'application/json'), ('Transfer-Encoding', 'chunked'), ('Unused', 'ignored')]
        plain_response = FakeResponse(body(), headers)
        recorded_response = FakeResponse(body(), headers)
        plain_calls, recorded_calls, record = [], [], {}
        plain = HttpsTransport(connection_factory=fake_connection(plain_response, plain_calls))
        recorded = HttpsTransport(connection_factory=self.runner.recording_connection_factory(
            record, fake_connection(recorded_response, recorded_calls)))
        self.assertEqual(plain.get('https://api.cheaperinference.com/public/models', **args),
                         recorded.get('https://api.cheaperinference.com/public/models', **args))
        self.assertEqual(plain_calls, recorded_calls)
        self.assertEqual(plain_response.reads, recorded_response.reads)
        self.assertEqual(record['headers'], [list(h) for h in headers[:2]])

    def test_overflow_and_partial_body_have_unavailable_complete_body_evidence(self):
        for data, headers, reason in (
            (b'x' * 16_777_217, [('Content-Type', 'application/json')], 'response_too_large'),
            (b'{}', [('Content-Type', 'application/json'), ('Content-Length', '3')], 'incomplete_body'),
        ):
            with tempfile.TemporaryDirectory() as temporary:
                self.run_dir = Path(temporary) / 'run'
                run = self.execute(data=data, headers=headers)
                self.assertEqual(run['failure'], reason)
                self.assertIsNone(run['http']['body_bytes'])
                self.assertIsNone(run['http']['body_sha256'])
                self.assertFalse((self.run_dir / 'rejected-body.bin').exists())

    def test_pre_request_evidence_failure_never_spends_request_budget(self):
        with patch.object(self.runner, 'write_json', side_effect=OSError('preflight evidence failure')):
            run = self.execute()
        self.assertEqual(run['persistence'], 'absent')
        self.assertEqual(run['request_count'], 0)
        self.assertEqual(self.requests, [])

    def test_clock_invalid_or_backwards_is_internal_not_corruption(self):
        for times, request_count in (([BASE.replace(tzinfo=None)], 0),
                                      ([BASE, BASE - timedelta(seconds=1)], 0),
                                      ([BASE, BASE, BASE, BASE, BASE - timedelta(seconds=1)], 1)):
            with tempfile.TemporaryDirectory() as temporary:
                self.run_dir = Path(temporary) / 'run'
                values = iter(times)
                response = FakeResponse(body(), [('Content-Type', 'application/json')])
                requests = []
                run = self.runner.execute_run(self.run_dir,
                    connection_factory=fake_connection(response, requests), clock=lambda: next(values),
                    stdout=io.StringIO(), stderr=io.StringIO())
                self.assertEqual(run['exit_code'], 7)
                self.assertIn('chronology', run['failure'])
                self.assertNotIn('corrupt', run['failure'])
                self.assertEqual(len(requests), request_count)

    def test_cli_only_two_modes_and_no_abbreviation_or_repeat(self):
        for args in ([], ['--describe-sources', '--execute-live'],
                     ['--describe-sources', '--run-dir', str(self.run_dir)],
                     ['--execute-live'], ['--run-dir', str(self.run_dir)],
                     ['--desc'], ['--describe-sources', '--describe-sources'],
                     ['--reconcile'], ['--execute-live', '--run-dir', str(self.run_dir), '--run-dir=x']):
            with self.subTest(args=args), patch('sys.stderr', io.StringIO()):
                with self.assertRaises(SystemExit) as error:
                    self.runner.main(args)
                self.assertEqual(error.exception.code, 2)
        self.assertFalse(self.run_dir.exists())

    def test_cli_executes_injected_fake_without_real_network(self):
        response = FakeResponse(body(), [('Content-Type', 'application/json')])
        with patch('socket.socket.connect', side_effect=AssertionError('real network')):
            code = self.runner.main(['--execute-live', '--run-dir', str(self.run_dir)],
                connection_factory=fake_connection(response, self.requests), clock=lambda: BASE,
                stdout=io.StringIO(), stderr=io.StringIO())
        self.assertEqual(code, 0)
        self.assertEqual(len(self.requests), 1)


class SafeReadbackTests(unittest.TestCase):
    def test_unsafe_files_are_refused_before_sqlite_connect(self):
        runner = load_runner()
        with tempfile.TemporaryDirectory() as temporary:
            db = Path(temporary) / 'db'
            for data in (b'', b'not sqlite' * 20,
                         b'SQLite format 3\0' + b'\0\0\2\2' + b'\0' * 80):
                db.write_bytes(data)
                with patch('sqlite3.connect', side_effect=AssertionError('unsafe open')):
                    with self.assertRaises(runner.Refusal):
                        runner.safe_readback(db)

    def test_sidecars_symlinks_uri_and_query_only(self):
        runner = load_runner()
        from model_price_watcher.storage import open_database
        with tempfile.TemporaryDirectory() as temporary:
            db = Path(temporary) / '?immutable=1&mode=memory #% é.sqlite'
            open_database(db).close()
            before = db.read_bytes()
            c = runner.safe_readback(db)
            with self.assertRaises(sqlite3.OperationalError):
                c.execute('DELETE FROM snapshots')
            c.close()
            self.assertEqual(db.read_bytes(), before)
            for suffix in ('-journal', '-wal', '-shm'):
                sidecar = Path(str(db) + suffix)
                sidecar.symlink_to(Path(temporary) / 'missing')
                with self.assertRaises(runner.Refusal):
                    runner.safe_readback(db)
                sidecar.unlink()  # ordinary disposable test artifacts only
            link = Path(temporary) / 'link'
            link.symlink_to(db)
            with self.assertRaises(runner.Refusal):
                runner.safe_readback(link)

    def test_wal_header_without_sidecars_and_missing_database_refused(self):
        runner = load_runner()
        from model_price_watcher.storage import open_database
        with tempfile.TemporaryDirectory() as temporary:
            db = Path(temporary) / 'db'
            c = open_database(db)
            c.execute('PRAGMA journal_mode=WAL')
            c.close()
            before = {p.name: p.read_bytes() for p in Path(temporary).iterdir()}
            self.assertEqual(db.read_bytes()[18:20], b'\2\2')
            with patch('sqlite3.connect', side_effect=AssertionError('WAL open')):
                with self.assertRaises(runner.Refusal):
                    runner.safe_readback(db)
            self.assertEqual(before, {p.name: p.read_bytes() for p in Path(temporary).iterdir()})
            missing = Path(temporary) / 'missing'
            with self.assertRaises(FileNotFoundError):
                runner.safe_readback(missing)
            self.assertFalse(missing.exists())

    def test_symlinked_ancestor_accepted_and_version_or_schema_refusal_nonmutating(self):
        runner = load_runner()
        from model_price_watcher.storage import open_database, StorageError
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'real').mkdir()
            (root / 'ancestor').symlink_to(root / 'real', target_is_directory=True)
            db = root / 'ancestor/catalog.sqlite'
            open_database(db).close()
            runner.safe_readback(db).close()
            for sql in ('PRAGMA user_version=1', 'PRAGMA user_version=2; CREATE TABLE unexpected (x)'):
                c = sqlite3.connect(db)
                c.executescript(sql)
                c.close()
                before = db.read_bytes()
                with self.assertRaises((runner.Refusal, StorageError)):
                    runner.safe_readback(db)
                self.assertEqual(db.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
