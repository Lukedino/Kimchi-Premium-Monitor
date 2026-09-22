"""Same-response timestamp evidence, with entirely synthetic prices and dates."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from quote_evidence import Quote, SOURCES, capture_quote

UTC = timezone.utc
KST = timezone(timedelta(hours=9))
NOW = datetime(2026, 9, 22, 3, tzinfo=UTC)


class QuoteEvidenceTests(unittest.TestCase):
    def upbit(self, **changes):
        item = {"trade_price": 1400, "trade_date": "20260922", "trade_time": "025900"}
        item.update(changes)
        return item

    def quote(self, item=None, **kwargs):
        return capture_quote(1400, "upbit", fetched_at=NOW,
                             response=self.upbit() if item is None else item, **kwargs)

    def test_old_quote_keeps_old_source_time_despite_new_retrieval(self):
        result = self.quote(self.upbit(trade_date="20260918"))
        self.assertEqual(result.fetched_at, NOW)
        self.assertEqual(result.source_time, datetime(2026, 9, 18, 2, 59, tzinfo=UTC))
        self.assertEqual(result.time_precision, "instant")
        self.assertEqual(result.time_resolution, "second")
        self.assertIsNone(result.time_issue)

    def test_utc_fields_are_not_korean_wall_time(self):
        result = self.quote(self.upbit(trade_date="20260921", trade_time="160000"))
        self.assertEqual(result.source_time.astimezone(KST).date().isoformat(), "2026-09-22")

    def test_fetched_offset_normalizes_to_utc(self):
        result = capture_quote(1400, "upbit", fetched_at=NOW.astimezone(KST), response=self.upbit())
        self.assertEqual(result.to_dict()["fetched_at"], "2026-09-22T03:00:00+00:00")

    def test_missing_time_is_not_response_generation_timestamp_or_http_date(self):
        item = {"trade_price": 1400, "timestamp": 1790046000000,
                "trade_timestamp": 1790046000000, "Date": "SYNTHETIC_SECRET"}
        result = self.quote(item)
        self.assertEqual(result.time_issue, "source_time_missing")
        self.assertIsNone(result.source_time)
        self.assertNotIn("SYNTHETIC_SECRET", json.dumps(result.to_dict()))

    def test_invalid_dates_and_times_keep_the_numeric_price(self):
        for day, clock in (("20260230", "010000"), ("20260922", "250000"),
                           ("2026-09-22", "030000"), ("20260922", "03:00:00"),
                           (20260922, "030000"), ("SYNTHETIC_SECRET\n", {})):
            with self.subTest(day=day, clock=clock):
                result = self.quote(self.upbit(trade_date=day, trade_time=clock))
                self.assertEqual(result.time_issue, "source_time_invalid")
                self.assertEqual(float(result), 1400)
                self.assertNotIn("SYNTHETIC_SECRET", json.dumps(result.to_dict()))

    def test_future_instant_is_an_issue_not_a_price_rejection(self):
        result = self.quote(self.upbit(trade_time="030001"))
        self.assertEqual(result.time_issue, "source_time_future")
        self.assertEqual(float(result), 1400)
        self.assertGreater(result.source_time, result.fetched_at)

    def test_different_response_price_cannot_lend_its_time(self):
        result = self.quote(self.upbit(trade_price=1500))
        self.assertEqual(result.time_issue, "response_evidence_mismatch")
        self.assertIsNone(result.source_time)

    def test_malformed_response_cannot_lend_its_time(self):
        for payload in ({}, {"trade_price": True}, [], "SYNTHETIC_SECRET"):
            with self.subTest(payload=payload):
                result = self.quote(payload)
                self.assertEqual(float(result), 1400)
                self.assertEqual(result.time_precision, "unknown")

    def test_er_api_uses_update_of_same_rate_response(self):
        stamp = datetime(2026, 9, 21, tzinfo=UTC)
        payload = {"rates": {"KRW": 1400}, "time_last_update_unix": int(stamp.timestamp()),
                   "time_next_update_unix": int(NOW.timestamp())}
        result = capture_quote(1400, "er_api", fetched_at=NOW, response=payload)
        self.assertEqual(result.source_time, stamp)

    def test_er_api_rejects_guessed_units_and_bad_types(self):
        for value in (True, "1790035200", 1790035200.0, -1, 10 ** 100):
            with self.subTest(value=value):
                q = capture_quote(1400, "er_api", fetched_at=NOW,
                                  response={"rates": {"KRW": 1400}, "time_last_update_unix": value})
                self.assertEqual(q.time_issue, "source_time_invalid")

    def test_er_api_future_update_preserves_evidence(self):
        q = capture_quote(1400, "er_api", fetched_at=NOW,
                          response={"rates": {"KRW": 1400}, "time_last_update_unix": int(NOW.timestamp()) + 1})
        self.assertEqual(q.time_issue, "source_time_future")

    def test_er_api_missing_time_is_not_replaced_by_next_update(self):
        q = capture_quote(1400, "er_api", fetched_at=NOW,
                          response={"rates": {"KRW": 1400}, "time_next_update_unix": int(NOW.timestamp())})
        self.assertEqual(q.time_issue, "source_time_missing")

    def test_yahoo_daily_index_is_date_only_in_its_own_timezone(self):
        q = capture_quote(3000, "yahoo_gold_history", fetched_at=NOW,
                          history_index=datetime(2026, 9, 22, tzinfo=KST))
        self.assertEqual(q.source_date.isoformat(), "2026-09-22")
        self.assertIsNone(q.source_time)
        self.assertEqual((q.time_precision, q.time_resolution, q.time_issue), ("date", "day", None))

    def test_yahoo_future_date_compares_in_index_zone(self):
        q = capture_quote(3000, "yahoo_gold_history", fetched_at=NOW,
                          history_index=datetime(2026, 9, 23, tzinfo=KST))
        self.assertEqual(q.time_issue, "source_date_future")

    def test_yahoo_local_day_is_not_falsely_future_near_utc_midnight(self):
        q = capture_quote(3000, "yahoo_gold_history",
                          fetched_at=datetime(2026, 9, 21, 16, tzinfo=UTC),
                          history_index=datetime(2026, 9, 22, tzinfo=KST))
        self.assertIsNone(q.time_issue)

    def test_yahoo_naive_or_string_index_is_not_localized(self):
        for value, issue in ((datetime(2026, 9, 21), "source_time_naive"),
                             ("2026-09-21", "source_time_invalid"), (None, "source_time_missing")):
            q = capture_quote(3000, "yahoo_gold_history", fetched_at=NOW, history_index=value)
            self.assertEqual(q.time_issue, issue)
            self.assertIsNone(q.source_date)

    def test_unverified_sources_do_not_parse_plausible_fields(self):
        for source in SOURCES - {"upbit", "er_api", "yahoo_gold_history"}:
            with self.subTest(source=source):
                q = capture_quote(1e-12, source, fetched_at=NOW,
                                  response={"localTradedAt": "2026-09-21", "timestamp": 1790000000000,
                                            "source_time": "SYNTHETIC_SECRET"})
                self.assertEqual(q.time_issue, "unverified_time_contract")
                self.assertEqual(float(q), 1e-12)
                self.assertNotIn("SYNTHETIC_SECRET", json.dumps(q.to_dict()))

    def test_fallback_replaces_value_source_and_evidence_together(self):
        old = capture_quote(1400, "naver_fx", fetched_at=NOW)
        new = capture_quote(1401, "er_api", fetched_at=NOW + timedelta(seconds=10),
                            response={"rates": {"KRW": 1401}, "time_last_update_unix": int(NOW.timestamp()) - 30})
        self.assertEqual((float(old), old.source, old.source_time), (1400, "naver_fx", None))
        self.assertEqual((float(new), new.source), (1401, "er_api"))
        self.assertEqual(new.fetched_at - old.fetched_at, timedelta(seconds=10))

    def test_numeric_calculation_and_threshold_inputs_remain_identical(self):
        value = 1400.123456789
        q = capture_quote(value, "naver_fx", fetched_at=NOW)
        self.assertEqual(float(q), value)
        for usdt in (1200, value, 1600):
            self.assertEqual((usdt / float(q) - 1) * 100, (usdt / value - 1) * 100)

    def test_invalid_values_and_naive_fetch_raise_only_fixed_errors(self):
        for value in (True, 0, -1, float("nan"), float("inf"), "SYNTHETIC_SECRET"):
            with self.assertRaisesRegex(ValueError, "^quote_value_invalid$"):
                capture_quote(value, "upbit", fetched_at=NOW)
        with self.assertRaisesRegex(ValueError, "^aware_datetime_required$"):
            capture_quote(1, "upbit", fetched_at=NOW.replace(tzinfo=None))

    def test_no_arbitrary_source_or_precision_can_be_serialized(self):
        with self.assertRaisesRegex(ValueError, "^quote_source_invalid$"):
            capture_quote(1, "SYNTHETIC_SECRET", fetched_at=NOW)
        with self.assertRaisesRegex(ValueError, "^quote_contract_invalid$"):
            Quote(1, "upbit", NOW, time_precision="instant", source_time=NOW, time_resolution="day")

    def test_result_is_immutable_and_independent_of_input_dictionary(self):
        item = self.upbit()
        q = self.quote(item)
        item["trade_date"] = "20990101"
        self.assertEqual(q.source_time.date().isoformat(), "2026-09-22")
        with self.assertRaises(AttributeError):
            q.value = 2
        json.dumps(q.to_dict(), allow_nan=False)


if __name__ == "__main__":
    unittest.main()
