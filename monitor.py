#!/usr/bin/env python3
"""
김치프리미엄 모니터 — 테더 김프 & 금 김프
- 테더 김프: Upbit USDT/KRW vs USD/KRW 환율
- 금 김프: KRX 금현물(네이버) vs 국제 금시세(yfinance) + 환율
- 알림: 텔레그램 봇
- 시그널 시 Private repo dispatch (선택)
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
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
DISPATCH_PAT = os.environ.get("DISPATCH_PAT", "")
DISPATCH_REPO = os.environ.get("DISPATCH_REPO", "")

USDT_KIMP_LOW = float(os.environ.get("USDT_KIMP_LOW", "0"))
GOLD_KIMP_LOW = float(os.environ.get("GOLD_KIMP_LOW", "0"))
GOLD_KIMP_HIGH = float(os.environ.get("GOLD_KIMP_HIGH", "10"))


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
    """
    네이버 증권에서 KRX 금현물 1g 가격(원) 크롤링
    여러 URL과 패턴을 시도하여 안정성 확보
    """
    sources = [
        # 소스 1: 네이버 모바일
        {
            "url": "https://m.stock.naver.com/marketindex/metals/M04020000",
            "headers": {
                "User-Agent": (
                    "Mozilla/5.0 (Linux; Android 13) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Mobile Safari/537.36"
                )
            },
        },
        # 소스 2: 네이버 시세 API (금현물 ETF)
        {
            "url": "https://api.stock.naver.com/etf/411060/basic",
            "headers": {"User-Agent": "Mozilla/5.0"},
        },
        # 소스 3: 네이버 데스크톱
        {
            "url": "https://finance.naver.com/marketindex/goldDaily498498.naver",
            "headers": {"User-Agent": "Mozilla/5.0"},
        },
    ]

    # ── 소스 1: 모바일 페이지 크롤링 ──
    try:
        src = sources[0]
        resp = requests.get(src["url"], headers=src["headers"], timeout=15)
        resp.raise_for_status()
        text = resp.text

        # 패턴들 시도
        patterns = [
            r"([\d,]+)\s*원/g",                    # "233,910원/g"
            r'"currentPrice"\s*:\s*"?([\d,.]+)"?',  # JSON 내 currentPrice
            r'금.*?([\d]{3},[\d]{3})\s*원',         # "금 현물 233,910원"
            r'class="price"[^>]*>([\d,]+)',          # <span class="price">233910
        ]
        for pattern in patterns:
            match = re.search(pattern, text)
            if match:
                price = float(match.group(1).replace(",", ""))
                if 50_000 < price < 1_000_000:  # 합리적 범위 검증
                    print(f"  [KRX Gold] 국내 금현물 = {price:,.0f} 원/g (모바일)")
                    return price
        print(f"  [KRX Gold] 모바일 파싱 실패, 응답 길이={len(text)}")
    except Exception as e:
        print(f"  [KRX Gold] 모바일 조회 실패: {e}")

    # ── 소스 2: 네이버 ETF API → 금 1g 가격으로 환산 ──
    try:
        src = sources[1]
        resp = requests.get(src["url"], headers=src["headers"], timeout=15)
        resp.raise_for_status()
        data = resp.json()
        # ETF 현재가 추출
        etf_price = None
        for key in ["closePrice", "nowVal", "stckPrpr"]:
            if key in data:
                etf_price = float(str(data[key]).replace(",", ""))
                break
        if etf_price is None and "currentPrice" in str(data):
            match = re.search(r'"currentPrice"\s*:\s*"?([\d,.]+)"?', str(data))
            if match:
                etf_price = float(match.group(1).replace(",", ""))

        if etf_price:
            # ACE KRX금현물 ETF: 1주 ≈ 0.1454g (변동 가능, 근사값)
            gold_per_gram = etf_price / 0.1454
            if 50_000 < gold_per_gram < 1_000_000:
                print(f"  [KRX Gold] 국내 금현물 ≈ {gold_per_gram:,.0f} 원/g (ETF 환산)")
                return gold_per_gram
        print(f"  [KRX Gold] ETF API 파싱 실패")
    except Exception as e:
        print(f"  [KRX Gold] ETF API 실패: {e}")

    # ── 소스 3: 네이버 데스크톱 금시세 페이지 ──
    try:
        src = sources[2]
        resp = requests.get(src["url"], headers=src["headers"], timeout=15)
        resp.raise_for_status()
        text = resp.text
        match = re.search(r"([\d,]+)\s*원", text)
        if match:
            price = float(match.group(1).replace(",", ""))
            if 50_000 < price < 1_000_000:
                print(f"  [KRX Gold] 국내 금현물 = {price:,.0f} 원/g (데스크톱)")
                return price
    except Exception as e:
        print(f"  [KRX Gold] 데스크톱 조회 실패: {e}")

    raise RuntimeError("KRX 금현물 가격을 파싱할 수 없습니다.")



def get_international_gold_usd_per_oz() -> float:
    ticker = yf.Ticker("GC=F")
    try:
        price = ticker.fast_info.last_price
    except Exception:
        hist = ticker.history(period="1d")
        if hist.empty:
            raise RuntimeError("yfinance에서 금 시세를 가져올 수 없습니다.")
        price = float(hist["Close"].iloc[-1])

    print(f"  [Yahoo] 국제 금 = ${price:,.2f}/oz")
    return float(price)


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
        resp.raise_for_status()
        print("  [Telegram] 알림 전송 성공")
    except Exception as e:
        print(f"  [Telegram] 전송 실패: {e}")


def trigger_private_repo(signal_data: dict):
    if not DISPATCH_PAT or not DISPATCH_REPO:
        print("  [Dispatch] PAT/REPO 미설정 — dispatch 건너뜀")
        return
    url = f"https://api.github.com/repos/{DISPATCH_REPO}/dispatches"
    headers = {
        "Authorization": f"Bearer {DISPATCH_PAT}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    payload = {"event_type": "kimp-signal", "client_payload": signal_data}
    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=10)
        if resp.status_code == 204:
            print(f"  [Dispatch] → {DISPATCH_REPO} 트리거 성공")
        else:
            print(f"  [Dispatch] 실패: {resp.status_code} {resp.text}")
    except Exception as e:
        print(f"  [Dispatch] 오류: {e}")


# ═══════════════════════════════════════════════════════
#  메인
# ═══════════════════════════════════════════════════════

def main():
    now = datetime.now(KST)
    print(f"\n{'='*55}")
    print(f"  김치프리미엄 모니터  |  {now.strftime('%Y-%m-%d %H:%M:%S KST')}")
    print(f"{'='*55}")

    alerts = []
    signal_data = {}

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
            emoji = "🔵" if usdt_kimp < 0 else "🟡"
            alert_msg = (
                f"{emoji} <b>테더 김프 알림</b>\n"
                f"김프: <b>{usdt_kimp:+.2f}%</b> (기준: ≤{USDT_KIMP_LOW}%)\n"
                f"Upbit USDT: {upbit_usdt:,.0f}원\n"
                f"환율(USD/KRW): {usd_krw:,.2f}원\n"
                f"차이: {upbit_usdt - usd_krw:+,.2f}원\n"
                f"⏰ {now.strftime('%H:%M KST')}"
            )
            alerts.append(alert_msg)
            signal_data["usdt"] = {
                "kimp": round(usdt_kimp, 4),
                "upbit_price": upbit_usdt,
                "usd_krw": usd_krw,
            }
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

        # ⚡ 이중 트리거: 0% 이하 OR 10% 이상
        gold_triggered = False
        trigger_reason = ""

        if gold_kimp <= GOLD_KIMP_LOW:
            gold_triggered = True
            trigger_reason = f"≤ {GOLD_KIMP_LOW}%"
            emoji = "🔵"
        elif gold_kimp >= GOLD_KIMP_HIGH:
            gold_triggered = True
            trigger_reason = f"≥ {GOLD_KIMP_HIGH}%"
            emoji = "🔴"

        if gold_triggered:
            alert_msg = (
                f"{emoji} <b>금 김프 알림</b> ({trigger_reason})\n"
                f"김프: <b>{gold_kimp:+.2f}%</b>\n"
                f"국내(KRX): {krx_gold:,.0f}원/g\n"
                f"국제: {intl_gold_krw_g:,.0f}원/g (${intl_gold_oz:,.2f}/oz)\n"
                f"환율(USD/KRW): {usd_krw:,.2f}원\n"
                f"⏰ {now.strftime('%H:%M KST')}"
            )
            alerts.append(alert_msg)
            signal_data["gold"] = {
                "kimp": round(gold_kimp, 4),
                "krx_gold_krw_g": krx_gold,
                "intl_gold_usd_oz": intl_gold_oz,
                "intl_gold_krw_g": round(intl_gold_krw_g, 2),
                "usd_krw": usd_krw,
                "trigger": trigger_reason,
            }
    except Exception as e:
        print(f"  ⚠ 금 김프 계산 실패: {e}")

    # 4. 결과 요약
    print(f"\n{'─'*55}")
    usdt_str = f"{usdt_kimp:+.2f}%" if usdt_kimp is not None else "N/A"
    gold_str = f"{gold_kimp:+.2f}%" if gold_kimp is not None else "N/A"
    print(f"  요약: 테더 김프={usdt_str} | 금 김프={gold_str}")
    print(f"  조건: 테더 ≤{USDT_KIMP_LOW}% | 금 ≤{GOLD_KIMP_LOW}% 또는 ≥{GOLD_KIMP_HIGH}%")

    if alerts:
        print(f"\n  🚨 알림 {len(alerts)}건 발송!")
        send_telegram("\n\n".join(alerts))
        if signal_data:
            trigger_private_repo(signal_data)
    else:
        print("\n  ✅ 정상 범위 — 알림 없음")

    print(f"{'='*55}\n")


if __name__ == "__main__":
    main()
