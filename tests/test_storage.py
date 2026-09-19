"""On-disk SQLite snapshot persistence without network or acquisition."""

import sqlite3
import tempfile
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import ROUND_DOWN, Decimal, InvalidOperation, Overflow, localcontext
from pathlib import Path
from unittest.mock import patch

import model_price_watcher.storage as storage
from model_price_watcher.models import CatalogObservation, SourceMetadata
from model_price_watcher.providers.openrouter import parse_catalog
from model_price_watcher.storage import (
    MAX_JSON_DEPTH,
    SCHEMA_VERSION,
    ObservationRecord,
    SnapshotRecord,
    StorageError,
    get_current_observations,
    get_latest_successful_snapshot,
    get_offering_history,
    get_snapshot_observations,
    get_successful_history,
    get_successful_snapshots,
    open_database,
    write_snapshot,
)


STARTED = datetime(2026, 9, 19, 12, 0, tzinfo=timezone.utc)
OBSERVED = datetime(2026, 9, 19, 12, 30, tzinfo=timezone.utc)
COMPLETED = datetime(2026, 9, 19, 13, 0, tzinfo=timezone.utc)
LATER = datetime(2026, 9, 19, 14, 0, tzinfo=timezone.utc)
EARLIER = datetime(2026, 9, 19, 11, 0, tzinfo=timezone.utc)
EXTRA_READ_FRAMES = 80


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


def _nested_dict(depth):
    node = {}
    for _ in range(depth - 1):
        node = {"n": node}
    return node


def _nested_list(depth):
    if depth == 1:
        return {}
    node = []
    for _ in range(depth - 2):
        node = [node]
    return {"n": node}


def _nested_mixed(depth):
    node = {} if depth % 2 == 1 else []
    for level in range(depth - 1, 0, -1):
        node = {"n": node} if level % 2 == 1 else [node]
    return node


def _nested_object_json(depth):
    text = "{}"
    for _ in range(depth - 1):
        text = '{"n":' + text + "}"
    return text


def _call_with_frames(func, extra, *args, **kwargs):
    if extra <= 0:
        return func(*args, **kwargs)
    return _call_with_frames(func, extra - 1, *args, **kwargs)


@contextmanager
def _deny_sql_begin(connection):
    def authorizer(action, arg1, *_rest):
        if action == sqlite3.SQLITE_TRANSACTION and arg1 == "BEGIN":
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK
    connection.set_authorizer(authorizer)
    try:
        yield
    finally:
        connection.set_authorizer(None)


class StorageTests(unittest.TestCase):
    def setUp(self):
        self._tempdir = tempfile.TemporaryDirectory()
        self.path = str(Path(self._tempdir.name) / "history.sqlite")
        self.connection = open_database(self.path)
        self.source = SourceMetadata("fixture:openrouter", {"capture": "test"})

    def tearDown(self):
        try:
            self.connection.close()
        except sqlite3.Error:
            pass
        self._tempdir.cleanup()

    def observation(self, offering_id="example/model", **changes):
        values = dict(
            provider="openrouter",
            offering_id=offering_id,
            observed_at=OBSERVED,
            source=self.source,
            input_usd_per_million=Decimal("1.25"),
            output_usd_per_million=Decimal("2.5"),
            unsupported_pricing={},
            raw_offering={"id": offering_id},
        )
        values.update(changes)
        return CatalogObservation(**values)

    def write(self, rows=None, **changes):
        values = dict(
            provider="openrouter",
            started_at=STARTED,
            completed_at=COMPLETED,
            source=self.source,
            observations=(self.observation(),) if rows is None else rows,
        )
        values.update(changes)
        return write_snapshot(self.connection, **values)

    def counts(self):
        snapshots = self.connection.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
        observations = self.connection.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
        return snapshots, observations

    def stored_money(self, offering_id, snapshot_id=None):
        sql = (
            "SELECT input_per_million, output_per_million, "
            "typeof(input_per_million), typeof(output_per_million) "
            "FROM observations WHERE offering_id = ?"
        )
        params = [offering_id]
        if snapshot_id is not None:
            sql += " AND snapshot_id = ?"
            params.append(snapshot_id)
        return self.connection.execute(sql, params).fetchone()

    def reopen(self):
        self.connection.close()
        self.connection = open_database(self.path)

    def test_initialization_and_reopening(self):
        self.assertEqual(SCHEMA_VERSION, 1)
        self.assertEqual(self.connection.execute("PRAGMA user_version").fetchone()[0], 1)
        self.assertEqual(self.connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)
        objects = {
            (row["type"], row["name"])
            for row in self.connection.execute(
                "SELECT type, name FROM sqlite_master WHERE name NOT GLOB 'sqlite_*'"
            )
        }
        self.assertEqual(objects, {
            ("table", "snapshots"),
            ("table", "observations"),
            ("index", "snapshots_latest_idx"),
            ("index", "observations_offering_history_idx"),
        })
        snapshot_columns = {
            row[1] for row in self.connection.execute('PRAGMA table_info("snapshots")')
        }
        observation_columns = {
            row[1] for row in self.connection.execute('PRAGMA table_info("observations")')
        }
        self.assertTrue({
            "id", "provider", "started_at", "completed_at",
            "source_url", "source_metadata_json",
        } <= snapshot_columns)
        self.assertTrue({
            "snapshot_id", "offering_id", "observed_at", "input_per_million",
            "output_per_million", "conditions_json", "source_record_json",
        } <= observation_columns)
        first = self.write()
        path_connection = open_database(Path(self.path))
        try:
            again = get_latest_successful_snapshot(path_connection, "openrouter")
            self.assertEqual(again.id, first.id)
        finally:
            path_connection.close()
        self.reopen()
        self.assertEqual(self.connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)
        restored = get_latest_successful_snapshot(self.connection, "openrouter")
        self.assertEqual(restored.id, first.id)
        self.assertEqual(restored.provider, first.provider)
        self.assertEqual(get_snapshot_observations(self.connection, first.id)[0].offering_id, "example/model")

    def test_ownership_and_version_refusal(self):
        cases = []

        versioned = str(Path(self._tempdir.name) / "version2.sqlite")
        raw = sqlite3.connect(versioned)
        raw.execute("PRAGMA user_version = 2")
        raw.close()
        cases.append(("unsupported version", versioned, lambda conn: (
            conn.execute("PRAGMA user_version").fetchone()[0] == 2
            and conn.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE name NOT GLOB 'sqlite_*'"
            ).fetchone()[0] == 0
        )))

        table_path = str(Path(self._tempdir.name) / "unversioned-table.sqlite")
        raw = sqlite3.connect(table_path)
        raw.execute("CREATE TABLE foreign_table (id INTEGER)")
        raw.close()
        cases.append(("unversioned table", table_path, lambda conn: (
            conn.execute("PRAGMA user_version").fetchone()[0] == 0
            and conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'foreign_table'"
            ).fetchone() is not None
            and conn.execute(
                "SELECT name FROM sqlite_master WHERE name = 'snapshots'"
            ).fetchone() is None
        )))

        view_path = str(Path(self._tempdir.name) / "unversioned-view.sqlite")
        raw = sqlite3.connect(view_path)
        raw.execute("CREATE VIEW foreign_view AS SELECT 1 AS id")
        raw.close()
        cases.append(("unversioned view", view_path, lambda conn: (
            conn.execute("PRAGMA user_version").fetchone()[0] == 0
            and conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'view' AND name = 'foreign_view'"
            ).fetchone() is not None
        )))

        missing_path = str(Path(self._tempdir.name) / "v1-missing.sqlite")
        raw = sqlite3.connect(missing_path)
        raw.execute("PRAGMA user_version = 1")
        raw.close()
        cases.append(("version-1 missing tables", missing_path, lambda conn: (
            conn.execute("PRAGMA user_version").fetchone()[0] == 1
            and conn.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE name NOT GLOB 'sqlite_*'"
            ).fetchone()[0] == 0
        )))

        column_path = str(Path(self._tempdir.name) / "v1-columns.sqlite")
        raw = sqlite3.connect(column_path)
        raw.execute("PRAGMA user_version = 1")
        raw.execute("CREATE TABLE snapshots (id INTEGER PRIMARY KEY, provider TEXT)")
        raw.execute("CREATE TABLE observations (snapshot_id INTEGER, offering_id TEXT)")
        raw.close()
        cases.append(("version-1 missing columns", column_path, lambda conn: (
            conn.execute("PRAGMA user_version").fetchone()[0] == 1
            and {row[1] for row in conn.execute('PRAGMA table_info("snapshots")')}
            == {"id", "provider"}
        )))

        view_table_path = str(Path(self._tempdir.name) / "v1-view.sqlite")
        raw = sqlite3.connect(view_table_path)
        raw.execute("PRAGMA user_version = 1")
        raw.execute("CREATE VIEW snapshots AS SELECT 1 AS id")
        raw.execute(
            "CREATE TABLE observations ("
            "snapshot_id INTEGER, offering_id TEXT, observed_at TEXT, "
            "input_per_million TEXT, output_per_million TEXT, "
            "conditions_json TEXT, source_record_json TEXT)"
        )
        raw.close()
        cases.append(("version-1 snapshots view", view_table_path, lambda conn: (
            conn.execute(
                "SELECT type FROM sqlite_master WHERE name = 'snapshots'"
            ).fetchone()[0] == "view"
        )))

        for label, path, unchanged in cases:
            with self.subTest(label=label):
                with self.assertRaises(StorageError):
                    open_database(path)
                probe = sqlite3.connect(path)
                try:
                    self.assertTrue(unchanged(probe))
                finally:
                    probe.close()

    def test_basic_round_trip(self):
        metadata = {"z": 1, "a": {"keep": True}}
        source = SourceMetadata("locator:not-a-url", metadata)
        raw_offering = {"id": "B-model", "name": "B"}
        conditions = {"request": "0.01", "discount": {"batch_only": True}}
        rows = (
            self.observation(
                "B-model", source=source, raw_offering=raw_offering,
                unsupported_pricing=conditions, input_usd_per_million=Decimal("3"),
                output_usd_per_million=None,
            ),
            self.observation(
                "A-model", source=source, raw_offering={"id": "A-model"},
                input_usd_per_million=None, output_usd_per_million=Decimal("4"),
            ),
        )
        record = self.write(rows, source=source)
        self.assertIsInstance(record, SnapshotRecord)
        self.assertEqual(record.provider, "openrouter")
        self.assertEqual(record.source.location, "locator:not-a-url")
        self.assertEqual(record.started_at, STARTED)
        self.assertEqual(record.completed_at, COMPLETED)
        self.assertEqual(record.source.metadata, {"a": {"keep": True}, "z": Decimal(1)})
        stored = get_snapshot_observations(self.connection, record.id)
        self.assertEqual([row.offering_id for row in stored], ["A-model", "B-model"])
        self.assertIsInstance(stored, tuple)
        self.assertIsInstance(stored[0], ObservationRecord)
        self.assertIsNone(stored[0].input_per_million)
        self.assertEqual(stored[0].output_per_million, Decimal("4"))
        self.assertEqual(stored[1].input_per_million, Decimal("3"))
        self.assertIsNone(stored[1].output_per_million)
        self.assertEqual(stored[1].conditions, {"request": "0.01", "discount": {"batch_only": True}})
        self.assertEqual(stored[1].source_record["name"], "B")
        metadata["a"]["keep"] = False
        raw_offering["name"] = "mutated"
        conditions["request"] = "changed"
        record.source.metadata["z"] = "mutated-record"
        stored[1].conditions["request"] = "mutated-row"
        stored[1].source_record["name"] = "mutated-row"
        reread = get_snapshot_observations(self.connection, record.id)
        self.assertEqual(reread[1].conditions["request"], "0.01")
        self.assertEqual(reread[1].source_record["name"], "B")
        self.assertEqual(get_latest_successful_snapshot(self.connection, "openrouter").source.metadata["a"], {"keep": True})
        self.reopen()
        restored = get_latest_successful_snapshot(self.connection, "openrouter")
        self.assertEqual(restored.id, record.id)
        self.assertEqual(restored.source.location, "locator:not-a-url")
        self.assertEqual(
            [row.offering_id for row in get_snapshot_observations(self.connection, restored.id)],
            ["A-model", "B-model"],
        )

    def test_latest_chronology_and_provider_isolation(self):
        self.assertIsNone(get_latest_successful_snapshot(self.connection, "openrouter"))
        self.assertEqual(get_current_observations(self.connection, "openrouter"), ())
        self.assertEqual(get_snapshot_observations(self.connection, 1), ())
        self.assertEqual(
            get_offering_history(self.connection, provider="openrouter", offering_id="example/model"),
            (),
        )
        first = self.write((self.observation("one"),))
        newer = self.write(
            (self.observation("two", observed_at=datetime(2026, 9, 19, 13, 30, tzinfo=timezone.utc)),),
            started_at=COMPLETED, completed_at=LATER,
        )
        self.assertEqual(get_latest_successful_snapshot(self.connection, "openrouter").id, newer.id)
        self.assertEqual(
            [row.offering_id for row in get_current_observations(self.connection, "openrouter")],
            ["two"],
        )
        backfill = self.write(
            (self.observation("backfill", observed_at=datetime(2026, 9, 19, 11, 30, tzinfo=timezone.utc)),),
            started_at=EARLIER, completed_at=STARTED,
        )
        self.assertEqual(get_latest_successful_snapshot(self.connection, "openrouter").id, newer.id)
        self.assertNotEqual(backfill.id, newer.id)
        tied_source = SourceMetadata("fixture:openrouter", {"capture": "tied"})
        tied = write_snapshot(
            self.connection,
            provider="openrouter",
            started_at=STARTED,
            completed_at=LATER,
            source=tied_source,
            observations=(self.observation("tied", source=tied_source, observed_at=OBSERVED),),
        )
        self.assertEqual(get_latest_successful_snapshot(self.connection, "openrouter").id, tied.id)
        other_source = SourceMetadata("other", {})
        other = write_snapshot(
            self.connection,
            provider="OtherProvider",
            started_at=LATER,
            completed_at=LATER,
            source=other_source,
            observations=(
                self.observation(
                    "two", provider="OtherProvider", source=other_source, observed_at=LATER,
                ),
            ),
        )
        self.assertEqual(get_latest_successful_snapshot(self.connection, "openrouter").id, tied.id)
        self.assertEqual(get_latest_successful_snapshot(self.connection, "OtherProvider").id, other.id)
        self.assertEqual(get_current_observations(self.connection, "missing"), ())
        self.assertIsNone(get_latest_successful_snapshot(self.connection, "OpenRouter"))
        self.assertEqual(first.id < newer.id < tied.id, True)

    def test_current_replacement_and_history(self):
        first = self.write((
            self.observation("A", raw_offering={"id": "A"}, input_usd_per_million=Decimal("1")),
            self.observation("B", raw_offering={"id": "B"}, input_usd_per_million=Decimal("2")),
        ))
        self.assertEqual(
            [row.offering_id for row in get_current_observations(self.connection, "openrouter")],
            ["A", "B"],
        )
        second = self.write(
            (self.observation("A", raw_offering={"id": "A"}, input_usd_per_million=Decimal("1"), observed_at=LATER),),
            started_at=COMPLETED, completed_at=LATER,
        )
        current = get_current_observations(self.connection, "openrouter")
        self.assertEqual([row.offering_id for row in current], ["A"])
        self.assertEqual(current[0].snapshot_id, second.id)
        history_b = get_offering_history(self.connection, provider="openrouter", offering_id="B")
        self.assertEqual([row.snapshot_id for row in history_b], [first.id])
        history_a = get_offering_history(self.connection, provider="openrouter", offering_id="A")
        self.assertEqual([row.snapshot_id for row in history_a], [first.id, second.id])
        self.assertEqual(history_a[0].input_per_million, Decimal("1"))
        self.assertEqual(history_a[1].input_per_million, Decimal("1"))
        mixed_case = SourceMetadata("fixture:OpenRouter", {})
        write_snapshot(
            self.connection,
            provider="OpenRouter",
            started_at=STARTED,
            completed_at=COMPLETED,
            source=mixed_case,
            observations=(
                self.observation("A", provider="OpenRouter", source=mixed_case),
                self.observation("a", provider="OpenRouter", source=mixed_case, raw_offering={"id": "a"}),
            ),
        )
        self.assertEqual(
            [row.offering_id for row in get_current_observations(self.connection, "openrouter")],
            ["A"],
        )
        self.assertEqual(
            [row.offering_id for row in get_current_observations(self.connection, "OpenRouter")],
            ["A", "a"],
        )
        self.assertEqual(
            get_offering_history(self.connection, provider="openrouter", offering_id="a"),
            (),
        )
        self.assertEqual(
            [row.offering_id for row in get_offering_history(
                self.connection, provider="OpenRouter", offering_id="A",
            )],
            ["A"],
        )

    def test_valid_empty_catalog(self):
        nonempty = self.write((self.observation("keep"), self.observation("drop"),))
        accepted_empty = parse_catalog(
            '{"data":[]}', observed_at=COMPLETED, source=self.source,
        )
        self.assertTrue(accepted_empty.accepted)
        self.assertEqual(accepted_empty.observations, ())
        empty = self.write(
            accepted_empty.observations,
            started_at=COMPLETED, completed_at=LATER,
        )
        latest = get_latest_successful_snapshot(self.connection, "openrouter")
        self.assertEqual(latest.id, empty.id)
        self.assertEqual(get_current_observations(self.connection, "openrouter"), ())
        self.assertEqual(get_snapshot_observations(self.connection, empty.id), ())
        self.assertEqual(
            [row.snapshot_id for row in get_offering_history(
                self.connection, provider="openrouter", offering_id="keep",
            )],
            [nonempty.id],
        )
        rejected = parse_catalog("{}", observed_at=LATER, source=self.source)
        self.assertFalse(rejected.accepted)
        self.assertEqual(rejected.observations, ())
        # Rejected ParseResult.observations is also empty. Callers must check
        # result.accepted before write_snapshot; that empty tuple is not an
        # accepted catalog and is not submitted here.
        self.assertNotEqual(rejected.accepted, accepted_empty.accepted)
        self.assertEqual(get_latest_successful_snapshot(self.connection, "openrouter").id, empty.id)

    def test_boundary_rejection(self):
        previous = self.write()
        previous_counts = self.counts()
        current_id = get_latest_successful_snapshot(self.connection, "openrouter").id
        cases = [
            ("int money", TypeError, dict(observations=(self.observation(input_usd_per_million=1),))),
            ("float money", TypeError, dict(observations=(self.observation(input_usd_per_million=1.0),))),
            ("bool money", TypeError, dict(observations=(self.observation(input_usd_per_million=True),))),
            ("negative money", ValueError, dict(observations=(self.observation(input_usd_per_million=Decimal("-0.01")),))),
            ("nan money", ValueError, dict(observations=(self.observation(input_usd_per_million=Decimal("NaN")),))),
            ("inf money", ValueError, dict(observations=(self.observation(output_usd_per_million=Decimal("Infinity")),))),
            ("blank provider", ValueError, dict(provider="  ")),
            ("blank offering", ValueError, dict(observations=(self.observation("   "),))),
            ("surrogate provider", ValueError, dict(provider="open\ud800router")),
            ("nul offering", ValueError, dict(observations=(self.observation("id\x00x"),))),
            ("provider type", TypeError, dict(provider=1)),
            ("source type", TypeError, dict(source="fixture:openrouter")),
            ("naive timestamp", ValueError, dict(started_at=datetime(2026, 9, 19, 12, 0))),
            ("observations type", TypeError, dict(observations=None)),
            ("row type", TypeError, dict(observations=("example/model",))),
            (
                "empty location",
                ValueError,
                dict(
                    source=SourceMetadata("", {}),
                    observations=(self.observation(source=SourceMetadata("", {})),),
                ),
            ),
            ("provider mismatch", ValueError, dict(observations=(self.observation(provider="other"),))),
            (
                "source mismatch",
                ValueError,
                dict(observations=(self.observation(source=SourceMetadata("other", {"capture": "test"})),)),
            ),
            (
                "metadata mismatch",
                ValueError,
                dict(observations=(self.observation(source=SourceMetadata("fixture:openrouter", {})),)),
            ),
            (
                "reversed interval",
                ValueError,
                dict(started_at=COMPLETED, completed_at=STARTED),
            ),
            (
                "observation before interval",
                ValueError,
                dict(observations=(self.observation(observed_at=EARLIER),)),
            ),
            (
                "observation after interval",
                ValueError,
                dict(observations=(self.observation(observed_at=LATER),)),
            ),
            (
                "distinct observation times",
                ValueError,
                dict(observations=(
                    self.observation("one", observed_at=STARTED),
                    self.observation("two", observed_at=COMPLETED),
                )),
            ),
            (
                "float evidence",
                TypeError,
                dict(
                    source=SourceMetadata("fixture:openrouter", {"n": 1.5}),
                    observations=(self.observation(
                        source=SourceMetadata("fixture:openrouter", {"n": 1.5}),
                    ),),
                ),
            ),
        ]
        for label, error, changes in cases:
            with self.subTest(label=label):
                with self.assertRaises(error):
                    self.write(**changes)
                self.assertEqual(self.counts(), previous_counts)
                self.assertEqual(
                    get_latest_successful_snapshot(self.connection, "openrouter").id,
                    current_id,
                )
                self.assertEqual(current_id, previous.id)

    def test_genuine_statement_rollback(self):
        previous = self.write((self.observation("keep"),))
        previous_counts = self.counts()
        with self.assertRaises(sqlite3.IntegrityError):
            self.write((
                self.observation("A", raw_offering={"id": "A"}),
                self.observation("B", raw_offering={"id": "B"}),
                self.observation("A", raw_offering={"id": "A-dup"}),
            ))
        self.assertEqual(self.counts(), previous_counts)
        self.assertEqual(get_latest_successful_snapshot(self.connection, "openrouter").id, previous.id)
        self.assertEqual(
            [row.offering_id for row in get_current_observations(self.connection, "openrouter")],
            ["keep"],
        )
        self.assertFalse(self.connection.in_transaction)

    def test_genuine_commit_failure(self):
        previous = self.write((self.observation("keep"),))
        previous_counts = self.counts()
        self.connection.execute(
            """
            CREATE TRIGGER sabotage_commit
            AFTER INSERT ON snapshots
            BEGIN
                INSERT INTO observations (
                    snapshot_id, offering_id, observed_at,
                    input_per_million, output_per_million,
                    conditions_json, source_record_json
                ) VALUES (
                    -1, 'sabotage', NEW.started_at, NULL, NULL, '{}', '{}'
                );
            END
            """
        )
        self.connection.execute("PRAGMA defer_foreign_keys = ON")
        with self.assertRaises(sqlite3.IntegrityError):
            self.write(
                (self.observation("new", observed_at=LATER),),
                started_at=COMPLETED, completed_at=LATER,
            )
        self.assertFalse(self.connection.in_transaction)
        self.assertEqual(self.counts(), previous_counts)
        self.assertEqual(get_latest_successful_snapshot(self.connection, "openrouter").id, previous.id)
        self.assertEqual(
            [row.offering_id for row in get_current_observations(self.connection, "openrouter")],
            ["keep"],
        )
        self.assertIsNone(self.connection.execute(
            "SELECT 1 FROM observations WHERE offering_id = 'sabotage'"
        ).fetchone())

    def test_foreign_keys_and_transaction_ownership(self):
        previous = self.write()
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(
                "INSERT INTO observations ("
                "snapshot_id, offering_id, observed_at, input_per_million, "
                "output_per_million, conditions_json, source_record_json"
                ") VALUES (999, 'orphan', '2026-09-19T12:00:00.000000Z', NULL, NULL, '{}', '{}')"
            )
        self.connection.execute("PRAGMA foreign_keys = OFF")
        self.assertEqual(self.connection.execute("PRAGMA foreign_keys").fetchone()[0], 0)
        with self.assertRaises(StorageError):
            self.write((self.observation("disabled"),))
        self.assertEqual(self.connection.execute("PRAGMA foreign_keys").fetchone()[0], 0)
        self.assertEqual(get_latest_successful_snapshot(self.connection, "openrouter").id, previous.id)
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("BEGIN IMMEDIATE")
        self.connection.execute(
            "INSERT INTO snapshots ("
            "provider, started_at, completed_at, source_url, source_metadata_json"
            ") VALUES ('caller', '2026-09-19T12:00:00.000000Z', "
            "'2026-09-19T12:00:00.000000Z', 'caller', '{}')"
        )
        self.assertTrue(self.connection.in_transaction)
        with self.assertRaises(RuntimeError):
            self.write((self.observation("owned"),))
        self.assertTrue(self.connection.in_transaction)
        self.assertEqual(
            self.connection.execute(
                "SELECT provider FROM snapshots WHERE provider = 'caller'"
            ).fetchone()[0],
            "caller",
        )
        self.connection.rollback()
        self.assertFalse(self.connection.in_transaction)
        self.assertIsNone(self.connection.execute(
            "SELECT 1 FROM snapshots WHERE provider = 'caller'"
        ).fetchone())
        self.assertEqual(get_latest_successful_snapshot(self.connection, "openrouter").id, previous.id)

    def test_decimal_representation(self):
        long_value = Decimal("1.2345678901234567890123456789")
        extreme_high = Decimal((0, (1,), 1000005))
        extreme_low = Decimal((0, (1,), -999993))
        cases = [
            ("one", Decimal("1.00"), "1e0"),
            ("one-alt", Decimal("1.0"), "1e0"),
            ("one-int", Decimal("1"), "1e0"),
            ("frac", Decimal("1.2300"), "1.23e0"),
            ("hundred", Decimal("123"), "1.23e2"),
            ("milli", Decimal("0.0010"), "1e-3"),
            ("signed-zero", Decimal("-0.00"), "0"),
            ("zero", Decimal("0"), "0"),
            ("ten", Decimal("10"), "1e1"),
            ("long", long_value, "1.2345678901234567890123456789e0"),
            ("high", extreme_high, "1e1000005"),
            ("low", extreme_low, "1e-999993"),
            ("missing", None, None),
        ]
        rows = []
        for offering_id, amount, _stored in cases:
            rows.append(self.observation(
                offering_id,
                input_usd_per_million=amount,
                output_usd_per_million=Decimal("0") if amount is not None else None,
            ))
        record = self.write(tuple(rows))
        for offering_id, amount, stored in cases:
            with self.subTest(offering_id=offering_id):
                row = self.stored_money(offering_id, record.id)
                self.assertEqual(row[0], stored)
                if stored is None:
                    self.assertEqual(row[2], "null")
                    self.assertIsNone(row[1])
                    self.assertEqual(row[3], "null")
                else:
                    self.assertEqual(row[2], "text")
                    self.assertEqual(row[1], "0")
                    self.assertEqual(row[3], "text")
        decoded = {
            row.offering_id: row.input_per_million
            for row in get_snapshot_observations(self.connection, record.id)
        }
        self.assertEqual(decoded["one"], Decimal("1"))
        self.assertEqual(decoded["one-alt"], decoded["one"])
        self.assertEqual(decoded["frac"], Decimal("1.23"))
        self.assertEqual(decoded["signed-zero"], Decimal("0"))
        self.assertEqual(decoded["zero"], Decimal("0"))
        self.assertIsNone(decoded["missing"])
        self.assertNotEqual(decoded["missing"], decoded["zero"])
        self.assertEqual(decoded["long"], long_value)
        self.assertEqual(decoded["high"], extreme_high)
        self.assertEqual(decoded["low"], extreme_low)

    def test_decimal_context_independence(self):
        extreme = Decimal((0, (1,), 1000005))
        record = self.write((
            self.observation("ctx", input_usd_per_million=Decimal("1.2300")),
            self.observation("extreme", input_usd_per_million=extreme),
        ))
        for trapped in (True, False):
            with self.subTest(trapped=trapped), localcontext() as context:
                context.prec = 2
                context.Emax = 8
                context.Emin = -8
                context.rounding = ROUND_DOWN
                context.capitals = 1
                context.clamp = 1
                for signal in context.traps:
                    context.traps[signal] = trapped
                context.clear_flags()
                context.flags[InvalidOperation] = True
                context.flags[Overflow] = True
                before = _context_snapshot(context)
                hostile_source = SourceMetadata("fixture:openrouter", {"n": Decimal("12300")})
                written = self.write(
                    (self.observation(
                        "hostile",
                        input_usd_per_million=Decimal("1.25"),
                        source=hostile_source,
                        observed_at=LATER,
                    ),),
                    source=hostile_source,
                    started_at=COMPLETED,
                    completed_at=LATER,
                )
                rows = get_snapshot_observations(self.connection, written.id)
                self.assertEqual(rows[0].input_per_million, Decimal("1.25"))
                self.assertEqual(written.source.metadata["n"], Decimal("12300"))
                extreme_rows = get_snapshot_observations(self.connection, record.id)
                self.assertEqual(extreme_rows[1].input_per_million, extreme)
                self.assertEqual(self.stored_money("ctx", record.id)[0], "1.23e0")
                self.assertEqual(_context_snapshot(context), before)
                with self.assertRaises(ValueError):
                    self.write((self.observation(input_usd_per_million=Decimal("-1")),))
                self.assertEqual(_context_snapshot(context), before)
                self.connection.execute(
                    "UPDATE observations SET input_per_million = 'not-money' "
                    "WHERE snapshot_id = ?",
                    (written.id,),
                )
                with self.assertRaises(StorageError):
                    get_snapshot_observations(self.connection, written.id)
                self.assertEqual(_context_snapshot(context), before)
                self.connection.execute(
                    "UPDATE observations SET input_per_million = '1.25e0' "
                    "WHERE snapshot_id = ?",
                    (written.id,),
                )

    def test_json_fidelity_and_determinism(self):
        metadata = {
            "b": True,
            "a": False,
            "z": None,
            "n": Decimal("-12.30"),
            "i": 123,
            "arr": [2, 1],
            "u": "é",
            "s": "\ud800",
            "big": 10 ** 50,
        }
        equivalent = {"same": Decimal("123"), "also": 123, "zero": Decimal("-0"), "zero_int": 0}
        source = SourceMetadata("fixture:openrouter", metadata)
        record = self.write(
            (self.observation(
                source=source,
                unsupported_pricing=equivalent,
                raw_offering={"id": "example/model", "note": "café"},
            ),),
            source=source,
        )
        stored_meta = self.connection.execute(
            "SELECT source_metadata_json FROM snapshots WHERE id = ?",
            (record.id,),
        ).fetchone()[0]
        self.assertEqual(
            stored_meta,
            '{"a":false,"arr":[2e0,1e0],"b":true,"big":1e50,'
            '"i":1.23e2,"n":-1.23e1,"s":"\\ud800","u":"\\u00e9","z":null}',
        )
        stored_conditions = self.connection.execute(
            "SELECT conditions_json FROM observations WHERE snapshot_id = ?",
            (record.id,),
        ).fetchone()[0]
        self.assertEqual(
            stored_conditions,
            '{"also":1.23e2,"same":1.23e2,"zero":0,"zero_int":0}',
        )
        self.reopen()
        restored = get_latest_successful_snapshot(self.connection, "openrouter")
        self.assertIs(restored.source.metadata["a"], False)
        self.assertIs(restored.source.metadata["b"], True)
        self.assertIsNone(restored.source.metadata["z"])
        self.assertEqual(restored.source.metadata["arr"], [Decimal(2), Decimal(1)])
        self.assertEqual(restored.source.metadata["i"], Decimal(123))
        self.assertEqual(restored.source.metadata["n"], Decimal("-12.3"))
        self.assertEqual(restored.source.metadata["u"], "é")
        self.assertEqual(restored.source.metadata["s"], "\ud800")
        self.assertEqual(restored.source.metadata["big"], Decimal(10 ** 50))
        again = self.connection.execute(
            "SELECT source_metadata_json FROM snapshots WHERE id = ?",
            (restored.id,),
        ).fetchone()[0]
        self.assertEqual(again, stored_meta)
        rewritten = write_snapshot(
            self.connection,
            provider="openrouter",
            started_at=COMPLETED,
            completed_at=LATER,
            source=restored.source,
            observations=(self.observation(
                source=restored.source,
                observed_at=LATER,
                unsupported_pricing=get_snapshot_observations(self.connection, restored.id)[0].conditions,
                raw_offering=get_snapshot_observations(self.connection, restored.id)[0].source_record,
            ),),
        )
        rewritten_meta = self.connection.execute(
            "SELECT source_metadata_json, conditions_json FROM snapshots "
            "JOIN observations ON observations.snapshot_id = snapshots.id "
            "WHERE snapshots.id = ?",
            (rewritten.id,),
        ).fetchone()
        self.assertEqual(rewritten_meta[0], stored_meta)
        self.assertEqual(rewritten_meta[1], stored_conditions)

    def test_evidence_rejection_and_opacity(self):
        previous = self.write()
        previous_counts = self.counts()
        shared = {"n": 1}
        opaque = {
            "request": "0.01",
            "image": "0.02",
            "input_cache_read": "0.0000002",
            "discount": {"batch_only": True, "shared": shared},
        }
        cycle = {}
        cycle["self"] = cycle
        accepted = self.write(
            (self.observation(
                "opaque",
                unsupported_pricing=opaque,
                raw_offering={"id": "opaque", "pricing": opaque, "also": shared},
                observed_at=LATER,
            ),),
            started_at=COMPLETED,
            completed_at=LATER,
        )
        row = get_snapshot_observations(self.connection, accepted.id)[0]
        self.assertEqual(row.conditions["request"], "0.01")
        self.assertEqual(row.conditions["image"], "0.02")
        self.assertEqual(row.conditions["input_cache_read"], "0.0000002")
        self.assertEqual(row.conditions["discount"]["batch_only"], True)
        self.assertNotIn("request_price", row.__dict__)
        self.assertFalse(hasattr(row, "pricing_kind"))
        failures = [
            ("tuple", TypeError, {"note": (1, 2)}),
            ("set", TypeError, {"note": {1}}),
            ("bytes", TypeError, {"note": b"x"}),
            ("float", TypeError, {"note": 1.25}),
            ("object", TypeError, {"note": object()}),
            ("nonstring key", TypeError, {1: "x"}),
            ("nonfinite", ValueError, {"note": Decimal("NaN")}),
            ("cycle", ValueError, cycle),
            ("list root", TypeError, ["not", "an", "object"]),
        ]
        for label, error, payload in failures:
            with self.subTest(label=label):
                if isinstance(payload, list):
                    source = SourceMetadata("fixture:openrouter", payload)
                    changes = dict(source=source, observations=(self.observation(source=source),))
                else:
                    changes = dict(observations=(self.observation(unsupported_pricing=payload),))
                with self.assertRaises(error):
                    self.write(**changes)
                self.assertEqual(
                    get_latest_successful_snapshot(self.connection, "openrouter").id,
                    accepted.id,
                )
        self.assertGreater(self.counts()[0], previous_counts[0])

    def test_timestamp_contract(self):
        offset = timezone(timedelta(hours=-4))
        started = datetime(2026, 9, 19, 8, 0, 0, 123456, tzinfo=offset)
        completed = datetime(2026, 9, 19, 14, 0, tzinfo=timezone.utc)
        observed = datetime(2026, 9, 19, 12, 30, 0, 123456, tzinfo=timezone.utc)
        early_year = datetime(99, 1, 2, 3, 4, 5, 6, tzinfo=timezone.utc)
        record = self.write(
            (self.observation(observed_at=observed),),
            started_at=started,
            completed_at=completed,
        )
        stored = self.connection.execute(
            "SELECT started_at, completed_at, observed_at FROM snapshots "
            "JOIN observations ON observations.snapshot_id = snapshots.id "
            "WHERE snapshots.id = ?",
            (record.id,),
        ).fetchone()
        self.assertEqual(stored[0], "2026-09-19T12:00:00.123456Z")
        self.assertEqual(stored[1], "2026-09-19T14:00:00.000000Z")
        self.assertEqual(stored[2], "2026-09-19T12:30:00.123456Z")
        self.assertEqual(record.started_at, datetime(2026, 9, 19, 12, 0, 0, 123456, tzinfo=timezone.utc))
        archive = SourceMetadata("archive", {})
        year = self.write(
            (self.observation(provider="archive", source=archive, observed_at=early_year),),
            provider="archive",
            started_at=early_year,
            completed_at=early_year,
            source=archive,
        )
        year_text = self.connection.execute(
            "SELECT started_at FROM snapshots WHERE id = ?", (year.id,),
        ).fetchone()[0]
        self.assertEqual(year_text, "0099-01-02T03:04:05.000006Z")
        self.assertTrue(year_text.startswith("0099-"))
        inclusive = self.write(
            (self.observation("edge-start", observed_at=STARTED), self.observation("edge-end", observed_at=STARTED)),
        )
        self.assertEqual(len(get_snapshot_observations(self.connection, inclusive.id)), 2)
        with self.assertRaises(ValueError):
            self.write(started_at=datetime(2026, 9, 19, 12, 0))
        with self.assertRaises(ValueError):
            self.write(completed_at=datetime(2026, 9, 19, 13, 0))
        with self.assertRaises(ValueError):
            self.write(started_at=COMPLETED, completed_at=STARTED)
        with self.assertRaises(ValueError):
            self.write((self.observation(observed_at=LATER),))
        with self.assertRaises(ValueError):
            self.write((
                self.observation("one", observed_at=STARTED),
                self.observation("two", observed_at=COMPLETED),
            ))

    def test_malformed_stored_data(self):
        other_source = SourceMetadata("other", {"ok": True})
        earlier = write_snapshot(
            self.connection,
            provider="other",
            started_at=EARLIER,
            completed_at=STARTED,
            source=other_source,
            observations=(self.observation(
                "old", provider="other", source=other_source, observed_at=EARLIER,
            ),),
        )
        corruptions = [
            ("noncanonical money", "observation", "UPDATE observations SET input_per_million = '1.0e0' WHERE snapshot_id = ?", None),
            ("malformed money", "observation", "UPDATE observations SET input_per_million = 'not-money' WHERE snapshot_id = ?", None),
            ("blob money", "observation", "UPDATE observations SET input_per_million = ? WHERE snapshot_id = ?", b"\x00"),
            ("duplicate json keys", "snapshot", "UPDATE snapshots SET source_metadata_json = '{\"a\":1e0,\"a\":2e0}' WHERE id = ?", None),
            ("nonjson constant", "snapshot", "UPDATE snapshots SET source_metadata_json = '{\"a\":NaN}' WHERE id = ?", None),
            ("wrong json root", "snapshot", "UPDATE snapshots SET source_metadata_json = '[]' WHERE id = ?", None),
            ("noncanonical json", "snapshot", "UPDATE snapshots SET source_metadata_json = '{\"a\": 1}' WHERE id = ?", None),
            ("malformed timestamp", "snapshot", "UPDATE snapshots SET started_at = '2026-09-19T12:00:00Z' WHERE id = ?", None),
            ("calendar timestamp", "snapshot", "UPDATE snapshots SET started_at = '2026-02-30T12:00:00.000000Z' WHERE id = ?", None),
        ]
        for label, kind, sql, extra in corruptions:
            with self.subTest(label=label):
                good = self.write((self.observation("good"),))
                params = (good.id,) if extra is None else (extra, good.id)
                self.connection.execute(sql, params)
                with self.assertRaises(StorageError):
                    if kind == "snapshot":
                        get_latest_successful_snapshot(self.connection, "openrouter")
                    else:
                        get_current_observations(self.connection, "openrouter")
                other = get_latest_successful_snapshot(self.connection, "other")
                self.assertEqual(other.id, earlier.id)
                self.assertEqual(other.source.metadata, {"ok": True})
                self.assertNotEqual(
                    get_offering_history(self.connection, provider="other", offering_id="old")[0].input_per_million,
                    Decimal("0"),
                )
        reversed_path = str(Path(self._tempdir.name) / "reversed-interval.sqlite")
        raw = sqlite3.connect(reversed_path)
        raw.execute("PRAGMA user_version = 1")
        raw.execute(
            "CREATE TABLE snapshots ("
            "id INTEGER PRIMARY KEY, provider TEXT NOT NULL, started_at TEXT NOT NULL, "
            "completed_at TEXT NOT NULL, source_url TEXT NOT NULL, source_metadata_json TEXT NOT NULL)"
        )
        raw.execute(
            "CREATE TABLE observations ("
            "snapshot_id INTEGER NOT NULL, offering_id TEXT NOT NULL, observed_at TEXT NOT NULL, "
            "input_per_million TEXT, output_per_million TEXT, conditions_json TEXT NOT NULL, "
            "source_record_json TEXT NOT NULL, PRIMARY KEY (snapshot_id, offering_id))"
        )
        raw.execute(
            "INSERT INTO snapshots VALUES ("
            "1, 'openrouter', '2026-09-19T13:00:00.000000Z', '2026-09-19T12:00:00.000000Z', 'x', '{}')"
        )
        raw.commit()
        raw.close()
        reversed_connection = open_database(reversed_path)
        try:
            with self.assertRaises(StorageError):
                get_latest_successful_snapshot(reversed_connection, "openrouter")
        finally:
            reversed_connection.close()

    def test_json_evidence_depth_bound(self):
        self.assertEqual(MAX_JSON_DEPTH, 64)
        previous = self.write((self.observation("keep"),))
        previous_counts = self.counts()
        fields = {
            "metadata": lambda payload: dict(
                source=SourceMetadata("fixture:openrouter", payload),
                observations=(self.observation(
                    source=SourceMetadata("fixture:openrouter", payload),
                    raw_offering={"id": "example/model"},
                ),),
            ),
            "unsupported_pricing": lambda payload: dict(
                observations=(self.observation(unsupported_pricing=payload),),
            ),
            "raw_offering": lambda payload: dict(
                observations=(self.observation(raw_offering=payload),),
            ),
        }
        nestings = {
            "dictionary": _nested_dict,
            "list": _nested_list,
            "mixed": _nested_mixed,
        }
        for field, changes_for in fields.items():
            for nesting, builder in nestings.items():
                with self.subTest(field=field, nesting=nesting, depth=64):
                    payload = builder(64)
                    record = self.write(**changes_for(payload))
                    latest = get_latest_successful_snapshot(self.connection, "openrouter")
                    self.assertEqual(latest.id, record.id)
                    if field == "metadata":
                        self.assertEqual(latest.source.metadata, payload)
                    else:
                        row = get_snapshot_observations(self.connection, record.id)[0]
                        actual = row.conditions if field == "unsupported_pricing" else row.source_record
                        self.assertEqual(actual, payload)
                with self.subTest(field=field, nesting=nesting, depth=65):
                    before = self.counts()
                    latest_id = get_latest_successful_snapshot(self.connection, "openrouter").id
                    with _deny_sql_begin(self.connection), self.assertRaises(ValueError):
                        self.write(**changes_for(builder(65)))
                    self.assertFalse(self.connection.in_transaction)
                    self.assertEqual(self.counts(), before)
                    self.assertEqual(
                        get_latest_successful_snapshot(self.connection, "openrouter").id,
                        latest_id,
                    )
        deep_source = SourceMetadata(
            "fixture:openrouter",
            _nested_dict(64),
        )
        deep_record = write_snapshot(
            self.connection,
            provider="openrouter",
            started_at=STARTED,
            completed_at=COMPLETED,
            source=deep_source,
            observations=(self.observation(
                "deep-all",
                source=deep_source,
                unsupported_pricing=_nested_list(64),
                raw_offering=_nested_mixed(64),
            ),),
        )
        restored = _call_with_frames(
            get_latest_successful_snapshot, EXTRA_READ_FRAMES, self.connection, "openrouter",
        )
        self.assertEqual(restored.id, deep_record.id)
        self.assertEqual(restored.source.metadata, _nested_dict(64))
        deep_rows = _call_with_frames(
            get_snapshot_observations, EXTRA_READ_FRAMES, self.connection, deep_record.id,
        )
        self.assertEqual(deep_rows[0].conditions, _nested_list(64))
        self.assertEqual(deep_rows[0].source_record, _nested_mixed(64))
        self.connection.execute(
            "UPDATE snapshots SET source_metadata_json = ? WHERE id = ?",
            (_nested_object_json(65), deep_record.id),
        )
        with self.assertRaises(StorageError):
            _call_with_frames(
                get_latest_successful_snapshot, EXTRA_READ_FRAMES, self.connection, "openrouter",
            )
        self.connection.execute(
            "UPDATE observations SET conditions_json = ? WHERE snapshot_id = ?",
            (_nested_object_json(65), deep_record.id),
        )
        with self.assertRaises(StorageError):
            _call_with_frames(
                get_snapshot_observations, EXTRA_READ_FRAMES, self.connection, deep_record.id,
            )
        quoted = {"s": "[]{}[]" + "[" * 80 + "{" * 80 + "}" * 80 + "]" * 80}
        quoted_source = SourceMetadata("fixture:openrouter", quoted)
        quoted_record = self.write(
            (self.observation(source=quoted_source, raw_offering={"id": "example/model", "note": "[{]}"}),),
            source=quoted_source,
        )
        quoted_latest = get_latest_successful_snapshot(self.connection, "openrouter")
        self.assertEqual(quoted_latest.id, quoted_record.id)
        self.assertEqual(quoted_latest.source.metadata["s"], quoted["s"])
        self.connection.execute(
            "UPDATE snapshots SET source_metadata_json = ? WHERE id = ?",
            ('{"s":"' + "[" * 80 + "{" * 80 + "}" * 80 + "]" * 80 + '"}', quoted_record.id),
        )
        quoted_stored = get_latest_successful_snapshot(self.connection, "openrouter")
        self.assertEqual(quoted_stored.id, quoted_record.id)
        self.assertEqual(
            quoted_stored.source.metadata["s"],
            "[" * 80 + "{" * 80 + "}" * 80 + "]" * 80,
        )
        self.connection.execute(
            "UPDATE snapshots SET source_metadata_json = '{' WHERE id = ?",
            (quoted_record.id,),
        )
        with self.assertRaises(StorageError):
            get_latest_successful_snapshot(self.connection, "openrouter")
        self.assertEqual(
            get_offering_history(self.connection, provider="openrouter", offering_id="keep")[0].snapshot_id,
            previous.id,
        )
        self.assertGreaterEqual(self.counts()[0], previous_counts[0])

    def test_reversed_interval_empty_observations(self):
        previous = self.write()
        previous_counts = self.counts()
        with _deny_sql_begin(self.connection), self.assertRaises(ValueError):
            self.write((), started_at=COMPLETED, completed_at=STARTED)
        self.assertFalse(self.connection.in_transaction)
        self.assertEqual(self.counts(), previous_counts)
        self.assertEqual(get_latest_successful_snapshot(self.connection, "openrouter").id, previous.id)

    def test_stored_negative_money(self):
        record = self.write((self.observation("neg"),))
        for column in ("input_per_million", "output_per_million"):
            with self.subTest(column=column):
                self.connection.execute(
                    f"UPDATE observations SET {column} = '-1e0' WHERE snapshot_id = ?",
                    (record.id,),
                )
                with self.assertRaises(StorageError):
                    get_snapshot_observations(self.connection, record.id)
                with self.assertRaises(StorageError):
                    get_current_observations(self.connection, "openrouter")
                self.connection.execute(
                    f"UPDATE observations SET {column} = NULL WHERE snapshot_id = ?",
                    (record.id,),
                )

    def test_source_consistency_canonical_json(self):
        self.assertEqual(
            {"flag": True, "n": [1, 0]},
            {"flag": 1, "n": [True, False]},
        )
        previous = self.write()
        previous_counts = self.counts()
        snapshot_source = SourceMetadata("fixture:openrouter", {"flag": True, "n": [1, 0]})
        observation_source = SourceMetadata("fixture:openrouter", {"flag": 1, "n": [True, False]})
        with _deny_sql_begin(self.connection), self.assertRaises(ValueError):
            self.write(
                (self.observation(source=observation_source),),
                source=snapshot_source,
            )
        self.assertFalse(self.connection.in_transaction)
        self.assertEqual(self.counts(), previous_counts)
        self.assertEqual(get_latest_successful_snapshot(self.connection, "openrouter").id, previous.id)
        reordered = SourceMetadata("fixture:openrouter", {"b": 2, "a": 1})
        self.write(
            (self.observation(source=SourceMetadata("fixture:openrouter", {"a": 1, "b": 2})),),
            source=reordered,
        )
        numeric = SourceMetadata("fixture:openrouter", {"n": Decimal("123")})
        accepted = self.write(
            (self.observation(source=SourceMetadata("fixture:openrouter", {"n": 123})),),
            source=numeric,
        )
        self.assertEqual(get_latest_successful_snapshot(self.connection, "openrouter").id, accepted.id)
        self.assertEqual(accepted.source.metadata["n"], Decimal(123))

    def test_initialization_race_accepts_later_version_one(self):
        path = str(Path(self._tempdir.name) / "race.sqlite")
        real_user_version = storage._user_version
        calls = {"count": 0}

        def patched(connection):
            calls["count"] += 1
            if calls["count"] == 1:
                with patch.object(storage, "_user_version", real_user_version):
                    other = open_database(path)
                    other.close()
                return 0
            return real_user_version(connection)

        with patch.object(storage, "_user_version", patched):
            connection = open_database(path)
        try:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 1)
            names = {
                row["name"]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type = 'table' AND name NOT GLOB 'sqlite_*'"
                )
            }
            self.assertEqual(names, {"snapshots", "observations"})
            record = write_snapshot(
                connection,
                provider="openrouter",
                started_at=STARTED,
                completed_at=COMPLETED,
                source=self.source,
                observations=(self.observation(),),
            )
            self.assertEqual(get_latest_successful_snapshot(connection, "openrouter").id, record.id)
        finally:
            connection.close()

    def test_rollback_denial_preserves_original_error(self):
        path = str(Path(self._tempdir.name) / "rollback-deny.sqlite")
        connection = open_database(path)

        def authorizer(action, arg1, *_rest):
            if action == sqlite3.SQLITE_TRANSACTION and arg1 == "ROLLBACK":
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        connection.set_authorizer(authorizer)
        try:
            with self.assertRaises(sqlite3.IntegrityError) as raised:
                write_snapshot(
                    connection,
                    provider="openrouter",
                    started_at=STARTED,
                    completed_at=COMPLETED,
                    source=self.source,
                    observations=(self.observation("dup"), self.observation("dup")),
                )
            self.assertIsInstance(raised.exception.__context__, sqlite3.Error)
        finally:
            try:
                connection.close()
            except sqlite3.Error:
                pass

    def test_successful_snapshots_query(self):
        self.assertEqual(get_successful_history(self.connection, "openrouter"), ())
        with self.assertRaises(TypeError):
            get_successful_history(self.connection, None)
        self.assertEqual(get_successful_snapshots(self.connection, "openrouter"), ())
        with self.assertRaises(TypeError):
            get_successful_snapshots(self.connection, None)

        first = self.write((self.observation("one"),))
        newer = self.write(
            (self.observation("two", observed_at=datetime(2026, 9, 19, 13, 30, tzinfo=timezone.utc)),),
            started_at=COMPLETED, completed_at=LATER,
        )
        backfill = self.write(
            (self.observation("backfill", observed_at=datetime(2026, 9, 19, 11, 30, tzinfo=timezone.utc)),),
            started_at=EARLIER, completed_at=STARTED,
        )
        tied_source = SourceMetadata("fixture:openrouter", {"capture": "tied"})
        tied = write_snapshot(
            self.connection,
            provider="openrouter",
            started_at=STARTED,
            completed_at=LATER,
            source=tied_source,
            observations=(self.observation("tied", source=tied_source, observed_at=OBSERVED),),
        )
        empty = self.write((), started_at=LATER, completed_at=LATER + timedelta(minutes=1))
        other_source = SourceMetadata("other", {})
        other = write_snapshot(
            self.connection,
            provider="OtherProvider",
            started_at=LATER,
            completed_at=LATER,
            source=other_source,
            observations=(
                self.observation(
                    "two", provider="OtherProvider", source=other_source, observed_at=LATER,
                ),
            ),
        )

        openrouter = get_successful_snapshots(self.connection, "openrouter")
        self.assertEqual(
            [row.id for row in openrouter],
            [backfill.id, first.id, newer.id, tied.id, empty.id],
        )
        self.assertEqual(get_snapshot_observations(self.connection, empty.id), ())
        self.assertEqual(
            [row.id for row in get_successful_snapshots(self.connection, "OtherProvider")],
            [other.id],
        )
        self.assertEqual(get_successful_snapshots(self.connection, "OpenRouter"), ())
        self.assertIsInstance(openrouter, tuple)
        self.assertIsInstance(openrouter[0], SnapshotRecord)

        self.assertEqual(
            get_successful_history(self.connection, "openrouter"),
            tuple((snapshot, get_snapshot_observations(self.connection, snapshot.id))
                  for snapshot in openrouter),
        )
        self.assertEqual(get_successful_history(self.connection, "OpenRouter"), ())
        self.assertEqual(
            get_successful_history(self.connection, "OtherProvider"),
            ((other, get_snapshot_observations(self.connection, other.id)),),
        )

        good = self.write((self.observation("good"),), completed_at=LATER + timedelta(hours=2))
        self.connection.execute(
            "UPDATE snapshots SET started_at = '2026-09-19T12:00:00Z' WHERE id = ?",
            (good.id,),
        )
        with self.assertRaises(StorageError):
            get_successful_snapshots(self.connection, "openrouter")
        with self.assertRaises(StorageError):
            get_successful_history(self.connection, "openrouter")
        self.assertEqual(
            [row.id for row in get_successful_snapshots(self.connection, "OtherProvider")],
            [other.id],
        )

    def test_successful_history_observation_order_and_decoding(self):
        snapshot = self.write(tuple(self.observation(identity) for identity in ("z", "a", "A")))
        history = get_successful_history(self.connection, "openrouter")
        self.assertEqual(history, ((snapshot, get_snapshot_observations(self.connection, snapshot.id)),))
        self.assertEqual([row.offering_id for row in history[0][1]], ["A", "a", "z"])
        self.connection.execute(
            "UPDATE observations SET input_per_million = 'bad' WHERE snapshot_id = ?",
            (snapshot.id,),
        )
        with self.assertRaises(StorageError):
            get_successful_history(self.connection, "openrouter")


if __name__ == "__main__":
    unittest.main()
