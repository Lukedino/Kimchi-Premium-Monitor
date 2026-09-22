"""Real monitor adapters/run flow, synthetic responses/state and explicit clocks."""
import contextlib
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
with patch.object(os, "environ", {}):
    import monitor as m
from quote_evidence import Quote, capture_quote

UTC = timezone.utc
NOW = datetime(2026, 9, 22, 3, tzinfo=UTC)
OLD = datetime(2026, 9, 18, 2, 59, tzinfo=UTC)


class FakeClock:
    def __init__(self):
        self.stamp = NOW
        self.tick = 100.0

    def wall(self):
        return self.stamp

    def monotonic(self):
        return self.tick

    def advance(self, seconds):
        self.stamp += timedelta(seconds=seconds)
        self.tick += seconds


class Response:
    def __init__(self, data=None, text=""):
        self.data, self.text = data, text

    def raise_for_status(self):
        pass

    def json(self):
        return deepcopy(self.data)


class History:
    empty = False

    def __init__(self, *, index=None, index_error=None):
        self._index = index
        self.index_error = index_error
        self.index_reads = 0

    def __getitem__(self, name):
        if name != "Close":
            raise AssertionError("unexpected column")
        return SimpleNamespace(iloc=[2000.0, 3000.0])

    @property
    def index(self):
        self.index_reads += 1
        if self.index_error:
            raise self.index_error
        return self._index


class ObservabilityIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.output = self.stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        self.stack.enter_context(patch.object(os, "environ", {}))
        self.stack.enter_context(patch.object(socket, "socket", side_effect=AssertionError("network forbidden")))
        self.stack.enter_context(patch.object(subprocess, "run", side_effect=AssertionError("process forbidden")))
        self.clock = FakeClock()
        self.state = {"history": [], "last_alert": {}}
        self.saved = []
        self.sent = []
        self.calls = []
        self.delay = 5
        self.save_delay = 0
        self.delivered = True
        self.fail_routes = set()
        self.fx = 1400.0
        self.usdt = 1470.0
        self.krx = 3000 * self.fx / m.TROY_OUNCE_TO_GRAM * 1.05
        self.upbit_date = "20260918"
        self.upbit_time = "025900"
        self.client = SimpleNamespace(get=Mock(side_effect=self.get))
        self.stack.enter_context(patch.object(m, "_requests", return_value=self.client))
        self.ticker = self.stack.enter_context(patch.object(
            m, "_ticker", side_effect=RuntimeError("SYNTHETIC_SECRET_PROVIDER")))
        self.load = self.stack.enter_context(patch.object(m, "load_state", side_effect=lambda *_: deepcopy(self.state)))
        self.save = self.stack.enter_context(patch.object(m, "save_state", side_effect=self.persist))
        self.stack.enter_context(patch.object(m, "state_execution_lock", side_effect=lambda *_: contextlib.nullcontext("synthetic.json")))
        self.sender = self.stack.enter_context(patch.object(m, "send_telegram", side_effect=self.send))
        m.configure({})

    def get(self, url, **kwargs):
        if "FX_USDKRW" in url:
            route = "naver_fx"
            data = {"isSuccess": True, "result": [{"closePrice": str(self.fx), "localTradedAt": "2026-09-18"}]}
        elif "api.upbit.com" in url:
            route = "upbit"
            data = [{"trade_price": self.usdt, "trade_date": self.upbit_date,
                     "trade_time": self.upbit_time, "timestamp": 9999999999999},
                    {"trade_price": 99999, "trade_date": "20260922", "trade_time": "030000"}]
        elif "api.stock.naver.com" in url:
            route, data = "naver_gold", {"closePrice": str(self.krx)}
        elif "finance.naver.com" in url:
            route, data = "naver_html", None
        elif "swissquote.com" in url:
            route = "swissquote"
            data = [{"spreadProfilePrices": [{"bid": 2999, "ask": 3001}], "timestamp": 9999999999999}]
        elif "open.er-api.com" in url:
            route = "er_api"
            data = {"rates": {"KRW": self.fx}, "time_last_update_unix": int(OLD.timestamp()),
                    "time_next_update_unix": int(NOW.timestamp())}
        else:
            raise AssertionError("unexpected synthetic route")
        self.calls.append(route)
        self.clock.advance(self.delay)
        if route in self.fail_routes:
            raise RuntimeError("SYNTHETIC_SECRET_RESPONSE")
        return Response(data, "123456.75 원/g")

    def send(self, message):
        self.sent.append(message)
        self.clock.advance(7)
        return self.delivered

    def persist(self, state, **options):
        m.validate_state(state)
        # Enforce a real JSON round trip at the persistence boundary.
        self.state = json.loads(json.dumps(state, allow_nan=False))
        self.saved.append(deepcopy(self.state))
        self.clock.advance(self.save_delay)

    def run_monitor(self, **options):
        settings = dict(send_alerts=True, persist_state=True, state_file="synthetic.json",
                        clock=self.clock.wall, monotonic_clock=self.clock.monotonic,
                        run_mode="schedule", run_id="101", run_attempt="1")
        settings.update(options)
        return m.run_monitor(**settings)

    def use_history(self, history):
        class Ticker:
            @property
            def fast_info(self):
                raise RuntimeError("SYNTHETIC_SECRET_FAST")

            def history(self, **kwargs):
                self.kwargs = kwargs
                return history

        self.ticker.side_effect = None
        self.ticker.return_value = Ticker()
        self.fail_routes.add("swissquote")
        return self.ticker.return_value

    def test_upbit_selected_item_binds_price_and_old_time(self):
        quote = m.get_upbit_usdt_price(with_evidence=True, clock=self.clock.wall)
        self.assertIsInstance(quote, Quote)
        self.assertEqual(float(quote), self.usdt)
        self.assertEqual(quote.source_time, OLD)
        self.assertEqual(quote.fetched_at, NOW + timedelta(seconds=5))
        self.assertEqual(self.calls, ["upbit"])
        self.assertEqual(m.get_upbit_usdt_price(clock=self.clock.wall), self.usdt)

    def test_fx_fallback_binds_rate_and_time_to_er_response_only(self):
        self.fail_routes.add("naver_fx")
        quote = m.get_usd_krw_rate(with_evidence=True, clock=self.clock.wall)
        self.assertEqual(quote.source, "er_api")
        self.assertEqual(float(quote), self.fx)
        self.assertEqual(quote.source_time, OLD)
        self.assertEqual(quote.fetched_at, NOW + timedelta(seconds=10))
        self.assertEqual(self.calls, ["naver_fx", "er_api"])
        self.ticker.assert_called_once_with("KRW=X")
        self.assertNotIn("SYNTHETIC_SECRET", self.output.getvalue())

    def test_unverified_source_fields_do_not_gain_fake_instants(self):
        for reader, source in ((m.get_usd_krw_rate, "naver_fx"),
                               (m.get_krx_gold_price_per_gram, "naver_krx_api"),
                               (m.get_international_gold_usd_per_oz, "swissquote")):
            with self.subTest(source=source):
                quote = reader(with_evidence=True, clock=self.clock.wall)
                self.assertEqual(quote.source, source)
                self.assertEqual(quote.time_issue, "unverified_time_contract")
                self.assertIsNone(quote.source_time)
                self.assertIsNone(quote.source_date)

    def test_html_fallback_retains_its_distinct_source(self):
        self.fail_routes.add("naver_gold")
        quote = m.get_krx_gold_price_per_gram(with_evidence=True, clock=self.clock.wall)
        self.assertEqual((float(quote), quote.source), (123456.75, "naver_krx_html"))
        self.assertEqual(self.calls, ["naver_gold", "naver_html"])
        self.assertIsNone(quote.source_time)

    def test_yahoo_history_pairs_last_close_with_its_exchange_date(self):
        exchange_tz = timezone(timedelta(hours=9))
        hist = History(index=[datetime(2026, 9, 17, tzinfo=exchange_tz),
                              datetime(2026, 9, 18, tzinfo=exchange_tz)])
        ticker = self.use_history(hist)
        quote = m.get_international_gold_usd_per_oz(with_evidence=True, clock=self.clock.wall)
        self.assertEqual((float(quote), quote.source_date.isoformat()), (3000, "2026-09-18"))
        self.assertEqual((quote.source, quote.time_precision), ("yahoo_gold_history", "date"))
        self.assertIsNone(quote.source_time)
        self.assertEqual(ticker.kwargs, {"period": "1d"})
        self.assertEqual(hist.index_reads, 1)

    def test_optional_index_failure_does_not_reject_valid_close(self):
        for error in (RuntimeError("SYNTHETIC_SECRET_INDEX"), OSError("SYNTHETIC_SECRET_INDEX")):
            with self.subTest(error=type(error).__name__):
                hist = History(index_error=error)
                self.use_history(hist)
                quote = m.get_international_gold_usd_per_oz(with_evidence=True, clock=self.clock.wall)
                self.assertEqual(float(quote), 3000)
                self.assertEqual(quote.time_issue, "source_time_missing")
                self.assertEqual(hist.index_reads, 1)
                self.assertNotIn("SYNTHETIC_SECRET", self.output.getvalue())

    def test_legacy_float_reader_never_touches_history_metadata(self):
        hist = History(index_error=RuntimeError("index must not be read"))
        self.use_history(hist)
        self.assertEqual(m.get_international_gold_usd_per_oz(clock=self.clock.wall), 3000)
        self.assertEqual(hist.index_reads, 0)

    def test_fast_info_no_extra_history_query_to_manufacture_time(self):
        self.fail_routes.add("swissquote")
        history = Mock(side_effect=AssertionError("additional source query forbidden"))
        self.ticker.side_effect = None
        self.ticker.return_value = SimpleNamespace(fast_info=SimpleNamespace(last_price=3000), history=history)
        quote = m.get_international_gold_usd_per_oz(with_evidence=True, clock=self.clock.wall)
        self.assertEqual(quote.source, "yahoo_gold_fast_info")
        self.assertEqual(quote.time_issue, "unverified_time_contract")
        history.assert_not_called()

    def test_collection_delivery_finish_excludes_later_persistence(self):
        self.save_delay = 100
        self.assertEqual(self.run_monitor(run_mode="workflow_dispatch"), 0)
        run = self.state["run"]
        self.assertEqual(run["started_at"], NOW.isoformat())
        self.assertEqual(run["finished_at"], (NOW + timedelta(seconds=27)).isoformat())
        self.assertEqual(run["duration_seconds"], 27)
        self.assertEqual(run["finished_scope"], "collection_and_delivery")
        self.assertEqual(self.clock.wall(), NOW + timedelta(seconds=127))
        self.assertEqual(len(self.sent), 1)
        for source, seconds in (("fx", 5), ("usdt", 10), ("gold", 20)):
            self.assertEqual(self.state["health"][source]["last_success"],
                             (NOW + timedelta(seconds=seconds)).isoformat())
        self.assertEqual(run["price_evidence"]["usdt"]["source_time"], OLD.isoformat())
        self.assertEqual(run["price_evidence"]["usdt"]["fetched_at"],
                         (NOW + timedelta(seconds=10)).isoformat())

    def test_fx_failure_still_finishes_after_warning_and_stops_other_collectors(self):
        self.fail_routes.update({"naver_fx", "er_api"})
        self.assertEqual(self.run_monitor(), 1)
        run = self.state["run"]
        self.assertEqual((run["status"], run["failed_sources"]), ("failed", ["fx"]))
        self.assertEqual(run["price_evidence"], {})
        self.assertEqual(run["duration_seconds"], 17)
        self.assertEqual(run["finished_at"], (NOW + timedelta(seconds=17)).isoformat())
        self.assertEqual(self.state["health"]["fx"]["last_failure"], (NOW + timedelta(seconds=10)).isoformat())
        self.assertEqual(self.state["health"]["fx"]["last_warning"], run["finished_at"])
        self.assertEqual(self.calls, ["naver_fx", "er_api"])
        self.assertEqual(self.state["history"], [])
        self.assertNotIn("SYNTHETIC_SECRET", json.dumps(self.state) + self.output.getvalue())

    def test_failed_asset_does_not_reuse_prior_run_evidence(self):
        self.assertEqual(self.run_monitor(), 0)
        self.fail_routes.add("upbit")
        self.assertEqual(self.run_monitor(run_id="102"), 1)
        run = self.state["run"]
        self.assertEqual((run["status"], run["failed_sources"]), ("partial_failure", ["usdt"]))
        self.assertNotIn("usdt", run["price_evidence"])
        self.assertIn("international_gold", run["price_evidence"])
        self.assertIsNone(self.state["history"][-1]["usdt_kimp"])
        self.assertEqual(self.state["health"]["usdt"]["consecutive_failures"], 1)
        self.assertEqual(self.sent, [])  # Existing three-failure policy remains.

    def test_failed_delivery_has_finish_but_no_confirmed_alert(self):
        self.usdt = 1260
        self.delivered = False
        self.assertEqual(self.run_monitor(), 1)
        run = self.state["run"]
        self.assertEqual((run["failed_sources"], run["failed_alerts"]), ([], 1))
        self.assertEqual(run["duration_seconds"], 27)
        self.assertEqual(self.state["last_alert"], {})
        self.assertEqual(set(run["price_evidence"]), {"fx", "usdt", "krx_gold", "international_gold"})

    def test_manual_between_scheduled_runs_preserves_observed_baseline(self):
        self.run_monitor()
        first = deepcopy(self.state["scheduled_start"])
        self.clock.stamp = NOW + timedelta(minutes=10)
        self.run_monitor(run_mode="workflow_dispatch", run_id="102")
        self.assertEqual(self.state["scheduled_start"], first)
        self.assertIsNone(self.state["run"]["observed_interval_seconds"])
        self.clock.stamp = NOW + timedelta(minutes=30)
        self.run_monitor(run_id="103")
        self.assertEqual(self.state["run"]["observed_interval_seconds"], 1800)
        self.assertIsNone(self.state["run"]["schedule_delay_seconds"])
        self.assertEqual(self.state["scheduled_start"]["started_at"], (NOW + timedelta(minutes=30)).isoformat())

    def test_scheduled_retry_does_not_replace_prior_start(self):
        self.run_monitor()
        first = deepcopy(self.state["scheduled_start"])
        self.clock.advance(600)
        self.run_monitor(run_attempt="2")
        self.assertEqual(self.state["scheduled_start"], first)
        self.assertIsNone(self.state["run"]["observed_interval_seconds"])
        self.assertIn("scheduled_rerun", self.state["run"]["issues"])

    def test_naive_now_or_clock_rejected_before_read_or_collection(self):
        for options in ({"clock": lambda: NOW.replace(tzinfo=None)}, {"now": NOW.replace(tzinfo=None)}):
            with self.subTest(options=list(options)):
                self.assertEqual(self.run_monitor(**options), 1)
        self.load.assert_not_called()
        self.save.assert_not_called()
        self.assertEqual(self.calls, [])
        self.sender.assert_not_called()

    def test_future_source_evidence_does_not_change_current_health_policy(self):
        self.upbit_date = "20260923"
        self.assertEqual(self.run_monitor(), 0)
        self.assertEqual(self.state["run"]["price_evidence"]["usdt"]["time_issue"], "source_time_future")
        self.assertEqual(self.state["health"]["usdt"]["status"], "ok")
        self.assertEqual(self.sent, [])

    def test_driver_uses_supplied_start_and_legacy_history_is_not_backfilled(self):
        entry = {"time": (NOW - timedelta(hours=1)).isoformat(), "usdt_kimp": 5,
                 "gold_kimp": 5, "usd_krw": 1400, "intl_gold_usd_oz": 3000,
                 "krx_gold_krw_g": self.krx}
        self.state["history"].append(deepcopy(entry))
        self.run_monitor(run_mode="workflow_dispatch")
        self.assertEqual(self.state["history"][0], entry)
        self.assertIn("1.0시간", self.sent[0])
        self.assertNotIn("price_evidence", self.state["history"][0])

    def test_small_valid_prices_survive_json_persistence_without_rounding_to_zero(self):
        self.fx = 0.00001
        self.usdt = 0.0000105
        self.krx = 3000 * self.fx / m.TROY_OUNCE_TO_GRAM * 1.05
        self.assertEqual(self.run_monitor(), 0)
        latest = self.state["history"][-1]
        self.assertEqual(latest["usd_krw"], self.fx)
        self.assertEqual(latest["krx_gold_krw_g"], self.krx)
        self.assertEqual(self.state["run"]["price_evidence"]["usdt"]["value"], self.usdt)
        m.validate_state(self.state)

    def test_quote_wrapper_keeps_identical_calculations_and_alert_decisions(self):
        values = [("get_usd_krw_rate", 1400, "naver_fx"),
                  ("get_upbit_usdt_price", 1260, "upbit"),
                  ("get_krx_gold_price_per_gram", self.krx, "naver_krx_api"),
                  ("get_international_gold_usd_per_oz", 3000, "swissquote")]
        outcomes = []
        for evidence in (False, True):
            self.state = {"history": [], "last_alert": {}}
            self.sent.clear()
            self.clock = FakeClock()
            with contextlib.ExitStack() as stack:
                for name, price, source in values:
                    result = capture_quote(price, source, fetched_at=NOW) if evidence else price
                    stack.enter_context(patch.object(m, name, return_value=result))
                code = self.run_monitor()
            outcomes.append((code, self.state["history"], self.state["last_alert"],
                             self.state["run"]["status"], list(self.sent)))
        self.assertEqual(outcomes[0], outcomes[1])
        self.assertEqual(set(outcomes[1][2]), {"usdt_low"})


if __name__ == "__main__":
    unittest.main()
