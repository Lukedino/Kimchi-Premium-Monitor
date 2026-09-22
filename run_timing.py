"""Pure run observations; no scheduler inference, clock reads, or state mutation."""
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
import math
import re


def _aware(value):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("aware_datetime_required")
    return value.astimezone(timezone.utc)


def _monotonic(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) else None


def _previous(value):
    if value is None:
        return None, None
    stamp = value.get("started_at") if isinstance(value, dict) else value
    if not isinstance(stamp, str):
        return None, "previous_scheduled_start_invalid"
    try:
        parsed = datetime.fromisoformat(stamp)
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return None, "previous_scheduled_start_naive"
        return parsed.astimezone(timezone.utc), None
    except (ValueError, OverflowError):
        return None, "previous_scheduled_start_invalid"


@dataclass(frozen=True)
class RunTiming:
    started_at: datetime
    monotonic_started: float | None
    run_mode: str
    observed_interval_seconds: float | None
    schedule_delay_seconds: float | None
    _next_scheduled_start: object
    issues: tuple[str, ...]

    @property
    def next_scheduled_start(self):
        """A separate value for state; manual/retry calls preserve the old baseline."""
        return deepcopy(self._next_scheduled_start)

    def finish(self, *, finished_at, monotonic_finished):
        finished = _aware(finished_at)
        issues = list(self.issues)
        end = _monotonic(monotonic_finished)
        duration = None
        if end is None or self.monotonic_started is None:
            issues.append("monotonic_invalid")
        elif end < self.monotonic_started:
            issues.append("monotonic_reversed")
        else:
            duration = end - self.monotonic_started
            if not math.isfinite(duration):
                duration = None
                issues.append("monotonic_invalid")
        if finished < self.started_at:
            issues.append("wall_clock_reversed")
        return {"started_at": self.started_at.isoformat(), "finished_at": finished.isoformat(),
                "duration_seconds": duration, "run_mode": self.run_mode,
                "observed_interval_seconds": self.observed_interval_seconds,
                "schedule_delay_seconds": self.schedule_delay_seconds,
                "issues": sorted(set(issues))}


def start_run(run_mode, *, started_at, monotonic_started, previous_scheduled_start=None,
              scheduled_for=None, run_id=None, run_attempt=None):
    """Only literal 'schedule' advances the scheduled-start baseline.

    previous_scheduled_start is None, a legacy timestamp, or our
    {started_at: aware ISO string, run_id: digits-or-None} record. A legacy naive
    value is not localized. scheduled_for is an explicit aware scheduled-slot
    instant, if an actual authoritative slot is available (cron alone is not).
    GHA run_id/attempt are optional observations, not automatically read here.
    """
    started = _aware(started_at)
    monotonic = _monotonic(monotonic_started)
    mode = run_mode if run_mode in {"schedule", "workflow_dispatch"} else "other"
    baseline = deepcopy(previous_scheduled_start)
    issues = []
    identity = run_id if isinstance(run_id, str) and re.fullmatch(r"[0-9]{1,30}", run_id) else None
    if run_id is not None and identity is None:
        issues.append("run_id_invalid")
    if isinstance(run_attempt, str) and re.fullmatch(r"[0-9]{1,10}", run_attempt):
        attempt = int(run_attempt)
    else:
        attempt = run_attempt if type(run_attempt) is int else None
    if run_attempt is not None and (attempt is None or attempt < 1):
        issues.append("run_attempt_invalid")
        attempt = None
    interval = delay = None
    if mode == "schedule":
        previous, issue = _previous(previous_scheduled_start)
        if issue:
            issues.append(issue)
        repeated_id = identity is not None and isinstance(baseline, dict) and baseline.get("run_id") == identity
        retry = (attempt is not None and attempt > 1) or repeated_id
        if retry:
            issues.append("scheduled_rerun")
        elif previous is not None and started < previous:
            issues.append("scheduled_start_before_previous")
        else:
            if previous is not None:
                interval = (started - previous).total_seconds()
            baseline = {"started_at": started.isoformat(), "run_id": identity}
        if scheduled_for is not None:
            try:
                slot = _aware(scheduled_for)
            except (TypeError, ValueError, OverflowError):
                issues.append("scheduled_slot_invalid")
            else:
                if started < slot:
                    issues.append("scheduled_slot_future")
                else:
                    delay = (started - slot).total_seconds()
    return RunTiming(started, monotonic, mode, interval, delay, baseline, tuple(issues))
