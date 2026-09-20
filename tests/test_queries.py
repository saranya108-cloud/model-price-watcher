"""Selected evidence projection, with real detection and SQLite history."""

import sqlite3
import unittest
from collections.abc import Sequence
from dataclasses import FrozenInstanceError, fields, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch

from model_price_watcher import queries
from model_price_watcher.detection import (
    BaselineReason, DealStatus, DetectionDiagnostic, Presence, SnapshotFrame,
    analyze_history,
)
from model_price_watcher.models import CatalogObservation, SourceMetadata
from model_price_watcher.queries import (
    LookupStatus, SelectedOfferingLookup, SelectedOfferingReport,
    select_offerings, view_selected_offerings,
)
from model_price_watcher.storage import (
    ObservationRecord, SnapshotRecord, StorageError, get_successful_history,
    open_database, write_snapshot,
)


DAY = datetime(2026, 9, 1, tzinfo=timezone.utc)
SOURCE = SourceMetadata("fixture:queries", {"capture": "original"})
D = Decimal


def frame(number, rows=(), *, day=None, provider="p"):
    time = DAY + timedelta(days=number if day is None else day)
    return SnapshotFrame(
        SnapshotRecord(number, provider, time, time, SOURCE),
        tuple(ObservationRecord(
            number, identity, time, inp, out, conditions,
            {"id": identity, "capture": str(number)},
        ) for identity, inp, out, conditions in rows),
    )


def select(frames, ids, *, now=None, provider="p"):
    return select_offerings(
        frames, provider=provider, offering_ids=ids,
        now=now if now is not None else (
            frames[-1].snapshot.completed_at if frames else DAY
        ),
    )


class ProjectionTests(unittest.TestCase):
    def test_first_observation_has_price_and_provenance_without_comparison(self):
        first = frame(1, [("a", D(10), None, {"request": "opaque"})])
        report = select((first,), ["a", "missing"])
        current, missing = report.lookups
        self.assertEqual(current.status, LookupStatus.CURRENT)
        self.assertIs(current.current_observation, first.observations[0])
        self.assertIs(report.latest_snapshot, first.snapshot)
        self.assertEqual(report.latest_snapshot.source, SOURCE)
        self.assertEqual(current.current_observation.input_per_million, D(10))
        self.assertIsNone(current.current_observation.output_per_million)
        self.assertIsNone(current.detection.comparison)
        self.assertEqual(current.detection.presence, Presence.CURRENT)
        self.assertEqual(missing, SelectedOfferingLookup(
            "missing", LookupStatus.NEVER_OBSERVED, None, None,
        ))

    def test_latest_frame_is_only_current_price_source(self):
        old = frame(1, [("a", D(10), D(20), {}), ("gone", D(7), D(8), {})])
        latest = frame(2, [("a", None, D(18), {})])
        report = select((old, latest), ["gone", "a"])
        current, gone = report.lookups
        self.assertIs(current.current_observation, latest.observations[0])
        self.assertEqual(current.current_observation.snapshot_id, report.latest_snapshot.id)
        self.assertIsNone(current.current_observation.input_per_million)
        self.assertEqual(gone.status, LookupStatus.NO_LONGER_OBSERVED)
        self.assertIsNone(gone.current_observation)
        self.assertEqual(gone.detection.presence, Presence.NO_LONGER_OBSERVED)

    def test_no_history_and_successful_empty_latest_are_distinct(self):
        empty = select((), ["a"])
        self.assertIsNone(empty.latest_snapshot)
        self.assertEqual(empty.lookups[0].status, LookupStatus.NEVER_OBSERVED)
        self.assertEqual(empty.diagnostics, ())
        history = (frame(1, [("a", D(1), D(2), {})]), frame(2))
        report = select(history, ["a", "z"])
        self.assertEqual(report.latest_snapshot, history[-1].snapshot)
        self.assertEqual(tuple(x.status for x in report.lookups), (
            LookupStatus.NO_LONGER_OBSERVED, LookupStatus.NEVER_OBSERVED,
        ))
        self.assertTrue(all(x.current_observation is None for x in report.lookups))
        only_empty = select((frame(1),), ["a"])
        self.assertIsNotNone(only_empty.latest_snapshot)
        self.assertEqual(only_empty.lookups[0].status, LookupStatus.NEVER_OBSERVED)

    def test_return_after_empty_or_nonempty_absence_breaks_continuity(self):
        for barrier in (frame(2), frame(2, [("other", D(1), D(1), {})])):
            with self.subTest(barrier=barrier):
                history = (
                    frame(1, [("a", D(10), D(20), {})]), barrier,
                    frame(3, [("a", D(5), D(10), {})]),
                )
                report = select(history, ["a"])
                self.assertEqual(report.lookups[0].detection.baseline_reason,
                                 BaselineReason.RETURN_AFTER_ABSENCE)
                self.assertIsNone(report.lookups[0].detection.comparison)
                self.assertEqual(report.active_observed_decrease_ids, ())

    def test_active_expired_suppressed_none_and_unselected(self):
        history = (
            frame(1, [(name, D(10), D(20), {}) for name in ("active", "mixed", "same", "other")]),
            frame(2, [("active", D(5), D(20), {}), ("mixed", D(5), D(30), {}),
                      ("same", D(10), D(20), {}), ("other", D(5), D(20), {})]),
        )
        report = select(history, ["same", "mixed", "active"])
        self.assertEqual(tuple(x.detection.deal_status for x in report.lookups), (
            DealStatus.ACTIVE, DealStatus.SUPPRESSED, DealStatus.NONE,
        ))
        self.assertEqual(report.active_observed_decrease_ids, ("active",))
        expired = select(history, ["active"], now=DAY + timedelta(days=9))
        self.assertEqual(expired.lookups[0].detection.deal_status, DealStatus.EXPIRED)
        self.assertEqual(expired.active_observed_decrease_ids, ())

    def test_zero_token_facts_preserve_unsupported_and_unknown_evidence(self):
        rows = [
            ("zero", D(0), D(0), {}), ("signed", D("-0"), D("0.00"), {}),
            ("unsupported", D(0), D(0), {"request": "1"}),
            ("one", D(0), D(1), {}), ("unknown", D(0), None, {}),
            ("model:free", D(1), D(1), {}), ("unknown:free", None, None, {}),
        ]
        report = select((frame(1, rows),), [x[0] for x in rows])
        self.assertEqual(report.zero_token_price_ids, ("signed", "unsupported", "zero"))
        unsupported = next(x for x in report.lookups if x.offering_id == "unsupported")
        self.assertEqual(unsupported.current_observation.conditions, {"request": "1"})
        self.assertTrue(unsupported.detection.unsupported_pricing_present)
        gone = select((frame(1, rows), frame(2)), [x[0] for x in rows])
        self.assertEqual(gone.zero_token_price_ids, ())

    def test_exact_identities_deduplication_and_order(self):
        ids = ["a", "A", "x ", "x", "model:free", "model", "é", "e\u0301"]
        history = (frame(1, [(x, D(1), D(2), {}) for x in ids]),)
        report = select(history, ids + ids)
        self.assertEqual(tuple(x.offering_id for x in report.lookups), tuple(sorted(ids)))
        self.assertTrue(all(x.status is LookupStatus.CURRENT for x in report.lookups))

    def test_empty_selection_is_not_discovery(self):
        history = (frame(1, [("a", D(0), D(0), {})]),)
        with patch.object(queries, "analyze_history", wraps=analyze_history) as analyze:
            report = select(history, [])
        analyze.assert_called_once_with(history, provider="p", now=history[-1].snapshot.completed_at)
        self.assertEqual(report.lookups, ())
        self.assertEqual(report.active_observed_decrease_ids, ())
        self.assertEqual(report.zero_token_price_ids, ())
        self.assertEqual(report.latest_snapshot, history[0].snapshot)

    def test_projection_does_not_rescan_history_per_selection(self):
        class CountedSequence(Sequence):
            def __init__(self, values):
                self.values = values
                self.reads = 0

            def __len__(self):
                return len(self.values)

            def __getitem__(self, key):
                self.reads += 1
                return self.values[key]

        counts = []
        for selected_count in (1, 1000):
            observations = []
            frames = []
            for n in range(20):
                item = frame(n, [(str(i), D(1), D(2), {}) for i in range(50)])
                counted = CountedSequence(item.observations)
                observations.append(counted)
                frames.append(replace(item, observations=counted))
            frames = CountedSequence(frames)
            reduced = analyze_history(frames, provider="p", now=DAY + timedelta(days=20))
            detections = CountedSequence(reduced.offerings)
            frames.reads = 0
            for rows in observations:
                rows.reads = 0
            with patch.object(queries, "analyze_history", return_value=replace(
                reduced, offerings=detections,
            )):
                select(frames, [str(i) for i in range(selected_count)],
                       now=DAY + timedelta(days=20))
            counts.append((frames.reads, tuple(x.reads for x in observations), detections.reads))
        self.assertEqual(counts[0], counts[1])
        self.assertLessEqual(counts[1][0], 3)
        self.assertEqual(counts[1][1][:-1], (0,) * 19)
        self.assertLessEqual(counts[1][1][-1], 51)
        self.assertLessEqual(counts[1][2], 51)

    def test_evaluation_time_uses_detection_utc_normalization(self):
        now = DAY.astimezone(timezone(timedelta(hours=5)))
        report = select((), [], now=now)
        self.assertEqual(report.evaluated_at, DAY)
        self.assertIs(report.evaluated_at.tzinfo, timezone.utc)

    def test_unchanged_state_still_returns_latest_observation_evidence(self):
        history = (frame(1, [("a", D(10), D(20), {})]),
                   frame(2, [("a", D(5), D(20), {})]),
                   frame(3, [("a", D(5), D(20), {})]))
        report = select(history, ["a"])
        self.assertIs(report.lookups[0].current_observation, history[-1].observations[0])
        self.assertEqual(report.lookups[0].current_observation.input_per_million, D(5))
        self.assertEqual(report.lookups[0].current_observation.source_record["capture"], "3")
        self.assertEqual(report.lookups[0].detection.current_state_start_snapshot_id, 2)

    def test_argument_validation(self):
        for invalid in ("a", b"a", None, {"a"}, iter(["a"]), [1], [None]):
            with self.subTest(invalid=invalid), self.assertRaises(TypeError):
                select((), invalid)
        for invalid in ("", " ", "\t\n"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                select((), [invalid])
            with self.subTest(provider=invalid), self.assertRaises(ValueError):
                select((), [], provider=invalid)
        with self.assertRaises(TypeError):
            select((), [], provider=1)
        for invalid, error in ((None, TypeError), (DAY.replace(tzinfo=None), ValueError)):
            with self.subTest(now=invalid), self.assertRaises(error):
                select_offerings((), provider="p", offering_ids=[], now=invalid)
        with self.assertRaises(ValueError):
            select((frame(2), frame(1)), [])
        with self.assertRaises(ValueError):
            select((frame(1, provider="other"),), [])

    def test_real_absent_diagnostics_survive_selection(self):
        history = (
            frame(1, [(x, D(10), D(20), {}) for x in ("a", "b")], day=0),
            frame(2, [(x, D(5), D(20), {}) for x in ("a", "b")], day=0),
            frame(3),
        )
        report = select(history, ["a"])
        self.assertEqual(report.lookups[0].status, LookupStatus.NO_LONGER_OBSERVED)
        self.assertEqual(report.diagnostics, (
            DetectionDiagnostic("CHANGED_STATE_AT_EQUAL_TIME", "a", (1, 2)),
        ))

    def test_diagnostic_filter_preserves_global_and_selected_order(self):
        # Detection currently emits offering diagnostics only; inject provider
        # diagnostics at its public boundary to exercise the forward contract.
        diagnostics = tuple(DetectionDiagnostic(str(n), identity) for n, identity in
                            enumerate(("b", None, "a", "b", None, "a")))
        reduced = replace(analyze_history((), provider="p", now=DAY), diagnostics=diagnostics)
        with patch.object(queries, "analyze_history", return_value=reduced):
            report = select((), ["a"])
            empty = select((), [])
        self.assertEqual(report.diagnostics, tuple(diagnostics[n] for n in (1, 2, 4, 5)))
        self.assertEqual(empty.diagnostics, (diagnostics[1], diagnostics[4]))

    def test_inconsistent_detection_fails_instead_of_partial_answer(self):
        history = (frame(1, [("a", D(1), D(2), {})]),)
        reduced = analyze_history(history, provider="p", now=DAY + timedelta(days=1))
        wrong = replace(reduced.offerings[0], presence=Presence.NO_LONGER_OBSERVED)
        for broken in (replace(reduced, offerings=()), replace(reduced, offerings=(wrong,)),
                       replace(reduced, latest_snapshot_id=999),
                       replace(reduced, offerings=(replace(reduced.offerings[0], latest_snapshot_id=999),)),
                       replace(reduced, offerings=(replace(reduced.offerings[0], latest_observed_at=DAY),))):
            with self.subTest(broken=broken), patch.object(
                queries, "analyze_history", return_value=broken,
            ), self.assertRaises(RuntimeError):
                select(history, ["a"])
        absent_history = history + (frame(2),)
        with patch.object(queries, "analyze_history", return_value=replace(
            reduced, latest_snapshot_id=2, latest_completed_at=frame(2).snapshot.completed_at,
        )), self.assertRaises(RuntimeError):
            select(absent_history, ["a"])

    def test_frozen_minimal_api_without_free_execution_claims(self):
        self.assertEqual({x.name for x in fields(SelectedOfferingLookup)}, {
            "offering_id", "status", "current_observation", "detection",
        })
        self.assertEqual({x.name for x in fields(SelectedOfferingReport)}, {
            "provider", "evaluated_at", "latest_snapshot", "lookups", "diagnostics",
        })
        report = select((), ["a"])
        for obj in (report, report.lookups[0]):
            self.assertFalse(any("free" in name for name in dir(obj)))
        with self.assertRaises(FrozenInstanceError):
            report.provider = "other"
        with self.assertRaises(FrozenInstanceError):
            report.lookups[0].offering_id = "other"


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.connection = open_database(":memory:")
        self.addCleanup(self.connection.close)

    def write(self, item):
        snapshot = item.snapshot
        return write_snapshot(
            self.connection, provider=snapshot.provider,
            started_at=snapshot.started_at, completed_at=snapshot.completed_at,
            source=snapshot.source,
            observations=tuple(CatalogObservation(
                snapshot.provider, row.offering_id, row.observed_at, snapshot.source,
                row.input_per_million, row.output_per_million, row.conditions, row.source_record,
            ) for row in item.observations),
        )

    def view(self, ids=("a",), *, provider="p", now=DAY + timedelta(days=30)):
        return view_selected_offerings(self.connection, provider=provider, offering_ids=ids, now=now)

    def test_equivalence_at_each_snapshot_and_provider_isolation(self):
        self.write(frame(0, [("foreign", D(0), D(0), {})], provider="P"))
        self.assertIsNone(self.view().latest_snapshot)
        for item in (frame(1, [("a", D(10), None, {})]), frame(2),
                     frame(3, [("a", D(0), D(0), {"request": "1"})])):
            self.write(item)
            history = tuple(SnapshotFrame(*pair) for pair in get_successful_history(self.connection, "p"))
            self.assertEqual(self.view(("a", "foreign")), select(
                history, ("a", "foreign"), now=DAY + timedelta(days=30),
            ))
            self.assertEqual(self.view(("foreign",)).lookups[0].status, LookupStatus.NEVER_OBSERVED)
        self.assertIsNone(self.view(provider="p ").latest_snapshot)
        self.assertEqual(self.view(("foreign",), provider="P").zero_token_price_ids, ("foreign",))

    def test_one_complete_read_and_analysis_without_writes(self):
        for n in range(12):
            self.write(frame(n, [] if n == 5 else [("a", D(10), D(20), {}), ("unselected", D(2), D(3), {})]))
        for ids in ((), ("a",), tuple(str(n) for n in range(1000))):
            with self.subTest(count=len(ids)):
                statements = []
                before = self.connection.total_changes
                self.connection.set_trace_callback(statements.append)
                try:
                    with patch.object(queries, "get_successful_history", wraps=get_successful_history) as read, patch.object(
                        queries, "analyze_history", wraps=analyze_history,
                    ) as analyze:
                        self.view(ids)
                    read.assert_called_once_with(self.connection, "p")
                    analyze.assert_called_once()
                    frames = analyze.call_args.args[0]
                    self.assertEqual(len(frames), 12)
                    self.assertEqual(frames[5].observations, ())
                    self.assertEqual(len(frames[-1].observations), 2)
                finally:
                    self.connection.set_trace_callback(None)
                self.assertEqual([sql.split()[0] for sql in statements], ["BEGIN", "SELECT", "COMMIT"])
                self.assertEqual(self.connection.total_changes, before)
                self.assertFalse(self.connection.in_transaction)

    def test_caller_transaction_survives_success_and_all_failure_phases(self):
        self.write(frame(1, [("a", D(1), D(2), {})]))
        for owned_by_caller in (False, True):
            for phase in ("success", "decode", "analysis", "projection", "sqlite", "selection"):
                with self.subTest(caller=owned_by_caller, phase=phase):
                    if owned_by_caller:
                        self.connection.execute("BEGIN")
                    marker = RuntimeError("projection failed")
                    if phase == "success":
                        self.view()
                    elif phase == "decode":
                        with patch.object(queries, "get_successful_history", side_effect=StorageError("bad")):
                            with self.assertRaises(StorageError):
                                self.view()
                    elif phase == "analysis":
                        with self.assertRaises(ValueError):
                            self.view(now=DAY.replace(tzinfo=None))
                    elif phase == "projection":
                        with patch.object(queries, "SelectedOfferingReport", side_effect=marker):
                            with self.assertRaises(RuntimeError) as caught:
                                self.view()
                        self.assertIs(caught.exception, marker)
                    elif phase == "selection":
                        with self.assertRaises(TypeError):
                            self.view([1])
                    else:
                        marker = sqlite3.OperationalError("read failed")
                        with patch.object(queries, "get_successful_history", side_effect=marker):
                            with self.assertRaises(sqlite3.OperationalError) as caught:
                                self.view()
                        self.assertIs(caught.exception, marker)
                    self.assertEqual(self.connection.in_transaction, owned_by_caller)
                    if owned_by_caller:
                        self.connection.rollback()

    def test_malformed_persisted_identity_fails_closed_and_preserves_ownership(self):
        snapshot = self.write(frame(1, [("a", D(1), D(2), {})]))
        for identity, selections in (
            (" \t\n", (("a",), ())),
            ("valid\x00suffix", (("valid\x00suffix",),)),
        ):
            self.connection.execute(
                "UPDATE observations SET offering_id = ? WHERE snapshot_id = ?",
                (identity, snapshot.id),
            )
            for ids in selections:
                for caller in (False, True):
                    with self.subTest(identity=identity, ids=ids, caller=caller):
                        if caller:
                            self.connection.execute("BEGIN")
                        try:
                            with self.assertRaises(StorageError):
                                self.view(ids)
                            self.assertEqual(self.connection.in_transaction, caller)
                        finally:
                            if caller:
                                self.connection.rollback()

    def test_real_malformed_storage_and_invalid_history_propagate(self):
        self.write(frame(1, [("a", D(1), D(2), {})]))
        for caller in (False, True):
            for column, bad, error in (
                ("input_per_million", "not-money", StorageError),
                ("observed_at", "2026-09-01T00:00:00.000000Z", ValueError),
            ):
                with self.subTest(caller=caller, column=column):
                    original = self.connection.execute(f"SELECT {column} FROM observations").fetchone()[0]
                    self.connection.execute(f"UPDATE observations SET {column} = ?", (bad,))
                    if caller:
                        self.connection.execute("BEGIN")
                    with self.assertRaises(error):
                        self.view()
                    self.assertEqual(self.connection.in_transaction, caller)
                    if caller:
                        self.connection.rollback()
                    self.connection.execute(f"UPDATE observations SET {column} = ?", (original,))

    def test_rollback_failure_preserves_original_exception(self):
        class BrokenRollback(sqlite3.Connection):
            def rollback(self):
                raise sqlite3.OperationalError("rollback failed")

        connection = sqlite3.connect(":memory:", factory=BrokenRollback)
        self.addCleanup(connection.close)
        marker = StorageError("original read error")
        with patch.object(queries, "get_successful_history", side_effect=marker):
            with self.assertRaises(StorageError) as caught:
                view_selected_offerings(connection, provider="p", offering_ids=[], now=DAY)
        self.assertIs(caught.exception, marker)
        self.assertTrue(connection.in_transaction)

    def test_invalid_connection_type(self):
        with self.assertRaises(TypeError):
            view_selected_offerings(None, provider="p", offering_ids=[], now=DAY)

    def test_missing_schema_is_not_initialized_or_hidden(self):
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        with self.assertRaises(sqlite3.OperationalError):
            view_selected_offerings(connection, provider="p", offering_ids=[], now=DAY)
        self.assertFalse(connection.in_transaction)
        self.assertEqual(connection.execute("SELECT name FROM sqlite_master").fetchall(), [])

    def test_commit_failure_rolls_back_owned_transaction(self):
        def authorize(action, arg1, arg2, database, trigger):
            if action == sqlite3.SQLITE_TRANSACTION and arg1 == "COMMIT":
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        self.connection.set_authorizer(authorize)
        try:
            with self.assertRaises(sqlite3.DatabaseError):
                self.view()
            self.assertFalse(self.connection.in_transaction)
        finally:
            self.connection.set_authorizer(None)


if __name__ == "__main__":
    unittest.main()
