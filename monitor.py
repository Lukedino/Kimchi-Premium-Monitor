#!/usr/bin/env python3
"""
김치프리미엄 모니터 — 테더 김프 & 금 김프
스마트 알림: 방향성 기반 — 악화 시에만 재알림
금 김프: 단계별 알림 (0%, -1%, -2%, -3%...) + 변동 원인 분석
상태 저장: 레포 내 state.json (최근 10건 이력 + 마지막 알림값)
데이터 개선: 네이버 실시간 환율 API + 비정상값 검증 적용
"""

import os
import sys
import json
import re
import math
from datetime import datetime, timezone, timedelta

import requests
import yfinance as yf

# ─── 상수 ───────────────────────────────────────────────
KST = timezone(timedelta(hours=9))
TROY_OUNCE_TO_GRAM = 31.1035
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")
MAX_HISTORY = 10

# 금값 합리적 범위 (USD/oz)
GOLD_PRICE_MIN_USD = 1_000
GOLD_PRICE_MAX_USD = 10_000

# ─── 금 김프 단계별 알림 설정 ───────────────────────────
# low 방향: 0% 이하 진입 시 최초 알림, 이후 -1%, -2%, -3%... 단위로 알림
# high 방향: 기존과 동일 (GOLD_KIMP_HIGH 초과 시 알림)
GOLD_KIMP_STEP = 1.0  # 단계 간격 (1%p 단위)

# ─── 환경변수 ───────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN") or ""
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID")   or ""

USDT_KIMP_LOW  = float(os.environ.get("USDT_KIMP_LOW")  or "0")
USDT_KIMP_HIGH = float(os.environ.get("USDT_KIMP_HIGH") or "10")
GOLD_KIMP_LOW  = float(os.environ.get("GOLD_KIMP_LOW")  or "0")
GOLD_KIMP_HIGH = float(os.environ.get("GOLD_KIMP_HIGH") or "10")


# ═══════════════════════════════════════════════════════
#  상태 관리
# ═══════════════════════════════════════════════════════

def load_state() -> dict:
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                state = json.load(f)
            history_count = len(state.get("history", []))
            alert_keys    = list(state.get("last_alert", {}).keys())
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
        print("  [State] 파일 저장 완료")

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


def add_history(state: dict, usdt_kimp, gold_kimp, now: datetime,
                usd_krw=None, intl_gold_usd_oz=None, krx_gold_krw_g=None):
    entry = {
        "time":            now.isoformat(),
        "usdt_kimp":       round(usdt_kimp, 4) if usdt_kimp is not None else None,
        "gold_kimp":       round(gold_kimp, 4) if gold_kimp is not None else None,
        "usd_krw":         round(usd_krw, 2) if usd_krw is not None else None,
        "intl_gold_usd_oz": round(intl_gold_usd_oz, 2) if intl_gold_usd_oz is not None else None,
        "krx_gold_krw_g":  round(krx_gold_krw_g, 0) if krx_gold_krw_g is not None else None,
    }
    state.setdefault("history", []).append(entry)
    if len(state["history"]) > MAX_HISTORY:
        state["history"] = state["history"][-MAX_HISTORY:]


# ── 테더 김프용: 기존 방향성 알림 (변경 없음) ──────────

def should_alert(state: dict, key: str, current_value: float, now: datetime) -> tuple:
    """
    테더 김프 전용 — 방향성 기반 알림 판단 (기존 로직 유지)
    """
    last_alert = state.get("last_alert", {})
    prev = last_alert.get(key)

    if prev is None:
        return True, "첫 알림"

    prev_value = prev["value"]
    diff       = current_value - prev_value

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


# ── 금 김프용: 단계별 알림 ──────────────────────────────

def _get_gold_step_level(kimp_value: float, direction: str) -> int:
    """
    김프 값이 어느 '단계'에 있는지 계산
    
    direction="low" (하락 알림):
      0% 이하 진입 = level 0
      -1% 이하     = level 1
      -2% 이하     = level 2  ...
    
    direction="high" (상승 알림):
      GOLD_KIMP_HIGH 이상 진입 = level 0
      +1% 추가     = level 1
      +2% 추가     = level 2  ...
    """
    if direction == "low":
        # GOLD_KIMP_LOW(예: 0%) 기준으로 아래로 얼마나 벗어났는지
        if kimp_value > GOLD_KIMP_LOW:
            return -1  # 아직 기준 미달 (알림 대상 아님)
        distance = GOLD_KIMP_LOW - kimp_value  # 양수
        return int(distance / GOLD_KIMP_STEP)  # 0, 1, 2, 3...
    else:  # high
        if kimp_value < GOLD_KIMP_HIGH:
            return -1
        distance = kimp_value - GOLD_KIMP_HIGH
        return int(distance / GOLD_KIMP_STEP)


def should_alert_gold_step(state: dict, key: str, current_value: float,
                           direction: str, now: datetime) -> tuple:
    """
    금 김프 단계별 알림 판단
    
    알림 발생 조건:
    1) 첫 진입 (이전 상태 없음)
    2) 새로운 단계에 진입 (level이 이전보다 높아짐)
    
    알림 안 하는 경우:
    - 같은 단계 내에서 소폭 변동
    - 개선 방향 (level이 낮아짐)
    """
    current_level = _get_gold_step_level(current_value, direction)
    
    if current_level < 0:
        # 기준 미달 — 알림 대상 아님
        return False, "", current_level
    
    last_alert = state.get("last_alert", {})
    prev = last_alert.get(key)
    
    if prev is None:
        # 첫 진입
        threshold = GOLD_KIMP_LOW if direction == "low" else GOLD_KIMP_HIGH
        reason = f"첫 알림 (기준 {threshold}% 돌파, Level {current_level})"
        return True, reason, current_level
    
    prev_level = prev.get("step_level", 0)
    
    if current_level > prev_level:
        # 새 단계 진입 (악화)
        if direction == "low":
            step_threshold = GOLD_KIMP_LOW - (current_level * GOLD_KIMP_STEP)
            reason = (
                f"Level {prev_level}→{current_level} "
                f"({step_threshold:+.0f}% 선 돌파, "
                f"이전 {prev['value']:+.2f}% → 현재 {current_value:+.2f}%)"
            )
        else:
            step_threshold = GOLD_KIMP_HIGH + (current_level * GOLD_KIMP_STEP)
            reason = (
                f"Level {prev_level}→{current_level} "
                f"({step_threshold:+.0f}% 선 돌파, "
                f"이전 {prev['value']:+.2f}% → 현재 {current_value:+.2f}%)"
            )
        return True, reason, current_level
    
    # 같은 단계이거나 개선 방향
    if current_level < prev_level:
        print(f"  [Filter] {key}: Level {prev_level}→{current_level} (개선 방향) — 알림 생략")
    else:
        print(f"  [Filter] {key}: Level {current_level} 유지 "
              f"({prev['value']:+.2f}%→{current_value:+.2f}%) — 알림 생략")
    return False, "", current_level


def update_alert_state(state: dict, key: str, value: float, now: datetime,
                       step_level: int = None, extra: dict = None):
    """
    알림 상태 업데이트 (step_level, extra 데이터 포함 가능)
    """
    entry = {
        "value": round(value, 4),
        "time":  now.isoformat(),
    }
    if step_level is not None:
        entry["step_level"] = step_level
    if extra:
        entry.update(extra)
    state.setdefault("last_alert", {})[key] = entry


# ═══════════════════════════════════════════════════════
#  금 김프 변동 원인 분석
# ═══════════════════════════════════════════════════════

def analyze_gold_kimp_driver(
    state: dict,
    current_usd_krw: float,
    current_intl_gold_oz: float,
    current_krx_gold_g: float,
) -> str:
    """
    금 김프 변동의 주요 원인을 분석합니다.
    
    이전 상태(state.history 또는 last_alert)와 비교하여
    환율 / 국제금값 / 국내금값 각각의 변동률을 계산하고
    어느 요인이 김프 변동을 주도했는지 판별합니다.
    
    Returns:
        원인 분석 문자열 (예: "📌 주요인: 환율 상승 (+0.8%)")
    """
    # 이전 데이터 찾기: last_alert → history 순으로 탐색
    prev_fx = None
    prev_intl_gold = None
    prev_krx_gold = None
    prev_time = None
    
    # 1) last_alert에서 금 관련 이전 데이터 확인
    last_alert = state.get("last_alert", {})
    for key in ["gold_low", "gold_high"]:
        if key in last_alert and "usd_krw" in last_alert[key]:
            prev_fx = last_alert[key].get("usd_krw")
            prev_intl_gold = last_alert[key].get("intl_gold_usd_oz")
            prev_krx_gold = last_alert[key].get("krx_gold_krw_g")
            prev_time = last_alert[key].get("time", "")
            break
    
    # 2) last_alert에 없으면 history에서 마지막 유효 데이터
    if prev_fx is None:
        for entry in reversed(state.get("history", [])):
            if entry.get("usd_krw") is not None and entry.get("intl_gold_usd_oz") is not None:
                prev_fx = entry["usd_krw"]
                prev_intl_gold = entry.get("intl_gold_usd_oz")
                prev_krx_gold = entry.get("krx_gold_krw_g")
                prev_time = entry.get("time", "")
                break
    
    if prev_fx is None or prev_intl_gold is None:
        return "📌 원인 분석: 이전 데이터 없음 (첫 실행)"
    
    # 변동률 계산
    fx_change_pct = ((current_usd_krw - prev_fx) / prev_fx) * 100
    intl_gold_change_pct = ((current_intl_gold_oz - prev_intl_gold) / prev_intl_gold) * 100
    
    krx_change_pct = None
    if prev_krx_gold and prev_krx_gold > 0:
        krx_change_pct = ((current_krx_gold_g - prev_krx_gold) / prev_krx_gold) * 100
    
    # 기간 표시
    period_str = ""
    if prev_time:
        try:
            prev_dt = datetime.fromisoformat(prev_time)
            now_dt = datetime.now(KST)
            delta = now_dt - prev_dt
            hours = delta.total_seconds() / 3600
            if hours < 1:
                period_str = f"{int(delta.total_seconds() / 60)}분 전 대비"
            elif hours < 24:
                period_str = f"{hours:.1f}시간 전 대비"
            else:
                period_str = f"{delta.days}일 전 대비"
        except Exception:
            pass
    
    # ── 주요인 판별 ──
    # 금 김프 = (국내금 - 국제금×환율) / (국제금×환율)
    # 김프 하락 요인:
    #   - 환율 상승 → 국제금(원화 환산) 상승 → 김프 하락
    #   - 국제금값 상승 → 국제금(원화 환산) 상승 → 김프 하락
    #   - 국내금값 하락 → 김프 하락
    
    # 국제금(원화환산) 변동 = 환율변동 + 금값변동 (근사)
    intl_krw_change_approx = fx_change_pct + intl_gold_change_pct
    
    factors = []
    
    # 각 요인의 영향도 (절대값 기준으로 정렬)
    factor_list = [
        ("환율", fx_change_pct, current_usd_krw, prev_fx, "원"),
        ("국제금", intl_gold_change_pct, current_intl_gold_oz, prev_intl_gold, "$/oz"),
    ]
    if krx_change_pct is not None:
        factor_list.append(
            ("국내금", krx_change_pct, current_krx_gold_g, prev_krx_gold, "원/g")
        )
    
    # 영향도 순 정렬
    factor_list.sort(key=lambda x: abs(x[1]), reverse=True)
    
    lines = []
    if period_str:
        lines.append(f"📌 변동 원인 ({period_str})")
    else:
        lines.append("📌 변동 원인 분석")
    
    for name, change_pct, current, prev, unit in factor_list:
        if abs(change_pct) < 0.01:
            arrow = "→"
            tag = "변동없음"
        elif change_pct > 0:
            arrow = "↑"
            tag = "상승"
        else:
            arrow = "↓"
            tag = "하락"
        
        if unit == "원":
            lines.append(f"  {arrow} {name}: {prev:,.0f}→{current:,.0f}{unit} ({change_pct:+.2f}%, {tag})")
        elif unit == "$/oz":
            lines.append(f"  {arrow} {name}: ${prev:,.0f}→${current:,.0f} ({change_pct:+.2f}%, {tag})")
        else:
            lines.append(f"  {arrow} {name}: {prev:,.0f}→{current:,.0f}{unit} ({change_pct:+.2f}%, {tag})")
    
    # 주요인 한 줄 요약
    top_name, top_change, _, _, _ = factor_list[0]
    if abs(top_change) >= 0.05:
        if top_name == "환율":
            if top_change > 0:
                summary = f"💡 주요인: 환율 상승 → 국제금(원화) 비싸짐 → 김프 하락"
            else:
                summary = f"💡 주요인: 환율 하락 → 국제금(원화) 싸짐 → 김프 상승"
        elif top_name == "국제금":
            if top_change > 0:
                summary = f"💡 주요인: 국제금값 상승 → 국제금(원화) 비싸짐 → 김프 하락"
            else:
                summary = f"💡 주요인: 국제금값 하락 → 국제금(원화) 싸짐 → 김프 상승"
        elif top_name == "국내금":
            if top_change > 0:
                summary = f"💡 주요인: 국내금값 상승 → 김프 상승"
            else:
                summary = f"💡 주요인: 국내금값 하락 → 김프 하락"
        else:
            summary = f"💡 주요인: {top_name} ({top_change:+.2f}%)"
        lines.append(summary)
    else:
        lines.append("💡 모든 요인 소폭 변동 — 복합적 원인")
    
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════
#  데이터 수집 (기존과 동일 — 변경 없음)
# ═══════════════════════════════════════════════════════

def get_upbit_usdt_price() -> float:
    url     = "https://api.upbit.com/v1/ticker"
    params  = {"markets": "KRW-USDT"}
    headers = {"Accept": "application/json"}
    resp    = requests.get(url, params=params, headers=headers, timeout=10)
    resp.raise_for_status()
    price = float(resp.json()[0]["trade_price"])
    print(f"  [Upbit] USDT/KRW = {price:,.2f}")
    return price


def get_usd_krw_rate() -> float:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        )
    }
    today_kst = datetime.now(KST).strftime("%Y-%m-%d")

    try:
        url  = (
            "https://m.stock.naver.com/front-api/marketIndex/prices"
            "?category=exchange&reutersCode=FX_USDKRW"
        )
        resp = requests.get(url, headers=headers, timeout=10)
        resp.raise_for_status()
        data = resp.json()

        if data.get("isSuccess") and data.get("result"):
            item      = data["result"][0]
            traded_at = item.get("localTradedAt", "")
            rate      = float(item["closePrice"].replace(",", ""))

            if traded_at == today_kst:
                print(f"  [Naver] USD/KRW = {rate:,.2f}  (당일 {traded_at})")
            else:
                print(
                    f"  [Naver] USD/KRW = {rate:,.2f}"
                    f"  (최근 거래일 {traded_at} — 오늘 {today_kst}, 주말/공휴일 허용)"
                )
            return rate
    except Exception as e:
        print(f"  [Naver] 환율 API 실패: {e}")

    try:
        print("  [Yahoo] 폴백: KRW=X 시도...")
        ticker = yf.Ticker("KRW=X")
        rate   = float(ticker.fast_info.last_price)
        print(f"  [Yahoo] USD/KRW = {rate:,.2f}")
        return rate
    except Exception as e:
        print(f"  [Yahoo] 환율 실패: {e}")

    try:
        print("  [er-api] 폴백: 일간 환율 시도...")
        resp = requests.get("https://open.er-api.com/v6/latest/USD", timeout=10)
        resp.raise_for_status()
        rate = float(resp.json()["rates"]["KRW"])
        print(f"  [er-api] USD/KRW = {rate:,.2f}  (주의: 일간 업데이트)")
        return rate
    except Exception as e:
        print(f"  [er-api] 환율 실패: {e}")

    raise RuntimeError(
        "USD/KRW 환율을 가져올 수 있는 모든 소스(네이버·야후·er-api)가 응답하지 않습니다."
    )


def get_krx_gold_price_per_gram() -> float:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        )
    }

    try:
        url  = "https://api.stock.naver.com/marketindex/metals/M04020000"
        resp = requests.get(url, headers=headers, timeout=15)
        resp.raise_for_status()
        data  = resp.json()
        price = float(data["closePrice"].replace(",", ""))
        print(f"  [KRX Gold] 국내 금현물 = {price:,.0f} 원/g  (네이버 API)")
        return price
    except Exception as e:
        print(f"  [KRX Gold] 네이버 API 실패: {e}")

    try:
        url  = "https://finance.naver.com/marketindex/goldDetail.naver"
        resp = requests.get(url, headers=headers, timeout=15)
        resp.raise_for_status()
        text = resp.text
        for pattern in [r"([\d,]+\.\d+)\s*원/g", r"([\d,]+)\s*원/g"]:
            match = re.search(pattern, text)
            if match:
                price = float(match.group(1).replace(",", ""))
                print(f"  [KRX Gold] 국내 금현물 = {price:,.0f} 원/g  (데스크톱 파싱)")
                return price
    except Exception as e:
        print(f"  [KRX Gold] 데스크톱 파싱 실패: {e}")

    raise RuntimeError("KRX 금현물 가격을 파싱할 수 없습니다.")


def get_international_gold_usd_per_oz() -> float:
    try:
        url  = "https://forex-data-feed.swissquote.com/public-quotes/bboquotes/instrument/XAU/USD"
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        data   = resp.json()
        prices = data[0]["spreadProfilePrices"][0]
        bid    = prices["bid"]
        ask    = prices["ask"]
        spot   = (bid + ask) / 2

        if not (GOLD_PRICE_MIN_USD < spot < GOLD_PRICE_MAX_USD):
            raise ValueError(
                f"Swissquote 비정상값 감지: ${spot:,.2f}/oz"
                f"  (허용 범위 ${GOLD_PRICE_MIN_USD:,}~${GOLD_PRICE_MAX_USD:,})"
            )

        print(f"  [Swissquote] XAU/USD = ${spot:,.2f}/oz  (bid ${bid:,.2f} / ask ${ask:,.2f})")
        return spot
    except Exception as e:
        print(f"  [Swissquote] 실패: {e}")

    try:
        print("  [Yahoo] 폴백: GC=F 금 선물 시도...")
        ticker = yf.Ticker("GC=F")
        try:
            price = float(ticker.fast_info.last_price)
        except Exception:
            hist = ticker.history(period="1d")
            if hist.empty:
                raise RuntimeError("yfinance 히스토리 데이터 없음")
            price = float(hist["Close"].iloc[-1])

        if not (GOLD_PRICE_MIN_USD < price < GOLD_PRICE_MAX_USD):
            raise ValueError(f"비정상 금값 감지: ${price:,.2f}/oz")

        print(f"  [Yahoo] 국제 금 선물 = ${price:,.2f}/oz")
        return price
    except Exception as e:
        print(f"  [Yahoo] 금 선물 실패: {e}")

    raise RuntimeError("국제 금 시세를 가져올 수 없습니다.")


# ═══════════════════════════════════════════════════════
#  김프 계산 (기존과 동일)
# ═══════════════════════════════════════════════════════

def calc_usdt_kimp(upbit_usdt: float, usd_krw: float) -> float:
    return ((upbit_usdt - usd_krw) / usd_krw) * 100


def calc_gold_kimp(
    krx_gold_krw_g: float,
    intl_gold_usd_oz: float,
    usd_krw: float,
) -> tuple:
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
    url     = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id":    TELEGRAM_CHAT_ID,
        "text":       message,
        "parse_mode": "HTML",
    }
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
    print(f"\n{'='*57}")
    print(f"  김치프리미엄 모니터  |  {now.strftime('%Y-%m-%d %H:%M:%S KST')}")
    print(f"  테더: 방향성 알림  |  금: 단계별 알림 ({GOLD_KIMP_STEP}%p 간격)")
    print(f"{'='*57}")

    # ── 0. 상태 로드 ────────────────────────────────────
    print("\n[0] 알림 상태 로드")
    state  = load_state()
    alerts = []

    # ── 1. USD/KRW 환율 ─────────────────────────────────
    print("\n[1] USD/KRW 환율 조회")
    try:
        usd_krw = get_usd_krw_rate()
    except Exception as e:
        msg = f"❌ USD/KRW 환율 조회 실패: {e}"
        print(msg)
        send_telegram(msg)
        sys.exit(1)

    # ── 2. 테더 김프 (기존 로직 유지) ───────────────────
    print("\n[2] 테더 김프 계산")
    usdt_kimp  = None
    upbit_usdt = None
    try:
        upbit_usdt = get_upbit_usdt_price()
        usdt_kimp  = calc_usdt_kimp(upbit_usdt, usd_krw)
        print(f"  ▶ 테더 김프 = {usdt_kimp:+.2f}%")

        if usdt_kimp <= USDT_KIMP_LOW:
            send_it, reason = should_alert(state, "usdt_low", usdt_kimp, now)
            if send_it:
                emoji     = "🔵" if usdt_kimp < 0 else "🟡"
                alert_msg = (
                    f"{emoji} <b>테더 김프 알림</b> (≤{USDT_KIMP_LOW}%, {reason})\n"
                    f"김프: <b>{usdt_kimp:+.2f}%</b>\n"
                    f"Upbit USDT: {upbit_usdt:,.0f}원\n"
                    f"환율: {usd_krw:,.2f}원\n"
                    f"⏰ {now.strftime('%H:%M KST')}"
                )
                alerts.append(alert_msg)
                update_alert_state(state, "usdt_low", usdt_kimp, now)
            state.get("last_alert", {}).pop("usdt_high", None)

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
                update_alert_state(state, "usdt_high", usdt_kimp, now)
            state.get("last_alert", {}).pop("usdt_low", None)

        else:
            la = state.get("last_alert", {})
            if "usdt_low" in la or "usdt_high" in la:
                la.pop("usdt_low",  None)
                la.pop("usdt_high", None)
                print("  [State] 테더 정상 복귀 → 상태 초기화")

    except Exception as e:
        print(f"  ⚠ 테더 김프 계산 실패: {e}")

    # ── 3. 금 김프 (★ 단계별 알림 + 원인 분석) ────────
    print("\n[3] 금 김프 계산")
    gold_kimp       = None
    krx_gold        = None
    intl_gold_oz    = None
    intl_gold_krw_g = None
    try:
        krx_gold                   = get_krx_gold_price_per_gram()
        intl_gold_oz               = get_international_gold_usd_per_oz()
        gold_kimp, intl_gold_krw_g = calc_gold_kimp(krx_gold, intl_gold_oz, usd_krw)

        print(f"  ▶ 금 김프 = {gold_kimp:+.2f}%")
        print(f"    국내: {krx_gold:,.0f}원/g  |  국제: {intl_gold_krw_g:,.0f}원/g")
        print(f"    단계 기준: {GOLD_KIMP_LOW}% 이하 진입 시, {GOLD_KIMP_STEP}%p 간격 알림")

        # 원인 분석 (알림 여부와 무관하게 항상 수행)
        driver_analysis = analyze_gold_kimp_driver(
            state, usd_krw, intl_gold_oz, krx_gold
        )
        print(f"  {driver_analysis}")

        # ── 하락 방향 알림 (단계별) ──
        if gold_kimp <= GOLD_KIMP_LOW:
            send_it, reason, step_level = should_alert_gold_step(
                state, "gold_low", gold_kimp, "low", now
            )
            if send_it:
                alert_msg = (
                    f"🔵 <b>금 김프 알림</b> (≤{GOLD_KIMP_LOW}%, {reason})\n"
                    f"김프: <b>{gold_kimp:+.2f}%</b>\n"
                    f"국내: {krx_gold:,.0f}원/g\n"
                    f"국제: {intl_gold_krw_g:,.0f}원/g  (${intl_gold_oz:,.2f}/oz)\n"
                    f"환율: {usd_krw:,.2f}원\n"
                    f"\n{driver_analysis}\n"
                    f"\n⏰ {now.strftime('%H:%M KST')}"
                )
                alerts.append(alert_msg)
                update_alert_state(
                    state, "gold_low", gold_kimp, now,
                    step_level=step_level,
                    extra={
                        "usd_krw": round(usd_krw, 2),
                        "intl_gold_usd_oz": round(intl_gold_oz, 2),
                        "krx_gold_krw_g": round(krx_gold, 0),
                    }
                )
            else:
                # 알림은 안 보내지만, 비교용 데이터는 갱신
                # (다음 알림의 원인 분석 정확도를 위해)
                pass

            # 반대 방향 상태 클리어
            state.get("last_alert", {}).pop("gold_high", None)

        # ── 상승 방향 알림 (단계별) ──
        elif gold_kimp >= GOLD_KIMP_HIGH:
            send_it, reason, step_level = should_alert_gold_step(
                state, "gold_high", gold_kimp, "high", now
            )
            if send_it:
                alert_msg = (
                    f"🔴 <b>금 김프 알림</b> (≥{GOLD_KIMP_HIGH}%, {reason})\n"
                    f"김프: <b>{gold_kimp:+.2f}%</b>\n"
                    f"국내: {krx_gold:,.0f}원/g\n"
                    f"국제: {intl_gold_krw_g:,.0f}원/g  (${intl_gold_oz:,.2f}/oz)\n"
                    f"환율: {usd_krw:,.2f}원\n"
                    f"\n{driver_analysis}\n"
                    f"\n⏰ {now.strftime('%H:%M KST')}"
                )
                alerts.append(alert_msg)
                update_alert_state(
                    state, "gold_high", gold_kimp, now,
                    step_level=step_level,
                    extra={
                        "usd_krw": round(usd_krw, 2),
                        "intl_gold_usd_oz": round(intl_gold_oz, 2),
                        "krx_gold_krw_g": round(krx_gold, 0),
                    }
                )
            state.get("last_alert", {}).pop("gold_low", None)

        # ── 정상 범위 복귀 ──
        else:
            la = state.get("last_alert", {})
            if "gold_low" in la or "gold_high" in la:
                la.pop("gold_low",  None)
                la.pop("gold_high", None)
                print("  [State] 금 김프 정상 복귀 → 상태 초기화")

    except Exception as e:
        print(f"  ⚠ 금 김프 계산 실패: {e}")

    # ── 이력 기록 (환율/금값 포함) ───────────────────────
    add_history(state, usdt_kimp, gold_kimp, now,
                usd_krw=usd_krw,
                intl_gold_usd_oz=intl_gold_oz,
                krx_gold_krw_g=krx_gold)

    # ── 4. 결과 요약 출력 ───────────────────────────────
    print(f"\n{'─'*57}")
    usdt_str = f"{usdt_kimp:+.2f}%" if usdt_kimp is not None else "N/A"
    gold_str = f"{gold_kimp:+.2f}%" if gold_kimp is not None else "N/A"
    print(f"  요약  : 테더 김프 = {usdt_str}  |  금 김프 = {gold_str}")
    print(f"  조건  : 테더 ≤{USDT_KIMP_LOW}% 또는 ≥{USDT_KIMP_HIGH}%")
    print(f"          금   ≤{GOLD_KIMP_LOW}% 또는 ≥{GOLD_KIMP_HIGH}% (단계: {GOLD_KIMP_STEP}%p)")

    # 금 김프 현재 단계 표시
    if gold_kimp is not None:
        if gold_kimp <= GOLD_KIMP_LOW:
            cur_level = _get_gold_step_level(gold_kimp, "low")
            next_trigger = GOLD_KIMP_LOW - ((cur_level + 1) * GOLD_KIMP_STEP)
            print(f"          금 현재 Level {cur_level} — 다음 알림: {next_trigger:+.0f}% 이하")
        elif gold_kimp >= GOLD_KIMP_HIGH:
            cur_level = _get_gold_step_level(gold_kimp, "high")
            next_trigger = GOLD_KIMP_HIGH + ((cur_level + 1) * GOLD_KIMP_STEP)
            print(f"          금 현재 Level {cur_level} — 다음 알림: {next_trigger:+.0f}% 이상")

    run_mode  = os.environ.get("RUN_MODE") or ""
    is_manual = run_mode == "workflow_dispatch"
    print(f"  모드  : {'수동' if is_manual else '스케줄'}  (RUN_MODE={run_mode!r})")

    # ── 5. 수동 실행 시 현황 리포트 ────────────────────
    if is_manual and not alerts:
        report = (
            f"📊 <b>김프 현황 리포트</b> (수동 조회)\n\n"
            f"테더 김프: <b>{usdt_str}</b>\n"
            f"금 김프: <b>{gold_str}</b>\n"
        )
        if usdt_kimp is not None and upbit_usdt is not None:
            report += (
                f"\n[테더 상세]\n"
                f"  Upbit USDT: {upbit_usdt:,.0f}원\n"
                f"  환율: {usd_krw:,.2f}원\n"
                f"  기준: ≤{USDT_KIMP_LOW}% 또는 ≥{USDT_KIMP_HIGH}%\n"
            )
        if gold_kimp is not None and krx_gold is not None:
            report += (
                f"\n[금 상세]\n"
                f"  국내: {krx_gold:,.0f}원/g\n"
                f"  국제: {intl_gold_krw_g:,.0f}원/g  (${intl_gold_oz:,.2f}/oz)\n"
                f"  환율: {usd_krw:,.2f}원\n"
                f"  기준: ≤{GOLD_KIMP_LOW}% 또는 ≥{GOLD_KIMP_HIGH}% (단계: {GOLD_KIMP_STEP}%p)\n"
            )
            # 수동 조회에도 원인 분석 포함
            if 'driver_analysis' in dir():
                report += f"\n{driver_analysis}\n"
        report += f"\n⏰ {now.strftime('%Y-%m-%d %H:%M KST')}"
        alerts.append(report)

    # ── 6. 알림 전송 ────────────────────────────────────
    print(f"\n[4] 알림 전송 ({len(alerts)}건)")
    if alerts:
        for msg in alerts:
            send_telegram(msg)
    else:
        print("  알림 없음 (조건 미충족 / 같은 단계 내 변동 / 개선 방향)")

    # ── 7. 상태 저장 ────────────────────────────────────
    print("\n[5] 상태 저장")
    save_state(state)

    print(f"\n{'='*57}")
    print(f"  완료  |  {datetime.now(KST).strftime('%H:%M:%S KST')}")
    print(f"{'='*57}\n")


if __name__ == "__main__":
    main()
