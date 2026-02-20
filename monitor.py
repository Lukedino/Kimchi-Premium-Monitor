#!/usr/bin/env python3
"""
김치프리미엄 모니터 — 테더 김프 & 금 김프
스마트 알림: 방향성 기반 — 악화 시에만 재알림
상태 저장: 레포 내 state.json (최근 10건 이력 + 마지막 알림값)
"""

import os
import sys
import json
import re
from datetime import datetime, timezone, timedelta

import requests
import yfinance as yf

# ─── 상수 ───────────────────────────────────────────────
KST = timezone(timedelta(hours=9))
TROY_OUNCE_TO_GRAM = 31.1035
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")
MAX_HISTORY = 10

# ─── 환경변수 ───────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN") or ""
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID") or ""

USDT_KIMP_LOW = float(os.environ.get("USDT_KIMP_LOW") or "0")
USDT_KIMP_HIGH = float(os.environ.get("USDT_KIMP_HIGH") or "10")
GOLD_KIMP_LOW = float(os.environ.get("GOLD_KIMP_LOW") or "0")
GOLD_KIMP_HIGH = float(os.environ.get("GOLD_KIMP_HIGH") or "10")


# ═══════════════════════════════════════════════════════
#  상태 관리 (로컬 파일 + git commit)
# ═══════════════════════════════════════════════════════

def load_state() -> dict:
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                state = json.load(f)
            history_count = len(state.get("history", []))
            alert_keys = list(state.get("last_alert", {}).keys())
            print(f"  [State] 로드 성공: 이력 {history_count}건, 알림상태 {alert_keys}")
            return state
    except Exception as e:
        print(f"  [State] 로드 실패: {e}")
    print("  [State] 신규 생성")
    return {"history": [], "last_alert": {}}


def save_state(state: dict):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
        print(f"  [State] 파일 저장 완료")

        os.system("git config user.name 'kimp-bot'")
        os.system("git config user.email 'bot@kimp-monitor'")
        os.system(f"git add {STATE_FILE}")

        result = os.popen("git diff --cached --quiet; echo $?").read().strip()
        if result == "1":
            os.system('git commit -m "update state [skip ci]"')
            os.system("git push")
            print("  [State] git push 완료")
        else:
            print("  [State] 변경사항 없음 — push 생략")
    except Exception as e:
        print(f"  [State] 저장 실패: {e}")


def add_history(state: dict, usdt_kimp, gold_kimp, now: datetime):
    entry = {
        "time": now.isoformat(),
        "usdt_kimp": round(usdt_kimp, 4) if usdt_kimp is not None else None,
        "gold_kimp": round(gold_kimp, 4) if gold_kimp is not None else None,
    }
    state.setdefault("history", []).append(entry)
    if len(state["history"]) > MAX_HISTORY:
        state["history"] = state["history"][-MAX_HISTORY:]


def should_alert(state: dict, key: str, current_value: float, now: datetime) -> tuple:
    """
    방향성 기반 알림 판단
    - 첫 알림: 무조건 발송
    - 재알림: 이전보다 더 악화(같은 방향으로 심화)될 때만
      - _low 키: 현재값이 이전값보다 더 낮을 때
      - _high 키: 현재값이 이전값보다 더 높을 때
    """
    last_alert = state.get("last_alert", {})
    prev = last_alert.get(key)

    if prev is None:
        return True, "첫 알림"

    prev_value = prev["value"]
    diff = current_value - prev_value

    if key.endswith("_low"):
        if current_value < prev_value:
            return True, f"악화 ({prev_value:+.2f}% → {current_value:+.2f}%, {diff:+.2f}%p)"
        else:
            print(f"  [Filter] {key}: 이전 {prev_value:+.2f}% → 현재 {current_value:+.2f}% (개선 방향) — 알림 생략")
            return False, ""

    if key.endswith("_high"):
        if current_value > prev_value:
            return True, f"악화 ({prev_value:+.2f}% → {current_value:+.2f}%, {diff:+.2f}%p)"
        else:
            print(f"  [Filter] {key}: 이전 {prev_value:+.2f}% → 현재 {current_value:+.2f}% (개선 방향) — 알림 생략")
            return False, ""

    return True, "알림"


def update_state(state: dict, key: str, value: float, now: datetime):
    state.setdefault("last_alert", {})[key] = {
        "value": round(value, 4),
        "time": now.isoformat(),
    }


# ═══════════════════════════════════════════════════════
#  데이터 수집
# ═══════════════════════════════════════════════════════

def get_upbit_usdt_price() -> float:
    url = "https://api.upbit.com/v1/ticker"
    params = {"markets": "KRW-USDT"}
    headers = {"Accept": "application/json"}
    resp = requests.get(url, params=params, headers=headers, timeout=10)
    resp.raise_for_status()
    price = float(resp.json()[0]["trade_price"])
    print(f"  [Upbit] USDT/KRW = {price:,.2f}")
    return price


def get_usd_krw_rate() -> float:
    try:
        resp = requests.get("https://open.er-api.com/v6/latest/USD", timeout=10)
        resp.raise_for_status()
        rate = float(resp.json()["rates"]["KRW"])
        print(f"  [FX-1] USD/KRW = {rate:,.2f}")
        return rate
    except Exception as e:
        print(f"  [FX-1] 실패: {e}")
    try:
        resp = requests.get(
            "https://cdn.jsdelivr.net/npm/@fawazahmed0/currency-api@latest/v1/currencies/usd.json",
            timeout=10,
        )
        resp.raise_for_status()
        rate = float(resp.json()["usd"]["krw"])
        print(f"  [FX-2] USD/KRW = {rate:,.2f}")
        return rate
    except Exception as e:
        print(f"  [FX-2] 실패: {e}")
    raise RuntimeError("USD/KRW 환율을 가져올 수 없습니다.")


def get_krx_gold_price_per_gram() -> float:
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }
    try:
        url = "https://api.stock.naver.com/marketindex/metals/M04020000"
        resp = requests.get(url, headers=headers, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        price = float(data["closePrice"].replace(",", ""))
        print(f"  [KRX Gold] 국내 금현물 = {price:,.0f} 원/g (네이버 API)")
        return price
    except Exception as e:
        print(f"  [KRX Gold] 네이버 API 실패: {e}")
    try:
        url = "https://finance.naver.com/marketindex/goldDetail.naver"
        resp = requests.get(url, headers=headers, timeout=15)
        resp.raise_for_status()
        text = resp.text
        for pattern in [r"([\d,]+\.\d+)\s*원/g", r"([\d,]+)\s*원/g"]:
            match = re.search(pattern, text)
            if match:
                price = float(match.group(1).replace(",", ""))
                print(f"  [KRX Gold] 국내 금현물 = {price:,.0f} 원/g (데스크톱)")
                return price
    except Exception as e:
        print(f"  [KRX Gold] 데스크톱 실패: {e}")
    raise RuntimeError("KRX 금현물 가격을 파싱할 수 없습니다.")


def get_international_gold_usd_per_oz() -> float:
    try:
        url = "https://forex-data-feed.swissquote.com/public-quotes/bboquotes/instrument/XAU/USD"
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        prices = data[0]["spreadProfilePrices"][0]
        bid = prices["bid"]
        ask = prices["ask"]
        spot = (bid + ask) / 2
        print(f"  [Swissquote] 국제 금 현물 = ${spot:,.2f}/oz (bid ${bid:,.2f} / ask ${ask:,.2f})")
        return spot
    except Exception as e:
        print(f"  [Swissquote] 실패: {e}")

    try:
        print("  [Yahoo] 폴백: 선물(GC=F) 사용")
        ticker = yf.Ticker("GC=F")
        try:
            price = ticker.fast_info.last_price
        except Exception:
            hist = ticker.history(period="1d")
            if hist.empty:
                raise RuntimeError("yfinance 데이터 없음")
            price = float(hist["Close"].iloc[-1])
        print(f"  [Yahoo] 국제 금 선물 = ${price:,.2f}/oz (현물 대비 ~$20-40 높음)")
        return float(price)
    except Exception as e:
        print(f"  [Yahoo] 실패: {e}")

    raise RuntimeError("국제 금 시세를 가져올 수 없습니다.")


# ═══════════════════════════════════════════════════════
#  김프 계산
# ═══════════════════════════════════════════════════════

def calc_usdt_kimp(upbit_usdt: float, usd_krw: float) -> float:
    return ((upbit_usdt - usd_krw) / usd_krw) * 100


def calc_gold_kimp(krx_gold_krw_g: float, intl_gold_usd_oz: float, usd_krw: float):
    intl_gold_krw_g = (intl_gold_usd_oz * usd_krw) / TROY_OUNCE_TO_GRAM
    kimp = ((krx_gold_krw_g - intl_gold_krw_g) / intl_gold_krw_g) * 100
    return kimp, intl_gold_krw_g


# ═══════════════════════════════════════════════════════
#  알림
# ═══════════════════════════════════════════════════════

def send_telegram(message: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("  [Telegram] 토큰/채팅ID 미설정 — 알림 건너뜀")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}
    try:
        resp = requests.post(url, json=payload, timeout=10)
        if resp.ok:
            print("  [Telegram] 알림 전송 성공")
        else:
            try:
                err = resp.json().get("description", resp.text)
            except Exception:
                err = resp.text
            print(f"  [Telegram] 전송 실패: {resp.status_code} — {err}")
    except Exception as e:
        print(f"  [Telegram] 전송 오류: {e}")


# ═══════════════════════════════════════════════════════
#  메인
# ═══════════════════════════════════════════════════════

def main():
    now = datetime.now(KST)
    print(f"\n{'='*55}")
    print(f"  김치프리미엄 모니터  |  {now.strftime('%Y-%m-%d %H:%M:%S KST')}")
    print(f"  알림 방식: 방향성 기반 (악화 시에만 재알림)")
    print(f"{'='*55}")

    # 상태 로드
    print("\n[0] 알림 상태 로드")
    state = load_state()
    alerts = []

    # 1. USD/KRW
    print("\n[1] USD/KRW 환율 조회")
    try:
        usd_krw = get_usd_krw_rate()
    except Exception as e:
        msg = f"❌ USD/KRW 환율 조회 실패: {e}"
        print(msg)
        send_telegram(msg)
        sys.exit(1)

    # 2. 테더 김프
    print("\n[2] 테더 김프 계산")
    usdt_kimp = None
    try:
        upbit_usdt = get_upbit_usdt_price()
        usdt_kimp = calc_usdt_kimp(upbit_usdt, usd_krw)
        print(f"  ▶ 테더 김프 = {usdt_kimp:+.2f}%")

        if usdt_kimp <= USDT_KIMP_LOW:
            send_it, reason = should_alert(state, "usdt_low", usdt_kimp, now)
            if send_it:
                emoji = "🔵" if usdt_kimp < 0 else "🟡"
                alert_msg = (
                    f"{emoji} <b>테더 김프 알림</b> (≤{USDT_KIMP_LOW}%, {reason})\n"
                    f"김프: <b>{usdt_kimp:+.2f}%</b>\n"
                    f"Upbit USDT: {upbit_usdt:,.0f}원\n"
                    f"환율: {usd_krw:,.2f}원\n"
                    f"⏰ {now.strftime('%H:%M KST')}"
                )
                alerts.append(alert_msg)
                update_state(state, "usdt_low", usdt_kimp, now)
            if "usdt_high" in state.get("last_alert", {}):
                del state["last_alert"]["usdt_high"]

        elif usdt_kimp >= USDT_KIMP_HIGH:
            send_it, reason = should_alert(state, "usdt_high", usdt_kimp, now)
            if send_it:
                alert_msg = (
                    f"🔴 <b>테더 김프 알림</b> (≥{USDT_KIMP_HIGH}%, {reason})\n"
                    f"김프: <b>{usdt_kimp:+.2f}%</b>\n"
                    f"Upbit USDT: {upbit_usdt:,.0f}원\n"
                    f"환율: {usd_krw:,.2f}원\n"
                    f"⏰ {now.strftime('%H:%M KST')}"
                )
                alerts.append(alert_msg)
                update_state(state, "usdt_high", usdt_kimp, now)
            if "usdt_low" in state.get("last_alert", {}):
                del state["last_alert"]["usdt_low"]

        else:
            la = state.get("last_alert", {})
            if "usdt_low" in la or "usdt_high" in la:
                la.pop("usdt_low", None)
                la.pop("usdt_high", None)
                print("  [State] 테더 정상 복귀 → 상태 초기화")
    except Exception as e:
        print(f"  ⚠ 테더 김프 계산 실패: {e}")

    # 3. 금 김프
    print("\n[3] 금 김프 계산")
    gold_kimp = None
    try:
        krx_gold = get_krx_gold_price_per_gram()
        intl_gold_oz = get_international_gold_usd_per_oz()
        gold_kimp, intl_gold_krw_g = calc_gold_kimp(krx_gold, intl_gold_oz, usd_krw)

        print(f"  ▶ 금 김프 = {gold_kimp:+.2f}%")
        print(f"    국내: {krx_gold:,.0f}원/g | 국제: {intl_gold_krw_g:,.0f}원/g")

        if gold_kimp <= GOLD_KIMP_LOW:
            send_it, reason = should_alert(state, "gold_low", gold_kimp, now)
            if send_it:
                alert_msg = (
                    f"🔵 <b>금 김프 알림</b> (≤{GOLD_KIMP_LOW}%, {reason})\n"
                    f"김프: <b>{gold_kimp:+.2f}%</b>\n"
                    f"국내: {krx_gold:,.0f}원/g\n"
                    f"국제: {intl_gold_krw_g:,.0f}원/g (${intl_gold_oz:,.2f}/oz)\n"
                    f"환율: {usd_krw:,.2f}원\n"
                    f"⏰ {now.strftime('%H:%M KST')}"
                )
                alerts.append(alert_msg)
                update_state(state, "gold_low", gold_kimp, now)
            if "gold_high" in state.get("last_alert", {}):
                del state["last_alert"]["gold_high"]

        elif gold_kimp >= GOLD_KIMP_HIGH:
            send_it, reason = should_alert(state, "gold_high", gold_kimp, now)
            if send_it:
                alert_msg = (
                    f"🔴 <b>금 김프 알림</b> (≥{GOLD_KIMP_HIGH}%, {reason})\n"
                    f"김프: <b>{gold_kimp:+.2f}%</b>\n"
                    f"국내: {krx_gold:,.0f}원/g\n"
                    f"국제: {intl_gold_krw_g:,.0f}원/g (${intl_gold_oz:,.2f}/oz)\n"
                    f"환율: {usd_krw:,.2f}원\n"
                    f"⏰ {now.strftime('%H:%M KST')}"
                )
                alerts.append(alert_msg)
                update_state(state, "gold_high", gold_kimp, now)
            if "gold_low" in state.get("last_alert", {}):
                del state["last_alert"]["gold_low"]

        else:
            la = state.get("last_alert", {})
            if "gold_low" in la or "gold_high" in la:
                la.pop("gold_low", None)
                la.pop("gold_high", None)
                print("  [State] 금 김프 정상 복귀 → 상태 초기화")
    except Exception as e:
        print(f"  ⚠ 금 김프 계산 실패: {e}")

    # 이력 기록
    add_history(state, usdt_kimp, gold_kimp, now)

    # 4. 결과
    print(f"\n{'─'*55}")
    usdt_str = f"{usdt_kimp:+.2f}%" if usdt_kimp is not None else "N/A"
    gold_str = f"{gold_kimp:+.2f}%" if gold_kimp is not None else "N/A"
    print(f"  요약: 테더 김프={usdt_str} | 금 김프={gold_str}")
    print(f"  조건: 테더 ≤{USDT_KIMP_LOW}% 또는 ≥{USDT_KIMP_HIGH}% | 금 ≤{GOLD_KIMP_LOW}% 또는 ≥{GOLD_KIMP_HIGH}%")

    run_mode = os.environ.get("RUN_MODE") or ""
    is_manual = run_mode == "workflow_dispatch"
    print(f"  실행 모드: {'수동' if is_manual else '스케줄'} (RUN_MODE={run_mode})")

    if is_manual and not alerts:
        report = (
            f"📊 <b>김프 현황 리포트</b> (수동 조회)\n\n"
            f"테더 김프: <b>{usdt_str}</b>\n"
            f"금 김프: <b>{gold_str}</b>\n"
        )
        if usdt_kimp is not None:
            report += f"\nUpbit USDT: {upbit_usdt:,.0f}원\n"
            report += f"환율: {usd_krw:,.2f}원\n"
        if gold_kimp is not None:
            report += f"\n국내 금: {krx_gold:,.0f}원/g\n"
            report += f"국제 금: {intl_gold_krw_g:,.0f}원/g (${intl_gold_oz:,.2f}/oz)\n"
        report += (
            f"\n기준: 테더 ≤{USDT_KIMP_LOW}% 또는 ≥{USDT_KIMP_HIGH}% | 금 ≤{GOLD_KIMP_LOW}% 또는 ≥{GOLD_KIMP_HIGH}%\n"
            f"✅ 현재 정상 범위\n"
            f"⏰ {now.strftime('%Y-%m-%d %H:%M KST')}"
        )
        send_telegram(report)
        print(f"\n  📊 수동 실행 — 현황 리포트 전송!")
    elif alerts:
        print(f"\n  🚨 알림 {len(alerts)}건 발송!")
        send_telegram("\n\n".join(alerts))
    else:
        print("\n  ✅ 알림 없음 (정상 범위 또는 개선 방향)")

    # 상태 저장 (매 실행마다)
    print("\n[5] 상태 저장")
    save_state(state)

    print(f"{'='*55}\n")


if __name__ == "__main__":
    main()
