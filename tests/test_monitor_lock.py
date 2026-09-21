"""Check that monitor side effects stay inside the shared state lock."""
import contextlib
import io
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import unittest
import uuid
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import monitor as m
from state_lock import StateLockBusy, StateLockError, state_execution_lock


class MonitorLockTests(unittest.TestCase):
    def setUp(self):
        self.directory = ROOT / ("kimchi-lock-integration-" + uuid.uuid4().hex)
        self.directory.mkdir()
        self.addCleanup(shutil.rmtree, self.directory)
        self.path = self.directory / "synthetic-state.json"
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.output = self.stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        self.stack.enter_context(patch.dict(os.environ, {}, clear=True))
        self.stack.enter_context(patch.object(socket, "socket", side_effect=AssertionError("network forbidden")))
        self.stack.enter_context(patch.object(subprocess, "run", side_effect=AssertionError("Git forbidden")))
        self.stack.enter_context(patch.object(m, "_requests", side_effect=AssertionError("HTTP forbidden")))
        self.stack.enter_context(patch.object(m, "_ticker", side_effect=AssertionError("Yahoo forbidden")))
        m.configure({})

    def assert_locked(self):
        with self.assertRaises(StateLockBusy):
            with state_execution_lock(self.path):
                self.fail("Concurrent lock must fail")

    def test_contender_stops_before_state_read_or_side_effects(self):
        with state_execution_lock(self.path), patch.object(m, "_run_monitor") as run:
            self.assertEqual(m.run_monitor(send_alerts=True, persist_state=True, state_file=self.path), 1)
            run.assert_not_called()
        self.assertFalse(self.path.exists())

    def test_each_side_effect_flag_requires_lock(self):
        for flag in ("send_alerts", "persist_state", "publish"):
            with self.subTest(flag=flag), state_execution_lock(self.path), patch.object(m, "_run_monitor") as run:
                self.assertEqual(m.run_monitor(state_file=self.path, **{flag: True}), 1)
                run.assert_not_called()

    def test_dry_run_does_not_create_or_acquire_lock(self):
        with patch.object(m, "state_execution_lock", side_effect=AssertionError("unexpected lock")), \
             patch.object(m, "_run_monitor", return_value=7) as run:
            self.assertEqual(m.run_monitor(state_file=self.path), 7)
            self.assertEqual(run.call_args.kwargs["state_file"], self.path)
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_lock_open_failure_does_not_start_monitor(self):
        with patch.object(m, "state_execution_lock", side_effect=StateLockError("lock unavailable")), \
             patch.object(m, "_run_monitor") as run:
            self.assertEqual(m.run_monitor(persist_state=True, state_file=self.path), 1)
            run.assert_not_called()

    def test_exception_releases_lock(self):
        def fail(**kwargs):
            self.assert_locked()
            raise RuntimeError("synthetic failure")
        with patch.object(m, "_run_monitor", side_effect=fail), self.assertRaises(RuntimeError):
            m.run_monitor(persist_state=True, state_file=self.path)
        with state_execution_lock(self.path):
            pass

    def test_return_code_preserved_and_lock_released(self):
        def fail(**kwargs):
            self.assert_locked()
            return 1
        with patch.object(m, "_run_monitor", side_effect=fail):
            self.assertEqual(m.run_monitor(persist_state=True, state_file=self.path), 1)
        with state_execution_lock(self.path):
            pass

    def test_all_live_phases_and_publish_remain_locked(self):
        seen = []
        def phase(name, value):
            def call(*args, **kwargs):
                self.assert_locked()
                seen.append(name)
                if name == "save":
                    self.assertTrue(kwargs["publish"])
                    self.assertEqual(kwargs["state_file"], self.path.resolve())
                return value
            return call
        with contextlib.ExitStack() as stack:
            for name, label, value in (
                ("load_state", "load", {"history": [], "last_alert": {}}),
                ("get_usd_krw_rate", "fx", 1400),
                ("get_upbit_usdt_price", "usdt", 1260),
                ("get_krx_gold_price_per_gram", "krx", 3000 * 1400 / m.TROY_OUNCE_TO_GRAM * 1.05),
                ("get_international_gold_usd_per_oz", "gold", 3000),
                ("send_telegram", "send", True),
                ("save_state", "save", None),
            ):
                stack.enter_context(patch.object(m, name, side_effect=phase(label, value)))
            self.assertEqual(m.run_monitor(send_alerts=True, persist_state=True, publish=True,
                                           state_file=self.path), 0)
        self.assertEqual(set(seen), {"load", "fx", "usdt", "krx", "gold", "send", "save"})
        self.assertEqual(seen[0], "load")
        self.assertEqual(seen[-1], "save")
        with state_execution_lock(self.path):
            pass


if __name__ == "__main__":
    unittest.main()
