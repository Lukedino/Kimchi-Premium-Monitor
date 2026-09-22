"""Synthetic wall/monotonic clocks; never infer scheduler slots from cron."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from run_timing import start_run

UTC = timezone.utc
KST = timezone(timedelta(hours=9))
NOW = datetime(2026, 9, 22, 3, tzinfo=UTC)


class RunTimingTests(unittest.TestCase):
    def start(self, mode="schedule", **kwargs):
        values = dict(started_at=NOW, monotonic_started=10)
        values.update(kwargs)
        return start_run(mode, **values)

    def finish(self, run, **kwargs):
        values = dict(finished_at=NOW + timedelta(minutes=2), monotonic_finished=130)
        values.update(kwargs)
        return run.finish(**values)

    def test_long_fake_fetch_keeps_start_finish_duration_separate(self):
        r = self.finish(self.start())
        self.assertEqual(r["started_at"], NOW.isoformat())
        self.assertEqual(r["finished_at"], (NOW + timedelta(minutes=2)).isoformat())
        self.assertEqual(r["duration_seconds"], 120)

    def test_thirty_minute_gap_is_observed_interval_not_delay_or_missed_run(self):
        run = self.start(previous_scheduled_start={"started_at": (NOW - timedelta(minutes=30)).isoformat(), "run_id": "100"}, run_id="101")
        r = self.finish(run)
        self.assertEqual(r["observed_interval_seconds"], 1800)
        self.assertIsNone(r["schedule_delay_seconds"])
        self.assertNotIn("missed_runs", r)

    def test_manual_run_preserves_schedule_baseline_between_scheduled_runs(self):
        first = self.start(run_id="100")
        baseline = first.next_scheduled_start
        manual = self.start("workflow_dispatch", started_at=NOW + timedelta(minutes=10), previous_scheduled_start=baseline)
        self.assertEqual(manual.next_scheduled_start, baseline)
        self.assertIsNone(manual.observed_interval_seconds)
        later = self.start(started_at=NOW + timedelta(minutes=30), previous_scheduled_start=manual.next_scheduled_start, run_id="101")
        self.assertEqual(later.observed_interval_seconds, 1800)

    def test_unspecified_or_other_event_is_not_a_scheduled_execution(self):
        baseline = {"started_at": NOW.isoformat(), "run_id": "100"}
        for mode in ("", "push", "repository_dispatch", "SYNTHETIC_SECRET"):
            run = self.start(mode, previous_scheduled_start=baseline)
            self.assertEqual(run.next_scheduled_start, baseline)
            self.assertEqual(run.run_mode, "other")
            self.assertNotIn("SYNTHETIC_SECRET", json.dumps(self.finish(run)))

    def test_first_schedule_has_no_interval_evidence(self):
        run = self.start(run_id="100")
        self.assertIsNone(run.observed_interval_seconds)
        self.assertEqual(run.next_scheduled_start, {"started_at": NOW.isoformat(), "run_id": "100"})

    def test_aware_offsets_describe_same_elapsed_interval(self):
        previous = (NOW - timedelta(minutes=15)).astimezone(KST).isoformat()
        run = self.start(started_at=NOW.astimezone(KST), previous_scheduled_start=previous)
        self.assertEqual(run.observed_interval_seconds, 900)
        self.assertEqual(self.finish(run)["started_at"], NOW.isoformat())

    def test_legacy_naive_timestamp_is_not_guessed_as_kst_or_utc(self):
        run = self.start(previous_scheduled_start="2026-09-22T11:45:00")
        self.assertIsNone(run.observed_interval_seconds)
        self.assertIn("previous_scheduled_start_naive", self.finish(run)["issues"])
        self.assertEqual(run.next_scheduled_start["started_at"], NOW.isoformat())

    def test_legacy_bad_timestamp_is_fixed_issue_not_exception_text(self):
        for previous in ("SYNTHETIC_SECRET", {"started_at": []}, 123):
            run = self.start(previous_scheduled_start=previous)
            result = self.finish(run)
            self.assertIsNone(result["observed_interval_seconds"])
            self.assertIn("previous_scheduled_start_invalid", result["issues"])
            self.assertNotIn("SYNTHETIC_SECRET", json.dumps(result))

    def test_manual_preserves_even_uninterpretable_old_baseline(self):
        previous = {"started_at": "2026-09-22T12:00:00", "future_extension": "kept"}
        run = self.start("workflow_dispatch", previous_scheduled_start=previous)
        self.assertEqual(run.next_scheduled_start, previous)

    def test_previous_future_start_is_not_negative_interval_or_new_baseline(self):
        previous = {"started_at": (NOW + timedelta(minutes=5)).isoformat(), "run_id": "100"}
        run = self.start(previous_scheduled_start=previous, run_id="101")
        self.assertEqual(run.next_scheduled_start, previous)
        self.assertIsNone(run.observed_interval_seconds)
        self.assertIn("scheduled_start_before_previous", self.finish(run)["issues"])

    def test_same_run_identity_preserves_baseline_and_has_no_new_interval(self):
        previous = {"started_at": (NOW - timedelta(minutes=5)).isoformat(), "run_id": "100"}
        run = self.start(previous_scheduled_start=previous, run_id="100")
        self.assertEqual(run.next_scheduled_start, previous)
        self.assertIsNone(run.observed_interval_seconds)
        self.assertIn("scheduled_rerun", self.finish(run)["issues"])

    def test_attempt_greater_than_one_does_not_advance_even_with_missing_identity(self):
        for attempt in (2, "2", 50):
            run = self.start(run_attempt=attempt)
            self.assertIsNone(run.next_scheduled_start)
            self.assertIsNone(run.observed_interval_seconds)
            self.assertIn("scheduled_rerun", self.finish(run)["issues"])

    def test_explicit_authoritative_slot_allows_delay_measurement(self):
        run = self.start(scheduled_for=NOW - timedelta(seconds=70))
        self.assertEqual(run.schedule_delay_seconds, 70)

    def test_future_or_naive_slot_does_not_create_negative_or_guessed_delay(self):
        for slot, issue in ((NOW + timedelta(seconds=1), "scheduled_slot_future"),
                            (NOW.replace(tzinfo=None), "scheduled_slot_invalid")):
            run = self.start(scheduled_for=slot)
            self.assertIsNone(run.schedule_delay_seconds)
            self.assertIn(issue, self.finish(run)["issues"])

    def test_wall_clock_rollback_does_not_replace_monotonic_duration(self):
        result = self.finish(self.start(), finished_at=NOW - timedelta(hours=1))
        self.assertEqual(result["duration_seconds"], 120)
        self.assertIn("wall_clock_reversed", result["issues"])

    def test_bad_or_reversed_monotonic_is_unknown_not_clamped(self):
        for end, code in ((9, "monotonic_reversed"), (float("nan"), "monotonic_invalid"),
                          (float("inf"), "monotonic_invalid"), (True, "monotonic_invalid"),
                          (10 ** 1000, "monotonic_invalid")):
            result = self.finish(self.start(), monotonic_finished=end)
            self.assertIsNone(result["duration_seconds"])
            self.assertIn(code, result["issues"])

    def test_finite_monotonic_subtraction_overflow_is_not_json_infinity(self):
        result = self.finish(self.start(monotonic_started=-1e308), monotonic_finished=1e308)
        self.assertIsNone(result["duration_seconds"])
        json.dumps(result, allow_nan=False)

    def test_bad_start_monotonic_does_not_use_wall_duration(self):
        result = self.finish(self.start(monotonic_started="SYNTHETIC_SECRET"))
        self.assertIsNone(result["duration_seconds"])
        self.assertNotIn("SYNTHETIC_SECRET", json.dumps(result))

    def test_naive_start_or_finish_rejected_without_timezone_guess(self):
        with self.assertRaisesRegex(ValueError, "^aware_datetime_required$"):
            self.start(started_at=NOW.replace(tzinfo=None))
        with self.assertRaisesRegex(ValueError, "^aware_datetime_required$"):
            self.finish(self.start(), finished_at=NOW.replace(tzinfo=None))

    def test_baseline_objects_are_not_mutated_or_shared(self):
        original = {"started_at": NOW.isoformat(), "extension": [1]}
        run = self.start("workflow_dispatch", previous_scheduled_start=original)
        original["extension"].append(2)
        out = run.next_scheduled_start
        out["extension"].append(3)
        self.assertEqual(run.next_scheduled_start["extension"], [1])


if __name__ == "__main__":
    unittest.main()
