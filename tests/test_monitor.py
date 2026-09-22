"""Offline regressions: synthetic prices/state, no credentials, network or real Git."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import socket
import shutil
import subprocess
import sys
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
with patch.object(os, "environ", {}):
    import monitor as m

NOW = datetime(2026, 9, 22, 12, tzinfo=timezone(timedelta(hours=9)))


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        # Windows 3.12's mode-0700 TemporaryDirectory ACL excludes some sandbox
        # identities. A normal private-to-this-test directory inherits workspace ACLs.
        directory = ROOT / ("kimchi-test-" + uuid.uuid4().hex)
        directory.mkdir()
        self.assertEqual(directory.parent, ROOT)
        self.stack.callback(shutil.rmtree, directory)
        self.state_file = directory / "synthetic-state.json"
        self.output = self.stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        self.stack.enter_context(patch.object(os, "environ", {}))
        self.stack.enter_context(patch.object(socket, "socket", side_effect=AssertionError("network forbidden")))
        self.git = self.stack.enter_context(patch.object(subprocess, "run", side_effect=AssertionError("Git forbidden")))
        self.stack.enter_context(patch.object(m, "_requests", side_effect=AssertionError("HTTP forbidden")))
        self.stack.enter_context(patch.object(m, "_ticker", side_effect=AssertionError("Yahoo forbidden")))
        m.configure({})

    def state(self):
        return json.loads(self.state_file.read_text(encoding="utf-8"))

    def write_state(self, state):
        self.state_file.write_text(json.dumps(state), encoding="utf-8")

    def run_prices(self, *, fx=1400, usdt=1470, gold_premium=5, gold=3000,
                   krx=None, delivered=True, now=NOW, **options):
        if krx is None:
            krx = (3000 * 1400 / m.TROY_OUNCE_TO_GRAM) * (1 + gold_premium / 100)
        with contextlib.ExitStack() as stack:
            readers = []
            for name, value in (("get_usd_krw_rate", fx), ("get_upbit_usdt_price", usdt),
                                ("get_krx_gold_price_per_gram", krx),
                                ("get_international_gold_usd_per_oz", gold)):
                kwargs = {"side_effect": value} if isinstance(value, Exception) else {"return_value": value}
                readers.append(stack.enter_context(patch.object(m, name, **kwargs)))
            kwargs = {"side_effect": delivered} if isinstance(delivered, list) else {"return_value": delivered}
            sender = stack.enter_context(patch.object(m, "send_telegram", **kwargs))
            settings = dict(send_alerts=True, persist_state=True, state_file=self.state_file, now=now)
            settings.update(options)
            code = m.run_monitor(**settings)
        return code, sender, readers

    def test_import_does_not_read_environment_or_load_network_clients(self):
        class NoEnvironment(dict):
            def get(self, *args):
                raise AssertionError("environment read at import")
        spec = importlib.util.spec_from_file_location("monitor_offline_import", ROOT / "monitor.py")
        module = importlib.util.module_from_spec(spec)
        with patch.object(os, "environ", NoEnvironment()):
            spec.loader.exec_module(module)
        self.assertEqual(module.TELEGRAM_BOT_TOKEN, "")
        self.assertFalse(hasattr(module, "requests"))
        self.assertFalse(hasattr(module, "yf"))

    def test_cli_default_does_no_work(self):
        with patch.object(m, "run_monitor", side_effect=AssertionError("unexpected execution")), \
             patch.object(m, "configure", side_effect=AssertionError("unexpected configuration read")):
            self.assertEqual(m.main([]), 0)
        self.git.assert_not_called()

    def test_invalid_configuration_fails_before_state_or_network(self):
        for settings in ({"USDT_KIMP_LOW": "nan"}, {"USDT_KIMP_HIGH": "infinity"},
                         {"GOLD_KIMP_LOW": "oops"}, {"GOLD_KIMP_LOW": "10"},
                         {"USDT_KIMP_LOW": "11"}):
            with self.subTest(settings=settings), patch.object(m, "run_monitor") as run:
                self.assertEqual(m.main(["--live"], environ=settings), 1)
                run.assert_not_called()
        self.assertEqual(m.USDT_KIMP_LOW, 0)

    def test_publish_requires_explicit_live_repository_path(self):
        for arguments in (["--publish-state"], ["--dry-run", "--publish-state"],
                          ["--live", "--publish-state", "--state-file", str(self.state_file)]):
            with self.subTest(arguments=arguments), contextlib.redirect_stderr(io.StringIO()), \
                 self.assertRaises(SystemExit) as error:
                m.main(arguments, environ={})
            self.assertEqual(error.exception.code, 2)

    def test_cli_live_does_not_publish_implicitly(self):
        with patch.object(m, "run_monitor", return_value=0) as run:
            self.assertEqual(m.main(["--live", "--state-file", str(self.state_file)], environ={}), 0)
        self.assertFalse(run.call_args.kwargs["publish"])
        self.assertTrue(run.call_args.kwargs["send_alerts"])

    def test_failed_delivery_retries_same_condition_then_suppresses(self):
        code, sender, _ = self.run_prices(usdt=1260, delivered=False)
        self.assertEqual(code, 1)
        self.assertEqual(sender.call_count, 1)
        self.assertEqual(self.state()["last_alert"], {})
        code, sender, _ = self.run_prices(usdt=1260, delivered=True)
        self.assertEqual(code, 0)
        self.assertEqual(sender.call_count, 1)
        self.assertEqual(self.state()["last_alert"]["usdt_low"]["value"], -10)
        code, sender, _ = self.run_prices(usdt=1260)
        self.assertEqual(code, 0)
        sender.assert_not_called()

    def test_mixed_delivery_consumes_only_confirmed_message(self):
        code, sender, _ = self.run_prices(usdt=1260, gold_premium=-2.2, delivered=[True, False])
        self.assertEqual(code, 1)
        self.assertEqual(sender.call_count, 2)
        self.assertEqual(set(self.state()["last_alert"]), {"usdt_low"})
        code, sender, _ = self.run_prices(usdt=1260, gold_premium=-2.2)
        self.assertEqual(code, 0)
        self.assertEqual(sender.call_count, 1)
        self.assertEqual(set(self.state()["last_alert"]), {"usdt_low", "gold_low"})

    def test_repeated_nonround_usdt_value_does_not_realert(self):
        for price in (1301, 1601):
            with self.subTest(price=price):
                self.write_state({"history": [], "last_alert": {}})
                self.run_prices(usdt=price)
                code, sender, _ = self.run_prices(usdt=price)
                self.assertEqual(code, 0)
                sender.assert_not_called()

    def test_valid_opposite_observation_resets_previous_band_even_if_delivery_fails(self):
        for first, opposite, reentry, key in ((1330, 1554, 1344, "usdt_low"),
                                            (1680, 1330, 1666, "usdt_high")):
            with self.subTest(key=key):
                self.write_state({"history": [], "last_alert": {}})
                self.run_prices(usdt=first)
                self.run_prices(usdt=opposite, delivered=False)
                self.assertEqual(self.state()["last_alert"], {})
                code, sender, _ = self.run_prices(usdt=reentry)
                self.assertEqual(code, 0)
                self.assertEqual(sender.call_count, 1)
                self.assertIn(key, self.state()["last_alert"])

    def test_gold_opposite_failed_delivery_does_not_suppress_reentry(self):
        for first, opposite, reentry, key in ((-3.2, 12.2, -2.2, "gold_low"),
                                            (13.2, -2.2, 12.2, "gold_high")):
            with self.subTest(key=key):
                self.write_state({"history": [], "last_alert": {}})
                self.run_prices(gold_premium=first)
                self.run_prices(gold_premium=opposite, delivered=False)
                self.assertEqual(self.state()["last_alert"], {})
                _, sender, _ = self.run_prices(gold_premium=reentry)
                self.assertEqual(sender.call_count, 1)
                self.assertIn(key, self.state()["last_alert"])

    def test_same_direction_delivery_failure_preserves_confirmed_value(self):
        self.run_prices(usdt=1330)
        original = self.state()["last_alert"]
        self.run_prices(usdt=1260, delivered=False)
        self.assertEqual(self.state()["last_alert"], original)

    def test_gold_highest_level_and_normal_reset_policy_is_unchanged(self):
        self.run_prices(gold_premium=-3.2)
        original = self.state()["last_alert"]["gold_low"]
        for premium in (-0.2, -2.2, -3.3):
            with self.subTest(premium=premium):
                code, sender, _ = self.run_prices(gold_premium=premium)
                self.assertEqual(code, 0)
                sender.assert_not_called()
                self.assertEqual(self.state()["last_alert"]["gold_low"], original)
        self.run_prices(gold_premium=5)
        self.assertEqual(self.state()["last_alert"], {})
        _, sender, _ = self.run_prices(gold_premium=-2.2)
        self.assertEqual(sender.call_count, 1)

    def test_gold_high_direction_delivery_and_level(self):
        self.run_prices(gold_premium=12.2, delivered=False)
        self.assertEqual(self.state()["last_alert"], {})
        self.run_prices(gold_premium=12.2)
        self.assertEqual(self.state()["last_alert"]["gold_high"]["step_level"], 2)
        _, sender, _ = self.run_prices(gold_premium=11.2)
        sender.assert_not_called()

    def test_normal_prices_clear_alerts(self):
        self.run_prices(usdt=1260, gold_premium=-2.2)
        self.run_prices()
        self.assertEqual(self.state()["last_alert"], {})

    def test_nan_fx_preserves_alerts_and_records_failure(self):
        self.run_prices(usdt=1260, gold_premium=-2.2)
        original = self.state()["last_alert"]
        code, _, readers = self.run_prices(fx=float("nan"))
        self.assertEqual(code, 1)
        self.assertEqual(self.state()["last_alert"], original)
        self.assertEqual(self.state()["run"]["failed_sources"], ["fx"])
        self.assertEqual(self.state()["health"]["fx"]["last_success"], NOW.isoformat())
        for reader in readers[1:]:
            reader.assert_not_called()
        self.assertNotIn("NaN", self.state_file.read_text())

    def test_invalid_asset_inputs_preserve_alerts_and_persist_null_history(self):
        for invalid in (float("nan"), float("inf"), -1, 0, True):
            with self.subTest(invalid=invalid):
                self.run_prices(usdt=1260, gold_premium=-2.2)
                original = self.state()["last_alert"]
                code, _, _ = self.run_prices(usdt=invalid, krx=invalid)
                state = self.state()
                self.assertEqual(code, 1)
                self.assertEqual(state["last_alert"], original)
                self.assertEqual(state["run"]["status"], "failed")
                self.assertEqual(state["history"][-1]["usdt_kimp"], None)
                self.assertEqual(state["history"][-1]["gold_kimp"], None)

    def test_partial_failure_keeps_other_asset_working(self):
        code, sender, _ = self.run_prices(usdt=RuntimeError("synthetic"), gold_premium=-2.2)
        state = self.state()
        self.assertEqual(code, 1)
        self.assertEqual(sender.call_count, 1)
        self.assertEqual(state["run"]["status"], "partial_failure")
        self.assertEqual(state["health"]["gold"]["status"], "ok")
        self.assertIn("gold_low", state["last_alert"])

    def test_diagnostic_warning_after_three_failures_suppressed_until_recovery(self):
        for count in range(1, 5):
            _, sender, _ = self.run_prices(usdt=RuntimeError("synthetic"))
            self.assertEqual(sender.call_count, int(count == 3))
            self.assertEqual(self.state()["health"]["usdt"]["consecutive_failures"], count)
        self.run_prices()
        self.assertNotIn("last_warning", self.state()["health"]["usdt"])
        for _ in range(3):
            _, sender, _ = self.run_prices(usdt=RuntimeError("synthetic"))
        self.assertEqual(sender.call_count, 1)

    def test_failed_diagnostic_warning_is_retried(self):
        for _ in range(3):
            self.run_prices(gold=RuntimeError("synthetic"), delivered=False)
        self.assertNotIn("last_warning", self.state()["health"]["gold"])
        _, sender, _ = self.run_prices(gold=RuntimeError("synthetic"))
        self.assertEqual(sender.call_count, 1)
        self.assertIn("last_warning", self.state()["health"]["gold"])

    def test_derived_processing_failure_keeps_failure_streak(self):
        with patch.object(m, "analyze_gold_kimp_driver", side_effect=ValueError("synthetic")):
            for count in range(1, 4):
                code, sender, _ = self.run_prices()
                self.assertEqual(code, 1)
                self.assertEqual(self.state()["health"]["gold"]["consecutive_failures"], count)
                self.assertEqual(sender.call_count, int(count == 3))

    def test_fx_warning_is_once_per_failure_episode(self):
        for count in range(3):
            code, sender, _ = self.run_prices(fx=RuntimeError("synthetic"))
            self.assertEqual(code, 1)
            self.assertEqual(sender.call_count, int(count == 0))

    def test_manual_report_has_no_threshold_state_effect(self):
        code, sender, _ = self.run_prices(run_mode="workflow_dispatch")
        self.assertEqual(code, 0)
        self.assertEqual(sender.call_count, 1)
        self.assertEqual(self.state()["last_alert"], {})

    def test_dry_run_neither_sends_nor_saves(self):
        self.run_prices()
        before = self.state_file.read_bytes()
        code, sender, _ = self.run_prices(usdt=1260, send_alerts=False, persist_state=False)
        self.assertEqual(code, 0)
        sender.assert_not_called()
        self.assertEqual(self.state_file.read_bytes(), before)
        self.git.assert_not_called()

    def test_missing_state_starts_empty(self):
        self.assertEqual(m.load_state(self.state_file), {"history": [], "last_alert": {}})

    def test_legacy_state_without_health_or_gold_step_is_accepted(self):
        state = {"history": [{"time": NOW.isoformat(), "usdt_kimp": None, "gold_kimp": -1,
                               "usd_krw": 1400, "intl_gold_usd_oz": 3000, "krx_gold_krw_g": None}],
                 "last_alert": {"gold_low": {"value": -1, "time": NOW.isoformat()}}}
        self.write_state(state)
        self.assertEqual(m.load_state(self.state_file), state)
        code, sender, _ = self.run_prices(gold_premium=-2.2)
        self.assertEqual(code, 0)
        self.assertEqual(sender.call_count, 1)
        self.assertEqual(self.state()["last_alert"]["gold_low"]["step_level"], 2)

    def test_malformed_state_is_preserved_and_stops_before_network(self):
        invalid_states = ["{", "[]", '{}', '{"history":[],"last_alert":null}',
                          '{"history":[],"last_alert":{},"bad":NaN}',
                          '{"history":[],"last_alert":{"usdt_low":{"value":-1}}}',
                          '{"history":[],"last_alert":{},"health":{"usdt":{}}}']
        for raw in invalid_states:
            with self.subTest(raw=raw):
                self.state_file.write_text(raw, encoding="utf-8")
                code, sender, readers = self.run_prices()
                self.assertEqual(code, 1)
                self.assertEqual(self.state_file.read_text(encoding="utf-8"), raw)
                sender.assert_not_called()
                for reader in readers:
                    reader.assert_not_called()

    def test_state_read_error_is_not_new_state(self):
        with patch("builtins.open", side_effect=PermissionError("synthetic")), self.assertRaises(m.StateError):
            m.load_state(self.state_file)

    def test_atomic_save_replaces_only_after_valid_complete_payload(self):
        self.run_prices()
        before = self.state_file.read_bytes()
        state = self.state()
        real_replace = os.replace
        observed = []

        def check_replace(source, destination):
            self.assertEqual(self.state_file.read_bytes(), before)
            self.assertEqual(Path(source).parent, self.state_file.parent)
            self.assertEqual(json.loads(Path(source).read_text(encoding="utf-8")), state)
            observed.append(True)
            real_replace(source, destination)

        with patch.object(os, "replace", side_effect=check_replace):
            m.save_state(state, state_file=self.state_file)
        self.assertTrue(observed)
        self.assertEqual(list(self.state_file.parent.glob(".state-*.tmp")), [])
        self.git.assert_not_called()

    def test_nonfinite_or_unserializable_state_never_overwrites_previous(self):
        self.run_prices()
        before = self.state_file.read_bytes()
        for value in (float("nan"), float("inf"), object()):
            with self.subTest(value=type(value).__name__):
                state = self.state()
                state["invalid"] = value
                with self.assertRaises(m.StateError):
                    m.save_state(state, state_file=self.state_file)
                self.assertEqual(self.state_file.read_bytes(), before)

    def test_atomic_replace_failure_preserves_old_file_and_cleans_temporary(self):
        self.run_prices()
        before = self.state_file.read_bytes()
        with patch.object(os, "replace", side_effect=PermissionError("synthetic")), self.assertRaises(m.StateError):
            m.save_state(self.state(), state_file=self.state_file)
        self.assertEqual(self.state_file.read_bytes(), before)
        self.assertEqual(list(self.state_file.parent.glob(".state-*.tmp")), [])

    def test_flush_failure_preserves_old_state_and_cleans_temporary(self):
        self.run_prices()
        before = self.state_file.read_bytes()
        with patch.object(os, "fsync", side_effect=OSError("synthetic")), self.assertRaises(m.StateError):
            m.save_state(self.state(), state_file=self.state_file)
        self.assertEqual(self.state_file.read_bytes(), before)
        self.assertEqual(list(self.state_file.parent.glob(".state-*.tmp")), [])

    def test_save_error_makes_run_fail(self):
        with patch.object(m, "save_state", side_effect=m.StateError("synthetic")):
            code, _, _ = self.run_prices()
        self.assertEqual(code, 1)

    def test_history_bound_is_preserved(self):
        for count in range(12):
            self.run_prices(now=NOW + timedelta(minutes=count))
        self.assertEqual(len(self.state()["history"]), 10)
        self.assertEqual(self.state()["history"][0]["time"], (NOW + timedelta(minutes=2)).isoformat())

    def test_calculation_rejects_nonfinite_nonpositive_and_overflow(self):
        for value in (float("nan"), float("inf"), -1, 0, True, None):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    m.calc_usdt_kimp(value, 1400)
                with self.assertRaises(ValueError):
                    m.calc_usdt_kimp(1400, value)
                with self.assertRaises(ValueError):
                    m.calc_gold_kimp(140000, 3000, value)
                with self.assertRaises(ValueError):
                    m.calc_gold_kimp(value, 3000, 1400)
        for value in (float("nan"), 999, 10000, 1e308):
            with self.subTest(gold=value), self.assertRaises(ValueError):
                m.calc_gold_kimp(140000, value, 1400)
        with self.assertRaises(ValueError):
            m.calc_usdt_kimp(1e308, 1e-308)
        with self.assertRaises(ValueError):
            m.calc_gold_kimp(140000, 3000, 1e308)

    def test_upbit_validates_response(self):
        for value in ("NaN", 0, -1, True, "Infinity"):
            response = Mock()
            response.json.return_value = [{"trade_price": value}]
            with self.subTest(value=value), patch.object(m, "_requests", return_value=Mock(get=Mock(return_value=response))), \
                 self.assertRaises(ValueError):
                m.get_upbit_usdt_price()

    def test_fx_invalid_fallbacks_continue_to_valid_source(self):
        naver = Mock()
        naver.json.return_value = {"isSuccess": True,
                                   "result": [{"localTradedAt": "2026-09-22", "closePrice": "NaN"}]}
        er_api = Mock()
        er_api.json.return_value = {"rates": {"KRW": 1400}}
        client = Mock(get=Mock(side_effect=[naver, er_api]))
        with patch.object(m, "_requests", return_value=client), \
             patch.object(m, "_ticker", return_value=SimpleNamespace(fast_info=SimpleNamespace(last_price=float("inf")))):
            self.assertEqual(m.get_usd_krw_rate(), 1400)
        self.assertEqual(client.get.call_count, 2)

    def test_krx_invalid_api_uses_desktop_fallback(self):
        first = Mock()
        first.json.return_value = {"closePrice": "0"}
        second = Mock(text="140,000 원/g")
        with patch.object(m, "_requests", return_value=Mock(get=Mock(side_effect=[first, second]))):
            self.assertEqual(m.get_krx_gold_price_per_gram(), 140000)

    def test_gold_invalid_quote_and_fast_price_use_history(self):
        response = Mock()
        response.json.return_value = [{"spreadProfilePrices": [{"bid": -1, "ask": 6001}]}]
        history = Mock(empty=False)
        history.__getitem__ = Mock(return_value=SimpleNamespace(iloc=[3000]))
        ticker = Mock(fast_info=SimpleNamespace(last_price=float("nan")))
        ticker.history.return_value = history
        with patch.object(m, "_requests", return_value=Mock(get=Mock(return_value=response))), \
             patch.object(m, "_ticker", return_value=ticker):
            self.assertEqual(m.get_international_gold_usd_per_oz(), 3000)
        ticker.history.assert_called_once_with(period="1d")

    def test_telegram_requires_http_and_api_success(self):
        m.configure({"TELEGRAM_BOT_TOKEN": "synthetic-token", "TELEGRAM_CHAT_ID": "synthetic-chat"})
        for http_ok, payload, expected in ((True, {"ok": True}, True), (True, {"ok": False}, False),
                                           (True, {}, False), (False, {"ok": True}, False)):
            response = Mock(ok=http_ok, status_code=200 if http_ok else 500)
            response.json.return_value = payload
            with self.subTest(http_ok=http_ok, payload=payload), \
                 patch.object(m, "_requests", return_value=Mock(post=Mock(return_value=response))):
                self.assertIs(m.send_telegram("synthetic message"), expected)

    def test_telegram_missing_credentials_and_exception_are_false_and_redacted(self):
        self.assertIs(m.send_telegram("synthetic"), False)
        m.configure({"TELEGRAM_BOT_TOKEN": "synthetic-token", "TELEGRAM_CHAT_ID": "synthetic-chat"})
        with patch.object(m, "_requests", side_effect=RuntimeError("https://secret/synthetic-token")):
            self.assertIs(m.send_telegram("synthetic"), False)
        self.assertNotIn("synthetic-token", self.output.getvalue())

    def test_git_each_stage_failure_is_observable(self):
        from test_publish_retry import FakeGit
        for operation in ("symbolic-ref", "for-each-ref", "rev-parse", "rev-list",
                          "diff", "ls-files", "commit", "show", "diff-tree", "push"):
            with self.subTest(operation=operation):
                fake = FakeGit()
                fake.changed = True
                fake.failures[operation] = 2
                with patch.object(subprocess, "run", side_effect=fake), self.assertRaises(m.StateError):
                    m.publish_state(m.STATE_FILE)
                self.assertNotIn("synthetic-secret", self.output.getvalue())

    def test_git_retry_is_bounded_and_never_rebases_or_forces(self):
        from test_publish_retry import FakeGit
        fake = FakeGit()
        fake.changed = True
        fake.push_results = [2, 0]
        with patch.object(subprocess, "run", side_effect=fake):
            m.publish_state(m.STATE_FILE)
        self.assertEqual(len(fake.pushes), 2)
        self.assertEqual(fake.pushes[0], fake.pushes[1])
        self.assertEqual(fake.pushes[0], ["push", "--no-follow-tags", "--", "origin",
                                         f"{fake.head}:refs/heads/main"])
        commits = [args for args in fake.calls if "commit" in args]
        self.assertEqual(len(commits), 1)
        self.assertIn("--only", commits[0])
        self.assertFalse(any(args[0] in {"add", "reset", "rebase", "fetch"} for args in fake.calls))

    def test_git_no_change_does_not_commit_or_push(self):
        from test_publish_retry import FakeGit
        fake = FakeGit()
        with patch.object(subprocess, "run", side_effect=fake):
            m.publish_state(m.STATE_FILE)
        self.assertEqual(fake.commit_count, 0)
        self.assertEqual(fake.pushes, [])

    def test_git_timeout_is_visible_without_command_output(self):
        with patch.object(subprocess, "run", side_effect=subprocess.TimeoutExpired("secret-command", 30)), \
             self.assertRaises(m.StateError) as error:
            m.publish_state(m.STATE_FILE)
        self.assertNotIn("secret-command", str(error.exception))


if __name__ == "__main__":
    unittest.main()
