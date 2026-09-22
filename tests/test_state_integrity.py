"""Synthetic state/public-log regression; real prices, Git and messages forbidden."""
import contextlib
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import unittest
from unittest.mock import Mock, patch
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import monitor


class StateIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.folder = ROOT / ("synthetic-state-" + uuid.uuid4().hex)
        self.folder.mkdir()
        self.addCleanup(shutil.rmtree, self.folder)
        self.path = self.folder / "state.json"
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.output = self.stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        for target, name in ((socket, "socket"), (subprocess, "run"),
                             (monitor, "_requests"), (monitor, "_ticker"),
                             (monitor, "send_telegram")):
            self.stack.enter_context(patch.object(target, name, side_effect=AssertionError("external effect")))

    def test_duplicate_keys_at_any_depth_preserve_original_bytes(self):
        for text in (
            '{"history":[],"last_alert":{"usdt_low":{"value":-1,"time":"2026-01-01"}},"last_alert":{}}',
            '{"history":[],"last_alert":{},"health":{"fx":{"status":"ok","status":"error"}}}',
            '{"history":[],"last_alert":{},"legacy":{"unknown":1,"unknown":2}}',
        ):
            with self.subTest(text=text):
                self.path.write_text(text, encoding="utf-8")
                before = self.path.read_bytes()
                with self.assertRaises(monitor.StateError):
                    monitor.load_state(self.path)
                self.assertEqual(self.path.read_bytes(), before)

    def test_tiny_positive_prices_roundtrip_without_zero_or_new_lower_bound(self):
        state = {"history": [], "last_alert": {}, "legacy": {"keep": "unchanged"}}
        monitor.add_history(state, 0, 0, datetime(2026, 1, 1, tzinfo=timezone.utc),
                            usd_krw=1e-10, intl_gold_usd_oz=1e-10, krx_gold_krw_g=1e-10)
        monitor.save_state(state, state_file=self.path)
        actual = monitor.load_state(self.path)
        self.assertEqual(actual, state)
        for name in ("usd_krw", "intl_gold_usd_oz", "krx_gold_krw_g"):
            self.assertEqual(actual["history"][0][name], 1e-10)

    def test_short_or_wrong_candidate_is_rejected_before_replace(self):
        old = b'{"history":[],"last_alert":{},"old":true}\n'
        original = monitor.tempfile.NamedTemporaryFile
        for change in (lambda text: text[:-1], lambda text: text.replace('"new"', '"bad"')):
            with self.subTest(change=change):
                self.path.write_bytes(old)

                class CorruptWriter:
                    def __init__(self, *args, **kwargs):
                        self.file = original(*args, **kwargs)
                    def __enter__(self):
                        self.file.__enter__()
                        return self
                    def __exit__(self, *args):
                        return self.file.__exit__(*args)
                    def __getattr__(self, name):
                        return getattr(self.file, name)
                    def write(self, text):
                        self.file.write(change(text))
                        return len(text)

                with patch.object(monitor.tempfile, "NamedTemporaryFile", CorruptWriter), \
                     patch.object(monitor.os, "replace", side_effect=AssertionError("must not replace")), \
                     self.assertRaises(monitor.StateError):
                    monitor.save_state({"history": [], "last_alert": {}, "new": True}, state_file=self.path)
                self.assertEqual(self.path.read_bytes(), old)
                self.assertEqual(list(self.folder.glob(".state-*.tmp")), [])

    def test_all_provider_fallback_errors_redact_raw_exception(self):
        secret = "SYNTHETIC_PRIVATE_URL\nhttps://example.invalid/token"
        for function in (monitor.get_usd_krw_rate, monitor.get_krx_gold_price_per_gram,
                         monitor.get_international_gold_usd_per_oz):
            with self.subTest(function=function.__name__), \
                 patch.object(monitor, "_requests", side_effect=RuntimeError(secret)), \
                 patch.object(monitor, "_ticker", side_effect=RuntimeError(secret)):
                with self.assertRaises(RuntimeError):
                    function()
        self.assertNotIn("SYNTHETIC_PRIVATE_URL", self.output.getvalue())
        self.assertNotIn("https://example.invalid", self.output.getvalue())
        self.assertIn("RuntimeError", self.output.getvalue())

    def test_redacted_failure_keeps_existing_fallback_order_and_value(self):
        client = Mock(get=Mock(side_effect=RuntimeError("SYNTHETIC_PRIVATE_URL")))
        ticker = Mock(fast_info=Mock(last_price=1400))
        with patch.object(monitor, "_requests", return_value=client), \
             patch.object(monitor, "_ticker", return_value=ticker) as yahoo:
            self.assertEqual(float(monitor.get_usd_krw_rate()), 1400)
        yahoo.assert_called_once_with("KRW=X")
        self.assertNotIn("SYNTHETIC_PRIVATE_URL", self.output.getvalue())


if __name__ == "__main__":
    unittest.main()
