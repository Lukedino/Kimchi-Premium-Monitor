"""성공↔실패 전이 알림(--notify-transition) 고정.
가짜 urlopen 만 쓰며 네트워크·실제 자격증명·state.json 은 쓰지 않는다."""
import contextlib
import io
import json
import os
from pathlib import Path
import runpy
import socket
import sys
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import notify_transition as nt  # noqa: E402

KST = timezone(timedelta(hours=9))
NOW = datetime(2026, 11, 23, 9, 15, tzinfo=KST)
OWN_RUN_ID = 9001
# 실제 토큰처럼 보이지 않도록 런타임에 조립한다.
FAKE_TOKEN = "-".join(["synthetic", "notify", "credential", str(len(__file__) % 7)])
FAKE_CHAT = "synthetic" + "-chat"


class FakeResponse:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeUrlopen:
    """GitHub runs 조회와 Telegram 발송을 흉내 낸다."""

    def __init__(self, conclusions=("success",), *, history_error=None, send_error=None,
                 send_ok=True, include_own=False):
        runs = [{"id": OWN_RUN_ID, "conclusion": "failure"}] if include_own else []
        runs += [{"id": 100 + i, "conclusion": c} for i, c in enumerate(conclusions)]
        self.runs = runs
        self.history_error = history_error
        self.send_error = send_error
        self.send_ok = send_ok
        self.history_calls = 0
        self.sent = []

    def __call__(self, request, timeout=None):
        assert timeout == 10
        url = request.full_url
        if url.startswith("https://api.github.com/"):
            self.history_calls += 1
            assert request.data is None
            assert "Authorization" not in request.headers
            if self.history_error:
                raise self.history_error
            return FakeResponse({"total_count": len(self.runs), "workflow_runs": self.runs})
        if url.startswith("https://api.telegram.org/"):
            if self.send_error:
                raise self.send_error
            from urllib.parse import parse_qs
            fields = parse_qs(request.data.decode("utf-8"))
            self.sent.append(fields["text"][0])
            return FakeResponse({"ok": self.send_ok})
        raise AssertionError("unexpected url")


def env(outcome="success", job="success", **extra):
    base = {"MONITOR_OUTCOME": outcome, "JOB_STATUS": job, "GITHUB_RUN_ID": str(OWN_RUN_ID),
            "GITHUB_RUN_NUMBER": "4321", "TELEGRAM_BOT_TOKEN": FAKE_TOKEN, "TELEGRAM_CHAT_ID": FAKE_CHAT}
    base.update(extra)
    return base


class TransitionTests(unittest.TestCase):
    def setUp(self):
        guard = patch.object(socket, "socket", side_effect=AssertionError("network forbidden"))
        guard.start()
        self.addCleanup(guard.stop)

    def run_notify(self, environ, fake):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = nt.main(environ, urlopen=fake, now=NOW)
        self.assertEqual(code, 0)
        return out.getvalue(), err.getvalue()

    def test_success_to_failure_sends_one_failure_message(self):
        fake = FakeUrlopen(("success",))
        out, _ = self.run_notify(env("failure", "failure"), fake)
        self.assertEqual(out.strip(), "notify: sent (failure)")
        self.assertEqual(fake.sent, [
            "김프 모니터(자체 알림)\n⚠️ 김프 모니터 실행 실패 · 2026-11-23 09:15 KST · 단계 수집 · run #4321"])

    def test_failure_to_success_sends_recovery(self):
        fake = FakeUrlopen(("failure",))
        out, _ = self.run_notify(env("success", "success"), fake)
        self.assertEqual(out.strip(), "notify: sent (recovery)")
        self.assertEqual(fake.sent, ["김프 모니터(자체 알림)\n✅ 김프 모니터 회복 · 2026-11-23 09:15 KST"])

    def test_same_result_sends_nothing(self):
        for previous, outcome, job in (("success", "success", "success"), ("failure", "failure", "failure"),
                                       ("timed_out", "skipped", "failure")):
            with self.subTest(previous=previous, outcome=outcome):
                fake = FakeUrlopen((previous,))
                out, _ = self.run_notify(env(outcome, job), fake)
                self.assertEqual(out.strip(), "notify: skipped (no transition)")
                self.assertEqual(fake.sent, [])

    def test_history_unavailable_sends_nothing(self):
        cases = {
            "network": FakeUrlopen(history_error=OSError("https://api.github.com/x refused")),
            "empty": FakeUrlopen(()),
            "only_undecided": FakeUrlopen(("cancelled", "skipped")),
        }
        for name, fake in cases.items():
            with self.subTest(name):
                out, _ = self.run_notify(env("failure", "failure"), fake)
                self.assertEqual(out.strip(), "notify: skipped (history unavailable)")
                self.assertEqual(fake.sent, [])
        bad_json = FakeUrlopen()
        bad_json.runs = "not-a-list"
        out, _ = self.run_notify(env("failure", "failure"), bad_json)
        self.assertEqual(out.strip(), "notify: skipped (history unavailable)")

    def test_skipped_counts_as_install_failure(self):
        fake = FakeUrlopen(("success",))
        out, _ = self.run_notify(env("skipped", "failure"), fake)
        self.assertEqual(out.strip(), "notify: sent (failure)")
        self.assertIn("단계 설치", fake.sent[0])

    def test_publish_failure_after_successful_collection(self):
        fake = FakeUrlopen(("success",))
        self.run_notify(env("success", "failure"), fake)
        self.assertIn("단계 게시", fake.sent[0])

    def test_cancelled_sends_nothing_and_skips_history(self):
        for outcome, job in (("cancelled", "cancelled"), ("success", "cancelled")):
            with self.subTest(outcome=outcome, job=job):
                fake = FakeUrlopen(("success",))
                out, _ = self.run_notify(env(outcome, job), fake)
                self.assertEqual(out.strip(), "notify: skipped (cancelled)")
                self.assertEqual(fake.history_calls, 0)
                self.assertEqual(fake.sent, [])

    def test_own_run_and_undecided_runs_are_skipped(self):
        fake = FakeUrlopen(("cancelled", "success"), include_own=True)
        out, _ = self.run_notify(env("failure", "failure"), fake)
        self.assertEqual(out.strip(), "notify: sent (failure)")

    def test_missing_credentials_sends_nothing(self):
        fake = FakeUrlopen(("success",))
        out, _ = self.run_notify(env("failure", "failure", TELEGRAM_BOT_TOKEN=""), fake)
        self.assertEqual(out.strip(), "notify: skipped (no credentials)")

    def test_message_has_no_forbidden_values(self):
        fake = FakeUrlopen(("success",))
        self.run_notify(env("failure", "failure", USDT_KIMP_HIGH="3.5"), fake)
        message = fake.sent[0]
        for forbidden in (FAKE_TOKEN, FAKE_CHAT, "http", "api.", "3.5", "%", "₩", "USDT"):
            self.assertNotIn(forbidden, message)
        # 숫자는 시각과 run 번호뿐이다.
        digits = "".join(ch if ch.isdigit() else " " for ch in message).split()
        self.assertEqual(digits, ["2026", "11", "23", "09", "15", "4321"])

    def test_token_url_in_exception_never_reaches_output(self):
        leaky = RuntimeError(f"https://api.telegram.org/bot{FAKE_TOKEN}/sendMessage failed")
        for fake in (FakeUrlopen(("success",), send_error=leaky),
                     FakeUrlopen(("success",), send_error=OSError(f"bot{FAKE_TOKEN}"))):
            out, err = self.run_notify(env("failure", "failure"), fake)
            self.assertNotIn(FAKE_TOKEN, out + err)
            self.assertNotIn("http", out + err)
            self.assertRegex(out.strip(), r"^notify: failed \((RuntimeError|OSError)\)$")

    def test_telegram_not_ok_is_failed(self):
        fake = FakeUrlopen(("success",), send_ok=False)
        out, _ = self.run_notify(env("failure", "failure"), fake)
        self.assertEqual(out.strip(), "notify: failed (not ok)")

    def test_log_is_a_single_line(self):
        fake = FakeUrlopen(("success",))
        out, err = self.run_notify(env("failure", "failure"), fake)
        self.assertEqual(len(out.splitlines()), 1)
        self.assertEqual(err, "")


class _BlockThirdParty:
    """서드파티·monitor 내부 모듈 import 를 막는 finder."""
    BLOCKED = {"requests", "yfinance", "urllib3", "pandas", "numpy",
               "state_lock", "quote_evidence", "run_timing"}

    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in self.BLOCKED:
            raise ImportError("blocked in test")
        return None


class EntryPointTests(unittest.TestCase):
    def test_monitor_branch_runs_before_other_imports(self):
        fake = FakeUrlopen(("success",))
        out = io.StringIO()
        finder = _BlockThirdParty()
        cached = {name: sys.modules.pop(name) for name in list(sys.modules)
                  if name.split(".")[0] in _BlockThirdParty.BLOCKED}
        sys.meta_path.insert(0, finder)
        try:
            with patch.object(sys, "argv", ["monitor.py", "--notify-transition"]),                  patch.dict(os.environ, env("failure", "failure"), clear=True),                  patch.object(nt.urllib.request, "urlopen", fake),                  patch.object(socket, "socket", side_effect=AssertionError("network forbidden")),                  contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as exit_info:
                runpy.run_path(str(ROOT / "monitor.py"), run_name="__main__")
        finally:
            sys.meta_path.remove(finder)
            sys.modules.update(cached)
        self.assertEqual(exit_info.exception.code, 0)
        self.assertEqual(out.getvalue().strip(), "notify: sent (failure)")
        self.assertEqual(len(fake.sent), 1)
        for name in _BlockThirdParty.BLOCKED:
            self.assertNotIn(name, sys.modules.keys() - cached.keys())

    def test_blocked_import_guard_is_effective(self):
        finder = _BlockThirdParty()
        cached = {name: sys.modules.pop(name) for name in list(sys.modules) if name == "state_lock"}
        sys.meta_path.insert(0, finder)
        try:
            with self.assertRaises(ImportError):
                __import__("state_lock")
        finally:
            sys.meta_path.remove(finder)
            sys.modules.update(cached)

    def test_workflow_step_contract(self):
        text = (ROOT / ".github/workflows/kimp-monitor.yml").read_text(encoding="utf-8")
        step = text.split("      - name: Notify on transition", 1)[1]
        self.assertIn("if: ${{ always() }}", step)
        self.assertIn("continue-on-error: true", step)
        self.assertIn("MONITOR_OUTCOME: ${{ steps.monitor.outcome }}", step)
        self.assertIn("JOB_STATUS: ${{ job.status }}", step)
        self.assertIn("python -B -S monitor.py --notify-transition", step)
        self.assertNotIn("GITHUB_TOKEN", step)


if __name__ == "__main__":
    unittest.main()
