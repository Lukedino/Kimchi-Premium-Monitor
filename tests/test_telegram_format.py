"""텔레그램 공통 양식 고정: HTML 이스케이프, 평문 재전송, 한국 시각, 길이 분할, 메시지 모양.
합성 가격·상태만 쓰며 자격증명·네트워크·실제 Git 은 쓰지 않는다."""
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
with patch.object(os, "environ", {}):
    import monitor as m

KST = timezone(timedelta(hours=9))
NOW = datetime(2026, 9, 22, 12, tzinfo=KST)  # 화요일


class ConnectionError(Exception):  # noqa: A001 - 이름으로 한글 원인을 고르는지 확인
    pass


def response(status, payload):
    resp = Mock(ok=200 <= status < 300, status_code=status)
    resp.json.return_value = payload
    return resp


class HelperTests(unittest.TestCase):
    def setUp(self):
        self.output = contextlib.ExitStack()
        self.addCleanup(self.output.close)
        self.out = self.output.enter_context(contextlib.redirect_stdout(io.StringIO()))
        self.output.enter_context(patch.object(socket, "socket", side_effect=AssertionError("network forbidden")))
        m.configure({"TELEGRAM_BOT_TOKEN": "synthetic-token", "TELEGRAM_CHAT_ID": "synthetic-chat"})
        self.addCleanup(m.configure, {})

    def test_escape_variable_text(self):
        self.assertEqual(m.esc("A&B <x> \"q\""), "A&amp;B &lt;x&gt; \"q\"")

    def test_kst_format_converts_utc_and_shows_weekday(self):
        utc = datetime(2026, 11, 13, 7, 28, tzinfo=timezone.utc)
        self.assertEqual(m.fmt_kst(utc), "11/13(금) 16:28")
        # UTC 기준으론 전날이지만 한국 시각으로 날짜가 넘어간다.
        self.assertEqual(m.fmt_kst(datetime(2026, 11, 12, 23, 30, tzinfo=timezone.utc)), "11/13(금) 08:30")
        self.assertEqual(m.fmt_kst(datetime(2025, 12, 31, 9, tzinfo=KST), ref=NOW), "2025/12/31(수) 09:00")

    def test_threshold_format(self):
        self.assertEqual(m.fmt_pct_line(0.0), "0%")
        self.assertEqual(m.fmt_pct_line(-0.0), "0%")
        self.assertEqual(m.fmt_pct_line(-2.0), "-2%")
        self.assertEqual(m.fmt_pct_line(10.0), "+10%")
        self.assertEqual(m.fmt_pct_line(-0.5), "-0.5%")

    def test_error_label_is_korean_or_escaped_code(self):
        self.assertEqual(m.error_label("ConnectionError"), "연결 실패")
        self.assertEqual(m.error_label("ReadTimeout"), "응답 시간 초과")
        self.assertEqual(m.error_label("Weird<Err>"), "기타 오류 <code>Weird&lt;Err&gt;</code>")

    def test_html_payload_disables_preview(self):
        post = Mock(return_value=response(200, {"ok": True}))
        with patch.object(m, "_requests", return_value=Mock(post=post)):
            self.assertTrue(m.send_telegram("<b>제목</b>"))
        payload = post.call_args.kwargs["json"]
        self.assertEqual(payload["parse_mode"], "HTML")
        self.assertEqual(payload["link_preview_options"], {"is_disabled": True})

    def test_parse_error_retries_once_as_plain_text(self):
        post = Mock(side_effect=[
            response(400, {"ok": False, "description": "Bad Request: can't parse entities: x"}),
            response(200, {"ok": True}),
        ])
        with patch.object(m, "_requests", return_value=Mock(post=post)):
            self.assertTrue(m.send_telegram("<b>[김프] 제목</b>\nA &amp; B &lt;5&gt;"))
        self.assertEqual(post.call_count, 2)
        plain = post.call_args_list[1].kwargs["json"]
        self.assertNotIn("parse_mode", plain)
        self.assertEqual(plain["text"], "[김프] 제목\nA & B <5>")
        self.assertNotIn("can't parse", self.out.getvalue())

    def test_other_400_is_not_retried(self):
        post = Mock(return_value=response(400, {"ok": False, "description": "Bad Request: chat not found"}))
        with patch.object(m, "_requests", return_value=Mock(post=post)):
            self.assertFalse(m.send_telegram("본문"))
        self.assertEqual(post.call_count, 1)

    def test_failed_plain_retry_is_failure(self):
        post = Mock(side_effect=[
            response(400, {"ok": False, "description": "can't parse entities"}),
            response(500, {"ok": False}),
        ])
        with patch.object(m, "_requests", return_value=Mock(post=post)):
            self.assertFalse(m.send_telegram("<b>x</b>"))

    def test_long_message_is_split_on_section_boundaries(self):
        section = "<b>섹션</b>\n" + "\n".join(f"• 줄 {i} &amp; 값" for i in range(150))
        text = "\n\n".join([section] * 4)
        self.assertGreater(len(text), m.TELEGRAM_MAX_LEN)
        chunks = m.split_message(text)
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertLessEqual(len(chunk), m.TELEGRAM_MAX_LEN)
            self.assertEqual(chunk.count("<b>"), chunk.count("</b>"))
        self.assertEqual("\n\n".join(chunks).replace("\n", ""), text.replace("\n", ""))
        post = Mock(return_value=response(200, {"ok": True}))
        with patch.object(m, "_requests", return_value=Mock(post=post)):
            self.assertTrue(m.send_telegram(text))
        self.assertEqual(post.call_count, len(chunks))

    def test_single_huge_line_does_not_cut_entities(self):
        line = "가" * 4094 + "&amp;" + "나" * 10
        chunks = m.split_message(line)
        self.assertTrue(all(len(c) <= m.TELEGRAM_MAX_LEN for c in chunks))
        self.assertTrue(chunks[1].startswith("&amp;"))
        self.assertEqual("".join(chunks), line)

    def test_short_message_is_not_split(self):
        self.assertEqual(m.split_message("짧은 본문"), ["짧은 본문"])


class MessageShapeTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        directory = ROOT / ("kimchi-test-" + uuid.uuid4().hex)
        directory.mkdir()
        self.stack.callback(shutil.rmtree, directory)
        self.state_file = directory / "synthetic-state.json"
        self.output = self.stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        self.stack.enter_context(patch.object(os, "environ", {}))
        self.stack.enter_context(patch.object(socket, "socket", side_effect=AssertionError("network forbidden")))
        self.stack.enter_context(patch.object(subprocess, "run", side_effect=AssertionError("Git forbidden")))
        self.stack.enter_context(patch.object(m, "_requests", side_effect=AssertionError("HTTP forbidden")))
        self.stack.enter_context(patch.object(m, "_ticker", side_effect=AssertionError("Yahoo forbidden")))
        m.configure({})

    def run_prices(self, *, fx=1400, usdt=1470, gold_premium=5, gold=3000, now=NOW, **options):
        krx = (3000 * 1400 / m.TROY_OUNCE_TO_GRAM) * (1 + gold_premium / 100)
        if not isinstance(gold, Exception) and not isinstance(fx, Exception):
            krx = (gold * fx / m.TROY_OUNCE_TO_GRAM) * (1 + gold_premium / 100)
        with contextlib.ExitStack() as stack:
            for name, value in (("get_usd_krw_rate", fx), ("get_upbit_usdt_price", usdt),
                                ("get_krx_gold_price_per_gram", krx),
                                ("get_international_gold_usd_per_oz", gold)):
                kwargs = {"side_effect": value} if isinstance(value, Exception) else {"return_value": value}
                stack.enter_context(patch.object(m, name, **kwargs))
            sender = stack.enter_context(patch.object(m, "send_telegram", return_value=True))
            settings = dict(send_alerts=True, persist_state=True, state_file=self.state_file, now=now)
            settings.update(options)
            m.run_monitor(**settings)
        return [c.args[0] for c in sender.call_args_list]

    def test_usdt_low_first_then_worsening(self):
        [first] = self.run_prices(usdt=1394.12)
        lines = first.split("\n")
        self.assertEqual(lines[0], "🟢 <b>[김프] 테더 김프 낮음</b>")
        self.assertEqual(lines[1], "09/22(화) 12:00 · 기준 0% 이하")
        self.assertEqual(lines[2], "")
        self.assertIn("김프: <b>-0.42%</b>", lines)
        self.assertIn("판정: 첫 알림", lines)
        self.assertIn("업비트 USDT: 1,394원", lines)
        self.assertIn("원/달러: 1,400.00원", lines)
        self.assertNotIn("KST", first)
        [second] = self.run_prices(usdt=1390.06, now=NOW + timedelta(minutes=15))
        self.assertIn("변화: -0.42% → -0.71% (-0.29%p)", second.split("\n"))
        self.assertIn("09/22(화) 12:15", second)

    def test_usdt_high_title(self):
        [message] = self.run_prices(usdt=1554)
        self.assertTrue(message.startswith("🟡 <b>[김프] 테더 김프 높음</b>\n09/22(화) 12:00 · 기준 +10% 이상\n"))

    def test_gold_step_message_spells_out_level(self):
        [first] = self.run_prices(gold_premium=-2.2)
        lines = first.split("\n")
        self.assertEqual(lines[0], "🟢 <b>[김프] 금 김프 낮음</b>")
        self.assertIn("단계: 2 (-2% 선 돌파) · 첫 알림", lines)
        self.assertIn("국내 금: " + f"{(3000 * 1400 / m.TROY_OUNCE_TO_GRAM) * 0.978:,.0f}원/g", lines)
        self.assertTrue(any(line.startswith("국제 금: ") and " · $3,000.00/oz" in line for line in lines))
        self.assertIn("원/달러: 1,400.00원", lines)
        self.assertTrue(first.endswith("<b>변동 원인</b>\n비교할 이전 시세 없음"))
        self.assertNotIn("Level", first)
        [second] = self.run_prices(gold_premium=-3.5, gold=3050, now=NOW + timedelta(hours=6))
        lines = second.split("\n")
        self.assertIn("단계: 2→3 (-3% 선 돌파)", lines)
        self.assertIn("변화: -2.20% → -3.50%", lines)
        self.assertIn("<b>변동 원인 · 6.0시간 전 대비</b>", lines)
        self.assertIn("• 국제 금 ↑ +1.67%", lines)
        self.assertIn("  $3,000 → $3,050/oz", lines)
        self.assertTrue(lines[-1].startswith("주요인: "))
        for line in lines:  # 괄호는 한 겹까지
            self.assertNotRegex(line, r"\([^)]*\(")

    def test_source_failure_message(self):
        messages = []
        for _ in range(3):
            messages += self.run_prices(gold=ConnectionError("synthetic"))
        [warning] = messages
        self.assertEqual(warning.split("\n"), [
            "🟡 <b>[김프] 시세 수집 실패</b>",
            "09/22(화) 12:00 · 금",
            "",
            "연속 실패: 3회",
            "원인: 연결 실패",
        ])

    def test_fx_failure_explains_impact(self):
        [warning] = self.run_prices(fx=RuntimeError("synthetic"))
        self.assertIn("09/22(화) 12:00 · 환율", warning)
        self.assertIn("원인: 기타 오류 <code>RuntimeError</code>", warning)
        self.assertTrue(warning.endswith("ℹ️ 환율이 없어 이번 회차는 테더·금 김프를 계산하지 못했습니다."))

    def test_manual_report_shape(self):
        [report] = self.run_prices(run_mode="workflow_dispatch")
        sections = report.split("\n\n")
        self.assertEqual(sections[0], "📊 <b>[김프] 현황 리포트</b>\n09/22(화) 12:00 · 수동 조회")
        self.assertEqual(sections[1], "테더 김프: <b>+5.00%</b>\n금 김프: <b>+5.00%</b>")
        self.assertTrue(sections[2].startswith("<b>테더</b>\n업비트 USDT: 1,470원"))
        self.assertIn("알림 기준: 0% 이하 · +10% 이상", sections[2])
        self.assertTrue(sections[3].startswith("<b>금</b>\n"))
        self.assertIn("단계 간격: 1%p", sections[3])
        self.assertTrue(sections[4].startswith("<b>변동 원인"))

    def test_manual_report_marks_failed_asset(self):
        [report] = self.run_prices(run_mode="workflow_dispatch", usdt=RuntimeError("synthetic"))
        self.assertIn("테더 김프: 조회 실패", report)
        self.assertNotIn("<b>테더</b>", report)

    def test_dry_run_does_not_echo_message_body(self):
        for _ in range(3):
            self.run_prices(gold=ConnectionError("synthetic"), send_alerts=False, persist_state=True)
        log = self.output.getvalue()
        self.assertIn("수집 실패 알림 후보: gold", log)
        self.assertNotIn("[김프]", log)


if __name__ == "__main__":
    unittest.main()
