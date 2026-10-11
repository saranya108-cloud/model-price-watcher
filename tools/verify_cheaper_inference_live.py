"""Source description and explicitly opt-in, single-request G1 evidence runner."""
import sys

# Set before any project imports, including when loaded by filesystem path.
sys.dont_write_bytecode = True

import argparse
from dataclasses import fields, is_dataclass
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
import hashlib
import http.client
import importlib
import inspect
import json
from pathlib import Path
import sqlite3
import stat
import time

CHECKOUT = Path(__file__).resolve().parents[1]
PACKAGE_TREE = CHECKOUT / 'src/model_price_watcher'
MAX_BODY = 16_777_216
RELEVANT_HEADERS = {'content-type', 'content-encoding', 'content-length', 'transfer-encoding'}
ARTIFACTS = ('run.json', 'catalog.sqlite', 'catalog-summary.json',
             'selected-results.json', 'verification.json')
LIMITATIONS = [
    'Source hashes prove recorded source-file bytes at measured paths, not Git commit identity, '
    'arbitrary runtime integrity, bytecode provenance, or machine integrity.',
    'Body hashes do not prove provider authenticity or HTTP wire framing.',
    'Successful parsing does not prove upstream completeness beyond the received accepted response.',
    'Accepted database evidence supports semantic replay, not original HTTP wire reconstruction.',
    'Structural inventory is review evidence, not a second schema interpreter.',
    'One catalog cannot prove historical decrease behavior.',
]


def utc(value):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise Refusal('internal/chronology: invalid clock', 7)
    try:
        return value.astimezone(timezone.utc)
    except (ValueError, OverflowError) as error:
        raise Refusal('internal/chronology: unrepresentable clock', 7) from error


def now():
    return datetime.now(timezone.utc)


def json_value(value):
    if isinstance(value, datetime):
        return utc(value).isoformat().replace('+00:00', 'Z')
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return {f.name: json_value(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, dict):
        return {k: json_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_value(v) for v in value]
    return value


def document(**items):
    return dict(format_version=1, recorded_at=json_value(now()), **items)


def write_json(path, value):
    # Only runner-owned files in an exclusively created directory are updated.
    path.write_text(json.dumps(json_value(value), sort_keys=True, ensure_ascii=True,
                               indent=2, allow_nan=False) + '\n', encoding='utf-8')


def digest(data):
    return hashlib.sha256(data).hexdigest()


class Refusal(RuntimeError):
    def __init__(self, reason, code=7):
        self.reason, self.code = reason, code
        super().__init__(reason)


def eager_import():
    # Imports cover success, failure, parser, persistence, readback and queries.
    # This is an import phase, never an inventory allowlist.
    for name in ('acquisition', 'cheaper_inference_acquisition',
                 'advertised_queries', 'storage', 'transport'):
        importlib.import_module('model_price_watcher.' + name)


def source_inventory():
    eager_import()
    sources = [(None, Path(__file__).resolve())]
    for name, module in sorted(tuple(sys.modules.items())):
        if name != 'model_price_watcher' and not name.startswith('model_price_watcher.'):
            continue
        try:
            source = inspect.getsourcefile(module)
            if source is None:
                raise ValueError('unavailable source')
            path = Path(source).resolve(strict=True)
            if path.suffix != '.py' or not path.is_relative_to(PACKAGE_TREE):
                raise ValueError('unexpected source origin')
            origin = getattr(getattr(module, '__spec__', None), 'origin', None)
            if origin is None or Path(origin).resolve(strict=True) != path:
                raise ValueError('inconsistent source origin')
        except (OSError, TypeError, ValueError) as error:
            raise Refusal('uninspectable package source: ' + name) from error
        sources.append((name, path))
    rows = []
    for name, path in sources:
        try:
            data = path.read_bytes()
        except OSError as error:
            raise Refusal('source unavailable: ' + str(path)) from error
        rows.append(dict(module=name, path=str(path), bytes=len(data),
                         sha256=hashlib.sha256(data).hexdigest()))
    return rows


def compare_sources(before, after):
    old = {row['module']: row for row in before}
    new = {row['module']: row for row in after}
    added = sorted(set(new) - set(old), key=str)
    missing = sorted(set(old) - set(new), key=str)
    origins = [name for name in old.keys() & new.keys() if old[name]['path'] != new[name]['path']]
    changed = [name for name in old.keys() & new.keys()
               if (old[name]['sha256'], old[name]['bytes']) != (new[name]['sha256'], new[name]['bytes'])]
    return dict(clean=not (added or missing or origins or changed), added=added,
                missing=missing, changed_origins=sorted(origins, key=str),
                changed_bytes=sorted(changed, key=str))


def safe_readback(path):
    """Trusted quiescent filesystem only; no recovery, migration or WAL access."""
    path = Path(path)  # Do not resolve away the final-component symlink.
    if not path.is_absolute():
        raise Refusal('database path must be absolute', 2)
    if not stat.S_ISREG(path.lstat().st_mode):
        raise Refusal('database must be a regular non-symlink file', 8)
    for suffix in ('-journal', '-wal', '-shm'):
        sidecar = Path(str(path) + suffix)
        try:
            sidecar.lstat()
        except FileNotFoundError:
            continue
        detail = ' Leftover journals normally indicate interrupted write/recovery state.' if suffix == '-journal' else ''
        raise Refusal('Sidecar ' + json.dumps(str(sidecar), ensure_ascii=True) +
                      ' was not modified; active or recovery-related SQLite state is outside '
                      'this runner boundary.' + detail, 8)
    with path.open('rb') as stream:
        header = stream.read(100)
    if len(header) != 100 or header[:16] != b'SQLite format 3\0':
        raise Refusal('malformed SQLite header', 6)
    if header[18:20] != b'\1\1':
        raise Refusal('WAL or unsupported SQLite journal format; file was not modified', 8)
    connection = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, isolation_level=None)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA foreign_keys=ON')
        connection.execute('PRAGMA query_only=ON')
        connection.execute('BEGIN')
        validate_database(connection)
        return connection
    except BaseException:
        connection.close()
        raise


def validate_database(connection):
    from model_price_watcher import storage
    if connection.execute('PRAGMA user_version').fetchone()[0] != 2:
        raise Refusal('schema v2 required; no migration performed', 5)
    if [tuple(r) for r in connection.execute('PRAGMA integrity_check')] != [('ok',)]:
        raise Refusal('SQLite physical integrity check failed', 6)
    # Sanctioned private verifier, including exact schema and FK checks; no DDL copy.
    storage._verify_schema(connection, 2)


def recording_connection_factory(record, connection_type=http.client.HTTPSConnection):
    class RecordingConnection(connection_type):
        def getresponse(self):
            response = super().getresponse()
            try:
                record['headers'] = [[k, v] for k, v in response.getheaders()
                                     if k.lower() in RELEVANT_HEADERS]
                record['headers_complete'] = True
            except Exception:
                # Header recording must never change production transport decisions.
                record['headers'] = None
                record['headers_complete'] = False
            return response
    return RecordingConnection


class RecordingTransport:
    def __init__(self, transport, record):
        self.transport, self.record = transport, record
        self.count = 0
        self.body = None

    def get(self, *args, **kwargs):
        if self.count:
            raise Refusal('request budget exhausted', 7)
        self.count += 1
        self.record['request_started_at'] = json_value(now())
        started = time.monotonic()
        try:
            response = self.transport.get(*args, **kwargs)
            from model_price_watcher.transport import HttpResponse
            if isinstance(response, HttpResponse) and isinstance(response.body, bytes) and len(response.body) <= MAX_BODY:
                self.body = response.body
                self.record.update(status=response.status, body_bytes=len(response.body),
                                   body_sha256=digest(response.body))
            return response
        finally:
            self.record['request_ended_at'] = json_value(now())
            self.record['monotonic_elapsed_seconds'] = time.monotonic() - started


def observation_value(item):
    """Common semantic fields of parser and storage records, without a second validator."""
    from model_price_watcher.models import CatalogObservation
    if isinstance(item, CatalogObservation):
        return dict(offering_id=item.offering_id, observed_at=item.observed_at,
                    input=item.input_usd_per_million, output=item.output_usd_per_million,
                    conditions=item.unsupported_pricing, source_record=item.raw_offering,
                    advertised_quote=item.advertised_quote)
    return dict(offering_id=item.offering_id, observed_at=item.observed_at,
                input=item.input_per_million, output=item.output_per_million,
                conditions=item.conditions, source_record=item.source_record,
                advertised_quote=item.advertised_quote)


def semantic_digest(observations):
    # Type-tag every node so retained numeric JSON and numeric-looking strings
    # cannot collide. Canonical Decimal tuples ignore representation-only zeros.
    def tagged(value):
        if isinstance(value, Decimal):
            sign, digits, exponent = value.as_tuple()
            digits = list(digits)
            if not any(digits):
                return ['decimal', 0, [0], 0]
            while digits[-1] == 0:
                digits.pop()
                exponent += 1
            return ['decimal', sign, digits, exponent]
        if is_dataclass(value):
            value = {f.name: getattr(value, f.name) for f in fields(value)}
        if isinstance(value, datetime):
            return ['utc', json_value(value)]
        if isinstance(value, dict):
            return ['object', [[k, tagged(v)] for k, v in sorted(value.items())]]
        if isinstance(value, (tuple, list)):
            return ['array', [tagged(v) for v in value]]
        return [type(value).__name__, value]
    values = [observation_value(o) for o in sorted(observations, key=lambda o: o.offering_id)]
    return digest(json.dumps(tagged(values), ensure_ascii=True, separators=(',', ':'),
                             allow_nan=False).encode('ascii'))


def observation_summary(observations):
    categories = dict(complete=0, partial=0, unknown=0, evidence_only=0)
    inventory = {}
    def walk(value, path):
        kind = ('null' if value is None else 'boolean' if isinstance(value, bool) else
                'number' if isinstance(value, (Decimal, int)) else 'string' if isinstance(value, str)
                else 'object' if isinstance(value, dict) else 'array')
        inventory.setdefault(path, set()).add(kind)
        if isinstance(value, dict):
            for key, child in value.items():
                walk(child, path + '[' + json.dumps(key, ensure_ascii=True) + ']')
        elif isinstance(value, (tuple, list)):
            for child in value:
                walk(child, path + '[]')
    for item in observations:
        quote = item.advertised_quote
        count = 0 if quote is None else sum(v is not None for v in
                       (quote.input_usd_per_million, quote.output_usd_per_million))
        category = 'evidence_only' if quote is None else ('unknown', 'partial', 'complete')[count]
        categories[category] += 1
        walk(observation_value(item)['source_record'], '$.models[]')
    return dict(accepted_observations=len(observations), categories=categories,
                numeric_component_observations=categories['complete'] + categories['partial'],
                complete_numeric_quotes=categories['complete'],
                structural_inventory={k: sorted(v) for k, v in sorted(inventory.items())})


def summarize(parsed):
    return document(accepted=parsed.accepted, diagnostics=json_value(parsed.diagnostics),
                    upstream_rows=None, invalid_rows=None, **observation_summary(parsed.observations))


def selected_verification(connection, observations, evaluated_at):
    from model_price_watcher.advertised_queries import view_selected_advertised_offerings
    from model_price_watcher.queries import LookupStatus
    groups = dict(complete=[], partial=[], evidence_only=[])
    by_id = {o.offering_id: o for o in observations}
    for identity, item in sorted(by_id.items()):
        quote = item.advertised_quote
        if quote is None:
            groups['evidence_only'].append(identity)
        else:
            count = sum(v is not None for v in (quote.input_usd_per_million, quote.output_usd_per_million))
            if count:
                groups['complete' if count == 2 else 'partial'].append(identity)
    sample = groups['complete'][:2] + groups['partial'][:1] + groups['evidence_only'][:1]
    sentinel = '__g1_absent_sentinel__'
    while sentinel in by_id:
        sentinel += '_'
    report = view_selected_advertised_offerings(connection, offering_ids=sample + [sentinel], now=evaluated_at)
    for lookup in report.lookups:
        if lookup.offering_id == sentinel:
            if lookup.status != LookupStatus.NEVER_OBSERVED or lookup.current_observation is not None:
                raise Refusal('absent sentinel disagreement')
        elif (lookup.status != LookupStatus.CURRENT or lookup.current_observation != by_id[lookup.offering_id]
              or lookup.detection.current_quote != by_id[lookup.offering_id].advertised_quote):
            raise Refusal('selected exact-ID disagreement')
    return document(sample_ids=sample, absent_sentinel=sentinel, agreement=True, report=json_value(report))


def verify_scan(path, result, body, observed_at, evaluated_at):
    from model_price_watcher import storage
    from model_price_watcher.providers.cheaper_inference import parse_catalog, STREAM_ID
    connection = safe_readback(path)
    try:
        history = storage.get_successful_history(connection, STREAM_ID)
        total = connection.execute('SELECT count(*) FROM snapshots').fetchone()[0]
        if total != 1 or len(history) != 1 or history[0][0] != result.snapshot:
            raise Refusal('stored snapshot identity disagreement')
        snapshot, stored = history[0]
        if evaluated_at < snapshot.completed_at:
            raise Refusal('internal/chronology: evaluation precedes stored completion', 7)
        parsed = parse_catalog(body.decode('utf-8'), observed_at=observed_at, source=snapshot.source)
        if not parsed.accepted or not stored or len(stored) != result.observation_count:
            raise Refusal('offline reparse/count disagreement')
        expected = sorted((observation_value(o) for o in parsed.observations), key=lambda o: o['offering_id'])
        actual = sorted((observation_value(o) for o in stored), key=lambda o: o['offering_id'])
        if (expected != actual or semantic_digest(parsed.observations) != semantic_digest(stored)
                or len(stored) != connection.execute('SELECT count(*) FROM observations').fetchone()[0]):
            raise Refusal('complete observation round-trip disagreement')
        selected = selected_verification(connection, stored, evaluated_at)
        return summarize(parsed), selected, semantic_digest(stored)
    finally:
        connection.close()


def persistence_message(status, *, failed=False):
    if status == 'established' and failed:
        return 'Persistence succeeded, although the command failed after or during finalization.'
    return {'absent': 'No snapshot was stored.', 'not_established': 'Persistence outcome not established.',
            'established': 'Persistence independently verified.'}[status]


def execute_run(run_dir, *, connection_factory=None, clock=now, stdout=None, stderr=None):
    """One execution; injected connections/clocks are callable-only offline seams."""
    from model_price_watcher import storage
    from model_price_watcher.acquisition import AcquisitionError, ScanResult
    from model_price_watcher.cheaper_inference_acquisition import scan_cheaper_inference
    from model_price_watcher.providers.cheaper_inference import STREAM_ID, SOURCE_URL, SOURCE_METADATA, parse_catalog
    from model_price_watcher.models import SourceMetadata
    from model_price_watcher.transport import HttpsTransport
    stdout, stderr = stdout or sys.stdout, stderr or sys.stderr
    path = Path(run_dir)
    if not path.is_absolute() or path.resolve().is_relative_to(CHECKOUT):
        raise Refusal('run directory must be absolute, new, and outside the checkout', 2)
    try:
        path.lstat()
    except FileNotFoundError:
        pass
    else:
        raise Refusal('run directory already exists; no reuse or overwrite', 2)
    sources = source_inventory()
    path.mkdir()  # Exclusive directory creation, no parents or exist_ok.
    run = document(sources_before=sources, sources_after=None, runtime=dict(python=sys.version, sqlite=sqlite3.sqlite_version),
                   invocation=['--run-dir', str(path), '--execute-live'], endpoint=SOURCE_URL,
                   limits=dict(method='GET', timeout_seconds=30.0, max_response_bytes=MAX_BODY,
                               request_budget=1, wall_clock_deadline=None), limitations=LIMITATIONS,
                   wall_started_at=json_value(now()), wall_ended_at=None, monotonic_elapsed_seconds=None,
                   scanner_clock_values=[], scanner_observed_at=None, scanner_completed_at=None,
                   after_scanner_return_at=None, request_count=0, persistence='absent', phase='preflight',
                   failure=None, scanner_failure=None, failure_phase=None, evidence_errors=[],
                   initial_counts=None, exit_code=7, evidence_complete=False, expected_snapshot=None,
                   expected_observation_sha256=None,
                   http=dict(headers=None, headers_complete=False, status=None, body_bytes=None,
                             body_sha256=None, request_started_at=None, request_ended_at=None,
                             monotonic_elapsed_seconds=None))
    transport = RecordingTransport(HttpsTransport(connection_factory=recording_connection_factory(
        run['http'], connection_factory or http.client.HTTPSConnection)), run['http'])
    connection = None
    began = time.monotonic()
    summary = document(accepted=None, accepted_observations=None, categories=None, diagnostics=None,
                       structural_inventory=None, upstream_rows=None, invalid_rows=None)
    selected = document(sample_ids=None, absent_sentinel=None, agreement=None, report=None)
    verification = document(integrity=None, round_trip=None, source_binding=None,
                            findings=dict(A_conditions_observed=None, B_conditions_observed=None, C_conditions_observed=None),
                            final_determination='Requires separate review and external candidate comparison')
    last_clock = None
    scanner_entered = False
    def scanner_clock():
        nonlocal last_clock
        value = utc(clock())
        if last_clock is not None and value < last_clock:
            raise Refusal('internal/chronology: clock moved backwards', 7)
        last_clock = value
        run['scanner_clock_values'].append(json_value(value))
        if len(run['scanner_clock_values']) == 2:
            run['scanner_observed_at'] = json_value(value)
        elif len(run['scanner_clock_values']) == 3:
            run['scanner_completed_at'] = json_value(value)
        return value
    try:
        connection = storage.open_database(path / 'catalog.sqlite')
        validate_database(connection)
        preflight_time = utc(clock())
        latest = storage.get_latest_successful_snapshot(connection, STREAM_ID)
        if latest is not None and preflight_time < latest.completed_at:
            raise Refusal('internal/chronology: clock precedes stored completion', 7)
        from model_price_watcher.advertised_queries import view_selected_advertised_offerings
        view_selected_advertised_offerings(connection, offering_ids=(), now=preflight_time)
        if any(connection.execute('SELECT count(*) FROM ' + table).fetchone()[0] for table in ('snapshots', 'observations')):
            raise Refusal('new disposable database is not empty', 7)
        run['initial_counts'] = dict(snapshots=0, observations=0)
        last_clock = preflight_time
        # A process lost after this pre-request intent record cannot falsely leave
        # a final absence or zero-request claim. These remain unavailable until
        # normal finalization; intent itself is not proof a request occurred.
        run.update(phase='scanner', persistence='not_established', request_count=None)
        write_json(path / 'run.json', run)
        scanner_entered = True
        result = scan_cheaper_inference(connection, transport=transport, clock=scanner_clock,
                                       timeout_seconds=30.0, max_response_bytes=MAX_BODY)
        if not isinstance(result, ScanResult):
            raise Refusal('scanner did not return ScanResult')
        run['phase'] = 'readback'
        connection.close()
        connection = None
        after_return = utc(clock())
        run['after_scanner_return_at'] = json_value(after_return)
        run['expected_snapshot'] = json_value(result.snapshot)
        observed = datetime.fromisoformat(run['scanner_observed_at'])
        summary, selected, semantic_hash = verify_scan(path / 'catalog.sqlite', result, transport.body, observed, after_return)
        run['expected_observation_sha256'] = semantic_hash
        run['persistence'] = 'established'
        verification.update(integrity=True, round_trip=True)
        verification['findings'].update(A_conditions_observed=True,
            B_conditions_observed=summary['complete_numeric_quotes'] > 0,
            C_conditions_observed=summary['complete_numeric_quotes'] == 0)
        run['exit_code'] = 0
    except AcquisitionError as error:
        prewrite = scanner_entered and run['phase'] == 'scanner'
        if prewrite:
            run['scanner_failure'] = error.reason
        run.update(persistence='absent' if prewrite else 'not_established', failure=error.reason,
                   exit_code=4 if error.reason == 'network_error' else 3 if error.reason in {
                       'invalid_framing', 'response_too_large', 'incomplete_body', 'unsupported_encoding',
                       'http_status', 'unsupported_media_type', 'invalid_response', 'invalid_utf8',
                       'catalog_rejected', 'empty_catalog'} else 7)
        if not prewrite:
            run['exit_code'] = 7
        if prewrite and error.reason == 'catalog_rejected' and transport.body is not None:
            try:
                (path / 'rejected-body.bin').write_bytes(transport.body)
                parsed = parse_catalog(transport.body.decode('utf-8'),
                    observed_at=datetime.fromisoformat(run['scanner_observed_at']),
                    source=SourceMetadata(SOURCE_URL, dict(SOURCE_METADATA)))
                summary = summarize(parsed)
            except Exception:
                run['evidence_errors'].append('rejected_body_recording_incomplete')
                run.update(failure='rejection_evidence_incomplete', exit_code=8)
    except KeyboardInterrupt:
        run.update(failure='interrupted', exit_code=130)
    except Refusal as error:
        run.update(failure=error.reason, exit_code=error.code)
    except OSError:
        run.update(failure='operational_io_failure', exit_code=8)
    except Exception:
        run.update(failure='internal_failure', exit_code=7)
    finally:
        if run['failure'] is not None:
            run['failure_phase'] = run['phase']
        if not scanner_entered:
            run['persistence'] = 'absent'
        if connection is not None:
            try:
                connection.close()
            except Exception:
                run.update(failure='connection_close_failure', exit_code=7)
    run['request_count'] = transport.count
    run['wall_ended_at'] = json_value(now())
    run['monotonic_elapsed_seconds'] = time.monotonic() - began
    try:
        run['sources_after'] = source_inventory()
        verification['source_binding'] = compare_sources(sources, run['sources_after'])
    except Exception:
        verification['source_binding'] = dict(clean=False, failure='final_source_inventory_unavailable')
    clean = verification['source_binding']['clean']
    if not clean or (transport.count and not run['http']['headers_complete']):
        verification['findings']['A_conditions_observed'] = False
        if run['exit_code'] == 0:
            run.update(exit_code=7, failure='source_or_recording_incomplete')
            run['failure_phase'] = 'source_reconciliation'
    run['phase'] = 'artifacts'
    try:
        write_json(path / 'catalog-summary.json', summary)
        write_json(path / 'selected-results.json', selected)
        write_json(path / 'verification.json', verification)
        print(persistence_message(run['persistence'], failed=run['exit_code'] != 0),
              file=stdout if run['exit_code'] == 0 else stderr)
        if run['failure'] is not None:
            print('Failure: ' + json.dumps(run['failure'], ensure_ascii=True), file=stderr)
        run['evidence_complete'] = (clean and run['exit_code'] != 130 and not run['evidence_errors']
                                    and (transport.count == 0 or run['http']['headers_complete']))
        run['phase'] = 'finished'
        write_json(path / 'run.json', run)
        names = list(ARTIFACTS) + (['rejected-body.bin'] if (path / 'rejected-body.bin').exists() else [])
        manifest = ''.join(digest((path / name).read_bytes()) + '  ' + name + '\n' for name in sorted(names))
        (path / 'manifest.sha256').write_text(manifest, encoding='ascii')
    except (Exception, KeyboardInterrupt) as error:
        run.update(evidence_complete=False, failure='evidence_output_incomplete',
                   failure_phase='artifacts', exit_code=130 if isinstance(error, KeyboardInterrupt) else 8)
        try:
            write_json(path / 'run.json', run)
            print(persistence_message(run['persistence'], failed=True) + ' Evidence package incomplete.', file=stderr)
        except (Exception, KeyboardInterrupt):
            pass
    return run


def reconcile_run(run_dir, *, expected_sources, expected_snapshot=None, expected_observation_sha256=None):
    """Independent non-mutating finding. Explicit expectations may supplement an incomplete run.

    Hash equality is a byte comparison, not an approval or authenticity decision.
    No automatic invocation after uncertain scanner failure.
    """
    from model_price_watcher import storage
    from model_price_watcher.providers.cheaper_inference import STREAM_ID
    path = Path(run_dir)
    finding = document(persistence='not_established', package_reconciled=False,
                       manifest_matches=False, sources_match=False, artifacts_agree=False,
                       snapshot_count=None, failure=None)
    connection = None
    try:
        run = json.loads((path / 'run.json').read_text(encoding='utf-8'))
        sources_match = (run['sources_before'] == expected_sources and
                         run['sources_after'] == expected_sources and
                         source_inventory() == expected_sources)
        finding['sources_match'] = sources_match
        expected_snapshot = expected_snapshot if expected_snapshot is not None else run.get('expected_snapshot')
        expected_observation_sha256 = (expected_observation_sha256 if expected_observation_sha256 is not None
                                       else run.get('expected_observation_sha256'))
        connection = safe_readback(path / 'catalog.sqlite')
        history = storage.get_successful_history(connection, STREAM_ID)
        count = connection.execute('SELECT count(*) FROM snapshots').fetchone()[0]
        rows = connection.execute('SELECT count(*) FROM observations').fetchone()[0]
        finding['snapshot_count'] = count
        if count == 0 and rows == 0:
            finding['persistence'] = 'absent'
        elif count == 1 and len(history) == 1:
            snapshot, observations = history[0]
            from model_price_watcher.advertised_queries import view_selected_advertised_offerings
            view_selected_advertised_offerings(connection, offering_ids=(), now=snapshot.completed_at)
            if (observations and rows == len(observations) and json_value(snapshot) == expected_snapshot
                    and semantic_digest(observations) == expected_observation_sha256):
                finding['persistence'] = 'established'
        # Never trust manifest-provided paths: require precisely the sanctioned names.
        names = set(ARTIFACTS)
        if (path / 'rejected-body.bin').exists():
            names.add('rejected-body.bin')
        entries = {}
        for line in (path / 'manifest.sha256').read_text(encoding='ascii').splitlines():
            hash_value, name = line.split('  ', 1)
            if name not in names or name in entries:
                raise Refusal('unexpected or duplicate manifest entry')
            entries[name] = hash_value
        finding['manifest_matches'] = (set(entries) == names and all(
            entries[name] == digest((path / name).read_bytes()) for name in names))
        files_match = {p.name for p in path.iterdir()} == names | {'manifest.sha256'}
        verification = json.loads((path / 'verification.json').read_text(encoding='utf-8'))
        summary = json.loads((path / 'catalog-summary.json').read_text(encoding='utf-8'))
        selected = json.loads((path / 'selected-results.json').read_text(encoding='utf-8'))
        artifacts_agree = False
        if finding['persistence'] == 'established' and run['exit_code'] == 0:
            details = observation_summary(observations)
            evaluated = datetime.fromisoformat(selected['report']['evaluated_at'])
            if evaluated < snapshot.completed_at:
                raise Refusal('internal/chronology: recorded evaluation precedes completion', 7)
            expected_selected = selected_verification(connection, observations, evaluated)
            artifacts_agree = (summary['accepted'] is True
                and all(summary.get(k) == v for k, v in details.items())
                and all(selected.get(k) == v for k, v in expected_selected.items() if k != 'recorded_at')
                and verification['integrity'] is True and verification['round_trip'] is True
                and verification['findings'] == dict(A_conditions_observed=True,
                    B_conditions_observed=details['complete_numeric_quotes'] > 0,
                    C_conditions_observed=details['complete_numeric_quotes'] == 0)
                and run['request_count'] == 1 and 'rejected-body.bin' not in names)
        elif finding['persistence'] == 'absent' and run['persistence'] == 'absent':
            artifacts_agree = selected['agreement'] is None and verification['round_trip'] is None
            if 'rejected-body.bin' in names:
                from model_price_watcher.models import SourceMetadata
                from model_price_watcher.providers.cheaper_inference import SOURCE_URL, SOURCE_METADATA, parse_catalog
                rejected = (path / 'rejected-body.bin').read_bytes()
                parsed = parse_catalog(rejected.decode('utf-8'),
                    observed_at=datetime.fromisoformat(run['scanner_observed_at']),
                    source=SourceMetadata(SOURCE_URL, dict(SOURCE_METADATA)))
                expected_summary = summarize(parsed)
                artifacts_agree = (artifacts_agree and run['failure'] == 'catalog_rejected'
                    and not parsed.accepted and run['http']['status'] == 200
                    and len(rejected) == run['http']['body_bytes'] and digest(rejected) == run['http']['body_sha256']
                    and all(summary.get(k) == v for k, v in expected_summary.items() if k != 'recorded_at'))
        finding['artifacts_agree'] = artifacts_agree
        finding['package_reconciled'] = bool(sources_match and finding['manifest_matches'] and files_match
            and artifacts_agree and run['evidence_complete'] and verification['source_binding']['clean'])
    except (Exception, KeyboardInterrupt) as error:
        finding['failure'] = error.reason if isinstance(error, Refusal) else 'reconciliation_incomplete'
    finally:
        if connection is not None:
            connection.close()
    return finding


def main(argv=None, *, stdout=None, stderr=None, connection_factory=None, clock=now):
    stdout, stderr = stdout or sys.stdout, stderr or sys.stderr
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument('--describe-sources', action='store_true')
    parser.add_argument('--execute-live', action='store_true')
    parser.add_argument('--run-dir')
    argv = list(sys.argv[1:] if argv is None else argv)
    for flag in ('--describe-sources', '--execute-live', '--run-dir'):
        if sum(token.split('=', 1)[0] == flag for token in argv) > 1:
            parser.error('singleton options may not be repeated')
    args = parser.parse_args(argv)
    describe = args.describe_sources and not args.execute_live and args.run_dir is None
    live = args.execute_live and args.run_dir is not None and not args.describe_sources
    if not (describe or live):
        parser.error('choose --describe-sources, or --run-dir ABSOLUTE_NEW_DIRECTORY --execute-live')
    try:
        if describe:
            print(json.dumps(document(sources=source_inventory(), limitations=LIMITATIONS), indent=2), file=stdout)
            return 0
        return execute_run(args.run_dir, connection_factory=connection_factory, clock=clock,
                           stdout=stdout, stderr=stderr)['exit_code']
    except Refusal as error:
        print(error.reason, file=stderr)
        return error.code
    except OSError:
        print('Operational I/O failure before request.', file=stderr)
        return 8


if __name__ == '__main__':
    raise SystemExit(main())
