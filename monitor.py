#!/usr/bin/env python3
"""
김치프리미엄 모니터 — 테더 김프 & 금 김프
스마트 알림: 최초 알림 후 급변(gap) 시에만 재알림
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

# ─── 환경변수 ───────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN") or ""
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID") or ""

USDT_KIMP_LOW = float(os.environ.get("USDT_KIMP_LOW") or "0")
GOLD_KIMP_LOW = float(os.environ.get("GOLD_KIMP_LOW") or "0")
GOLD_KIMP_HIGH = float(os.environ.get("GOLD_KIMP_HIGH") or "10")

# 재알림 기준: 이전 알림값 대비 이만큼 변하면 재알림 (%p 단위)
ALERT_GAP = float(os.environ.get("ALERT_GAP") or "0.5")

# Gist 상태 저장
GIST_TOKEN = os.environ.get("GIST_TOKEN") or ""
GIST_ID = os.environ.get("GIST_ID") or ""
GIST_FILENAME = "kimp_alert_state.json"


# ═══════════════════════════════════════════════════════
#  상태 관리 (GitHub Gist)
# ═══════════════════════════════════════════════════════

def load_state() -> dict:
    if not GIST_TOKEN or not GIST_ID:
        print("  [State] Gist 미설정 — 매번 알림")
        return {}
    try:
        url = f"https://api.github.com/gists/{GIST_ID}"
        headers = {"Authorization": f"Bearer {GIST_TOKEN}"}
        resp = requests.get(url, headers=headers, timeout=10)
        resp.raise_for_status()
        content = resp.json()["files"][GIST_FILENAME]["content"]
        state = json.loads(content)
        print(f"  [State] 로드: {json.dumps(state, ensure_ascii=False)}")
        return state
    except Exception as e:
        print(f"  [State] 로드 실패: {e}")
        return {}


def save_state(state: dict):
    if not GIST_TOKEN or not GIST_ID:
        return
    try:
        url = f"https://api.github.com/gists/{GIST_ID}"
        headers = {
            "Authorization": f"Bearer {GIST_TOKEN}",
            "Accept": "application/vnd.github+json",
        }
        payload = {"files": {GIST_FILENAME: {"content": json.dumps(state, indent=2)}}}
        resp = requests.patch(url, json=payload, headers=headers, timeout=10)
        resp.raise_for_status()
        print(f"  [State] 저장 완료")
    except Exception as e:
        print(f"  [State] 저장 실패: {e}")


def should_alert(state: dict, key: str, current_value: float, now: datetime) -> tuple:
    """
    알림 여부 판단
    Returns: (should_send: bool, reason: str)
    """
    if not GIST_TOKEN or not GIST_ID:
        return True, "첫 알림"

    prev = state.get(key)
    if prev is None:
        return True, "첫 알림"

    prev_value = prev["value"]
    diff = abs(current_value - prev_value)

    if diff >= ALERT_GAP:
        direction = "악화" if (
            (key == "usdt_low" and current_value < prev_value) or
            (key == "gold_low" and current_value < prev_value) or
            (key == "gold_high" and current_value > prev_value)
        ) else "변동"
        return True, f"{direction} ({prev_value:+.2f}% → {current_value:+.2f}%, 차이 {diff:.2f}%p)"

    print(f"  [Gap] {key}: 이전 {prev_value:+.2f}% → 현재 {current_value:+.2f}% (차이 {diff:.2f}%p < {ALERT_GAP}%p) — 알림 생략")
    return False, ""


def update_state(state: dict, key: str, value: float, now: datetime):
    state[key] = {
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
    """
    국제 금 현물(XAU/USD spot) 가격 조회
    소스 1: Swissquote 공개 피드 (API 키 불필요, 현물)
    소스 2: yfinance GC=F (선물, 폴백)
    """
    # 소스 1: Swissquote — XAU/USD 현물 (무료, 키 불필요)
    try:
        url = "https://forex-data-feed.swissquote.com/public-quotes/bboquotes/instrument/XAU/USD"
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        # 첫 번째 플랫폼의 premium 프로필에서 mid price 계산
        prices = data[0]["spreadProfilePrices"][0]
        bid = prices["bid"]
        ask = prices["ask"]
        spot = (bid + ask) / 2
        print(f"  [Swissquote] 국제 금 현물 = ${spot:,.2f}/oz (bid ${bid:,.2f} / ask ${ask:,.2f})")
        return spot
    except Exception as e:
        print(f"  [Swissquote] 실패: {e}")

    # 소스 2: yfinance GC=F (선물, 폴백)
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
    print(f"  재알림 기준: 이전 대비 ±{ALERT_GAP}%p 이상 변동 시")
    print(f"{'='*55}")

    # 상태 로드
    print("\n[0] 알림 상태 로드")
    state = load_state()
    state_updated = False
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
                    f"{emoji} <b>테더 김프 알림</b> ({reason})\n"
                    f"김프: <b>{usdt_kimp:+.2f}%</b> (기준: ≤{USDT_KIMP_LOW}%)\n"
                    f"Upbit USDT: {upbit_usdt:,.0f}원\n"
                    f"환율: {usd_krw:,.2f}원\n"
                    f"⏰ {now.strftime('%H:%M KST')}"
                )
                alerts.append(alert_msg)
                update_state(state, "usdt_low", usdt_kimp, now)
                state_updated = True
        else:
            if "usdt_low" in state:
                del state["usdt_low"]
                state_updated = True
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
                state_updated = True
            if "gold_high" in state:
                del state["gold_high"]
                state_updated = True

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
                state_updated = True
            if "gold_low" in state:
                del state["gold_low"]
                state_updated = True

        else:
            changed = False
            if "gold_low" in state:
                del state["gold_low"]
                changed = True
            if "gold_high" in state:
                del state["gold_high"]
                changed = True
            if changed:
                state_updated = True
                print("  [State] 금 김프 정상 복귀 → 상태 초기화")
    except Exception as e:
        print(f"  ⚠ 금 김프 계산 실패: {e}")

    # 4. 결과
    print(f"\n{'─'*55}")
    usdt_str = f"{usdt_kimp:+.2f}%" if usdt_kimp is not None else "N/A"
    gold_str = f"{gold_kimp:+.2f}%" if gold_kimp is not None else "N/A"
    print(f"  요약: 테더 김프={usdt_str} | 금 김프={gold_str}")
    print(f"  조건: 테더 ≤{USDT_KIMP_LOW}% | 금 ≤{GOLD_KIMP_LOW}% 또는 ≥{GOLD_KIMP_HIGH}%")

    # 수동 실행 시 항상 현재 상태 리포트 전송
    run_mode = os.environ.get("RUN_MODE") or ""
    is_manual = run_mode == "workflow_dispatch"

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
            f"\n기준: 테더 ≤{USDT_KIMP_LOW}% | 금 ≤{GOLD_KIMP_LOW}% 또는 ≥{GOLD_KIMP_HIGH}%\n"
            f"✅ 현재 정상 범위\n"
            f"⏰ {now.strftime('%Y-%m-%d %H:%M KST')}"
        )
        send_telegram(report)
        print(f"\n  📊 수동 실행 — 현황 리포트 전송!")
    elif alerts:
        print(f"\n  🚨 알림 {len(alerts)}건 발송!")
        send_telegram("\n\n".join(alerts))
    else:
        print("\n  ✅ 알림 없음 (정상 범위 또는 변동폭 미달)")

    if state_updated:
        print("\n[5] 상태 저장")
        save_state(state)

    print(f"{'='*55}\n")



if __name__ == "__main__":
    main()
