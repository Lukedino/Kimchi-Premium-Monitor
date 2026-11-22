"""성공↔실패 전이 알림(--notify-transition) 고정.
가짜 urlopen 만 쓰며 네트워크·실제 자격증명·state.json 은 쓰지 않는다."""
import contextlib
import io
import json
import os
from pathlib import Path
import runpy
import socket
import subprocess
import sys
import urllib.error
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
FAKE_API_TOKEN = "".join(["synthetic", "-api-", "placeholder"])


class FakeResponse:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode("utf-8")

    def read(self, limit=-1):
        assert limit == nt.MAX_RESPONSE_BYTES
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
        self.history_urls = []
        self.history_auth = []
        self.telegram_headers = []
        self.sent = []

    def __call__(self, request, timeout=None):
        assert timeout == 10
        url = request.full_url
        if url.startswith("https://api.github.com/"):
            self.history_calls += 1
            self.history_urls.append(url)
            assert request.data is None
            self.history_auth.append(request.get_header("Authorization"))
            # 리다이렉트로 넘어가지 않도록 unredirected 헤더로만 넣는다.
            assert "Authorization" not in request.headers
            if self.history_error:
                raise self.history_error
            return FakeResponse({"total_count": len(self.runs), "workflow_runs": self.runs})
        if url.startswith("https://api.telegram.org/"):
            if self.send_error:
                raise self.send_error
            from urllib.parse import parse_qs
            self.telegram_headers.append(dict(request.header_items()))
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

    def test_http_status_code_only_in_history_reason(self):
        for code in (403, 429):
            with self.subTest(code=code):
                error = urllib.error.HTTPError(nt.RUNS_URL, code, "rate limit body text", {}, None)
                fake = FakeUrlopen(history_error=error)
                out, err = self.run_notify(env("failure", "failure"), fake)
                self.assertEqual(out.strip(), f"notify: skipped (history unavailable (http {code}))")
                self.assertNotIn("rate limit", out + err)
                self.assertNotIn("api.github.com", out + err)
                self.assertEqual(fake.sent, [])

    def test_history_query_is_filtered_by_branch(self):
        fake = FakeUrlopen(("success",))
        self.run_notify(env("failure", "failure"), fake)
        self.assertTrue(fake.history_urls[0].endswith("&branch=main"))
        fake = FakeUrlopen(("success",))
        self.run_notify(env("failure", "failure", GITHUB_REF_NAME="feat/a b&c"), fake)
        self.assertTrue(fake.history_urls[0].endswith("&branch=feat%2Fa%20b%26c"))
        self.assertIn("status=completed", fake.history_urls[0])

    def test_previous_startup_failure_counts_as_failure(self):
        fake = FakeUrlopen(("startup_failure",))
        out, _ = self.run_notify(env("success", "success"), fake)
        self.assertEqual(out.strip(), "notify: sent (recovery)")

    def test_unknown_or_empty_outcome_is_distinct_from_cancelled(self):
        for outcome, job in (("", "failure"), ("weird", "success"), ("success", ""), ("failure", "odd")):
            with self.subTest(outcome=outcome, job=job):
                fake = FakeUrlopen(("success",))
                out, _ = self.run_notify(env(outcome, job), fake)
                self.assertEqual(out.strip(), "notify: skipped (unknown outcome)")
                self.assertEqual(fake.history_calls, 0)
                self.assertEqual(fake.sent, [])

    def test_api_token_only_on_github_request(self):
        fake = FakeUrlopen(("success",))
        out, err = self.run_notify(env("failure", "failure", GH_API_TOKEN=FAKE_API_TOKEN), fake)
        self.assertEqual(out.strip(), "notify: sent (failure)")
        self.assertEqual(fake.history_auth, [f"Bearer {FAKE_API_TOKEN}"])
        self.assertEqual(len(fake.telegram_headers), 1)
        self.assertNotIn(FAKE_API_TOKEN, repr(fake.telegram_headers) + fake.sent[0] + out + err)

    def test_without_api_token_history_is_anonymous(self):
        fake = FakeUrlopen(("success",))
        self.run_notify(env("failure", "failure"), fake)
        self.assertEqual(fake.history_auth, [None])

    def test_default_opener_does_not_follow_redirects(self):
        handlers = nt._OPENER.handlers
        self.assertTrue(any(isinstance(h, nt._NoRedirect) for h in handlers))
        self.assertFalse(any(type(h) is nt.urllib.request.HTTPRedirectHandler for h in handlers))
        request = nt.urllib.request.Request("https://api.github.com/x")
        request.add_unredirected_header("Authorization", "Bearer " + FAKE_API_TOKEN)
        self.assertIsNone(nt._NoRedirect().redirect_request(
            request, None, 302, "Found", {}, "https://elsewhere.invalid/"))

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
            with patch.object(sys, "argv", ["monitor.py", "--notify-transition"]),                  patch.dict(os.environ, env("failure", "failure"), clear=True),                  patch.object(nt, "_OPENER", type("FakeOpener", (), {"open": staticmethod(fake)})()),                  patch.object(socket, "socket", side_effect=AssertionError("network forbidden")),                  contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as exit_info:
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

    def test_isolated_subprocess_cancelled_path(self):
        # 워크플로와 같은 `-B -S`(스크립트 폴더가 sys.path 에 있어야 하므로 -I 는 쓰지 않음),
        # PYTHON* 등 환경 변수는 넘기지 않는다. 취소 경로라 네트워크를 쓰지 않는다.
        child_env = {k: v for k, v in os.environ.items()
                     if k.upper() in {"SYSTEMROOT", "PATH", "TEMP", "TMP"}}
        child_env.update(MONITOR_OUTCOME="cancelled", JOB_STATUS="cancelled",
                         TELEGRAM_BOT_TOKEN=FAKE_TOKEN, TELEGRAM_CHAT_ID=FAKE_CHAT)
        done = subprocess.run([sys.executable, "-S", "-B", "-X", "utf8", str(ROOT / "monitor.py"), "--notify-transition"],
                              cwd=str(ROOT), env=child_env, capture_output=True, text=True,
                              encoding="utf-8", timeout=30)
        self.assertEqual(done.returncode, 0)
        self.assertEqual(done.stdout.strip(), "notify: skipped (cancelled)")
        self.assertEqual(done.stderr, "")

    def test_workflow_step_contract(self):
        text = (ROOT / ".github/workflows/kimp-monitor.yml").read_text(encoding="utf-8")
        step = text.split("      - name: Notify on transition", 1)[1]
        self.assertIn("if: ${{ always() }}", step)
        self.assertIn("continue-on-error: true", step)
        self.assertIn("MONITOR_OUTCOME: ${{ steps.monitor.outcome }}", step)
        self.assertIn("JOB_STATUS: ${{ job.status }}", step)
        self.assertIn("python -B -S monitor.py --notify-transition", step)
        self.assertIn("GH_API_TOKEN: ${{ secrets.GITHUB_TOKEN }}", step)
        run_line = [line for line in step.splitlines() if line.strip().startswith("run:")]
        self.assertEqual(len(run_line), 1)
        self.assertNotIn("${{", run_line[0])


if __name__ == "__main__":
    unittest.main()
