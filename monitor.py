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
import argparse
import subprocess
import tempfile
from datetime import datetime, timezone, timedelta
from state_lock import StateLockError, state_execution_lock

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
# Importing this module does not read credentials or load network clients.
TELEGRAM_BOT_TOKEN = ""
TELEGRAM_CHAT_ID = ""
USDT_KIMP_LOW = GOLD_KIMP_LOW = 0.0
USDT_KIMP_HIGH = GOLD_KIMP_HIGH = 10.0
SOURCE_FAILURE_ALERT_AFTER = 3


def valid_number(value, label: str, *, positive=False) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{label}: boolean is not a price or threshold")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        raise ValueError(f"{label}: numeric value required") from None
    if not math.isfinite(number) or (positive and number <= 0):
        raise ValueError(f"{label}: finite{' positive' if positive else ''} value required")
    return number


def configure(environ):
    values = {
        name: valid_number(environ.get(name) or default, name)
        for name, default in (
            ("USDT_KIMP_LOW", "0"), ("USDT_KIMP_HIGH", "10"),
            ("GOLD_KIMP_LOW", "0"), ("GOLD_KIMP_HIGH", "10"),
        )
    }
    for asset in ("USDT", "GOLD"):
        if values[f"{asset}_KIMP_LOW"] >= values[f"{asset}_KIMP_HIGH"]:
            raise ValueError(f"{asset}: LOW must be less than HIGH")
    globals().update(values)
    globals()["TELEGRAM_BOT_TOKEN"] = environ.get("TELEGRAM_BOT_TOKEN") or ""
    globals()["TELEGRAM_CHAT_ID"] = environ.get("TELEGRAM_CHAT_ID") or ""


def _requests():
    import requests
    return requests


def _ticker(symbol):
    import yfinance
    return yfinance.Ticker(symbol)


# ═══════════════════════════════════════════════════════
#  상태 관리
# ═══════════════════════════════════════════════════════

class StateError(RuntimeError):
    """State could not be loaded, persisted, or published safely."""


def validate_state(state: dict):
    if not isinstance(state, dict):
        raise ValueError("state must be an object")
    if not isinstance(state.get("history"), list) or not isinstance(state.get("last_alert"), dict):
        raise ValueError("history/list and last_alert/object are required")
    json.dumps(state, allow_nan=False)
    for entry in [*state["history"], *state["last_alert"].values()]:
        if not isinstance(entry, dict) or not isinstance(entry.get("time"), str):
            raise ValueError("state entry requires a timestamp")
        datetime.fromisoformat(entry["time"])
        for name in ("value", "usdt_kimp", "gold_kimp", "usd_krw", "intl_gold_usd_oz", "krx_gold_krw_g"):
            value = entry.get(name)
            if value is not None:
                if not isinstance(value, (int, float)) or isinstance(value, bool):
                    raise ValueError("state measurements must be JSON numbers")
                valid_number(value, name, positive=name in ("usd_krw", "intl_gold_usd_oz", "krx_gold_krw_g"))
    for key, entry in state["last_alert"].items():
        if key not in ("usdt_low", "usdt_high", "gold_low", "gold_high") or entry.get("value") is None:
            raise ValueError("invalid alert entry")
        if "step_level" in entry and (type(entry["step_level"]) is not int or entry["step_level"] < 0):
            raise ValueError("invalid alert step level")
    health = state.get("health", {})
    if not isinstance(health, dict):
        raise ValueError("health must be an object")
    for entry in health.values():
        if not isinstance(entry, dict) or entry.get("status") not in ("ok", "error"):
            raise ValueError("invalid source health")
        if type(entry.get("consecutive_failures")) is not int or entry["consecutive_failures"] < 0:
            raise ValueError("invalid failure counter")
        for name in ("last_success", "last_failure", "first_failure", "last_warning"):
            if name in entry:
                datetime.fromisoformat(entry[name])


def load_state(state_file=None) -> dict:
    path = state_file or STATE_FILE
    try:
        with open(path, "r", encoding="utf-8") as f:
            state = json.load(f)
        validate_state(state)
    except FileNotFoundError:
        print("  [State] 신규 생성")
        return {"history": [], "last_alert": {}}
    except (OSError, ValueError, TypeError, OverflowError) as e:
        raise StateError(f"load failed ({type(e).__name__}); existing file preserved") from None
    print(f"  [State] 로드 성공: 이력 {len(state['history'])}건")
    return state


def publish_state(state_file):
    directory = os.path.dirname(os.path.abspath(__file__))
    actual = os.path.normcase(os.path.realpath(state_file))
    expected_path = os.path.normcase(os.path.realpath(os.path.join(directory, "state.json")))
    if actual != expected_path:
        raise StateError("only the repository state.json can be published")

    def git(*args, allowed=(0,), output=False):
        operation = "commit" if "commit" in args else args[0]
        try:
            result = subprocess.run(
                ["git", *args], cwd=directory, capture_output=True, text=True, timeout=30,
            )
        except (OSError, UnicodeError, subprocess.TimeoutExpired) as e:
            raise StateError(f"git {operation} failed ({type(e).__name__})") from None
        if result.returncode not in allowed:
            raise StateError(f"git {operation} failed (rc={result.returncode})")
        if output:
            if not isinstance(getattr(result, "stdout", None), str):
                raise StateError(f"git {operation} returned invalid output")
            return result.stdout
        return result.returncode

    git("add", "--", "state.json")
    changed = git("diff", "--cached", "--quiet", "--", "state.json", allowed=(0, 1))
    push_args = ("push",)
    if changed == 0:
        # A previous commit may have succeeded while both pushes failed. Query
        # only local refs; uncertainty is an error, never evidence of publication.
        branch = git("symbolic-ref", "--quiet", "HEAD", output=True).strip()
        if not branch.startswith("refs/heads/") or any(c.isspace() for c in branch):
            raise StateError("state publication requires an attached branch")
        fields = git(
            "for-each-ref",
            "--format=%(refname)%00%(upstream)%00%(upstream:remotename)%00%(upstream:remoteref)",
            branch, output=True,
        ).rstrip("\n").split("\0")
        if len(fields) != 4 or fields[0] != branch:
            raise StateError("state publication upstream does not match the branch")
        _, upstream, remote, remote_ref = fields
        if (not remote or remote == "." or remote.startswith("-")
                or any(c.isspace() for c in remote + upstream + remote_ref)
                or not upstream.startswith(f"refs/remotes/{remote}/")
                or not remote_ref.startswith("refs/heads/")
                or remote_ref == "refs/heads/"):
            raise StateError("state publication requires a remote branch upstream")

        def revision(ref):
            value = git("rev-parse", "--verify", ref, output=True).strip()
            if not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", value):
                raise StateError("state publication returned an invalid revision")
            return value

        head, base = revision("HEAD"), revision(upstream)
        counts = git("rev-list", "--left-right", "--count", f"{base}...{head}", output=True).strip()
        match = re.fullmatch(r"([0-9]+)\s+([0-9]+)", counts)
        if match is None:
            raise StateError("state publication returned invalid ahead/behind counts")
        behind, ahead = map(int, match.groups())
        if behind:
            raise StateError("state publication branch is behind or diverged from upstream")
        if not ahead:
            if head != base:
                raise StateError("state publication revision/count mismatch")
            print("  [State] 변경사항·미게시 커밋 없음 — push 생략")
            return

        commits = git("rev-list", "--reverse", f"{base}..{head}", output=True).splitlines()
        if len(commits) != ahead or not commits or commits[-1] != head:
            raise StateError("state publication pending history is inconsistent")
        previous = base
        for commit in commits:
            if not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", commit):
                raise StateError("state publication pending revision is invalid")
            metadata = git("show", "-s", "--format=%P%x00%an%x00%ae%x00%cn%x00%ce%x00%B",
                           commit, output=True).rstrip("\n").split("\0")
            expected = [previous, "kimp-bot", "bot@kimp-monitor", "kimp-bot",
                        "bot@kimp-monitor", "update state [skip ci]"]
            if metadata != expected:
                raise StateError("pending history contains an untrusted or non-linear commit")
            names = git("diff-tree", "--no-commit-id", "--name-only", "--no-renames", "-r", "-z",
                        commit, "--", output=True)
            if names != "state.json\0":
                raise StateError("pending history is not exclusively state.json changes")
            previous = commit
        # Freeze the verified tip and target. push.default, remote push refspecs
        # and automatic tag following must not publish unrelated user work.
        push_args = ("push", "--no-follow-tags", "--", remote, f"{head}:{remote_ref}")
    else:
        git("-c", "user.name=kimp-bot", "-c", "user.email=bot@kimp-monitor",
            "commit", "--only", "-m", "update state [skip ci]", "--", "state.json")
    # Retry the same push once; never fetch/rebase/force over competing state.
    for attempt in range(2):
        try:
            git(*push_args)
            print("  [State] git push 완료")
            return
        except StateError:
            if attempt == 1:
                raise
            print("  [State] push 실패 — 동일 커밋 1회 재시도")


def save_state(state: dict, *, publish=False, state_file=None):
    path = os.path.abspath(state_file or STATE_FILE)
    temporary = None
    try:
        validate_state(state)
        payload = json.dumps(state, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=os.path.dirname(path),
                                         prefix=".state-", suffix=".tmp", delete=False) as f:
            temporary = f.name
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, path)
        temporary = None
        print("  [State] 파일 저장 완료")
    except (OSError, ValueError, TypeError, OverflowError) as e:
        raise StateError(f"save failed ({type(e).__name__}); previous file preserved") from None
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                pass
    if publish:
        publish_state(path)


def add_history(state: dict, usdt_kimp, gold_kimp, now: datetime,
                usd_krw=None, intl_gold_usd_oz=None, krx_gold_krw_g=None):
    for label, value in (("usdt_kimp", usdt_kimp), ("gold_kimp", gold_kimp),
                         ("usd_krw", usd_krw), ("intl_gold_usd_oz", intl_gold_usd_oz),
                         ("krx_gold_krw_g", krx_gold_krw_g)):
        if value is not None:
            valid_number(value, label, positive=label not in ("usdt_kimp", "gold_kimp"))
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
    current_value = valid_number(current_value, "premium")
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
    kimp_value = valid_number(kimp_value, "gold premium")
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
    value = valid_number(value, "alert premium")
    entry = {
        # Keep comparison precision: rounding can make an identical quote look worse.
        "value": value,
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
    resp    = _requests().get(url, params=params, headers=headers, timeout=10)
    resp.raise_for_status()
    price = valid_number(resp.json()[0]["trade_price"], "Upbit KRW/USDT", positive=True)
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
        resp = _requests().get(url, headers=headers, timeout=10)
        resp.raise_for_status()
        data = resp.json()

        if data.get("isSuccess") and data.get("result"):
            item      = data["result"][0]
            traded_at = item.get("localTradedAt", "")
            rate      = valid_number(item["closePrice"].replace(",", ""), "Naver KRW/USD", positive=True)

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
        ticker = _ticker("KRW=X")
        rate   = valid_number(ticker.fast_info.last_price, "Yahoo KRW/USD", positive=True)
        print(f"  [Yahoo] USD/KRW = {rate:,.2f}")
        return rate
    except Exception as e:
        print(f"  [Yahoo] 환율 실패: {e}")

    try:
        print("  [er-api] 폴백: 일간 환율 시도...")
        resp = _requests().get("https://open.er-api.com/v6/latest/USD", timeout=10)
        resp.raise_for_status()
        rate = valid_number(resp.json()["rates"]["KRW"], "er-api KRW/USD", positive=True)
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
        resp = _requests().get(url, headers=headers, timeout=15)
        resp.raise_for_status()
        data  = resp.json()
        price = valid_number(data["closePrice"].replace(",", ""), "KRX KRW/g", positive=True)
        print(f"  [KRX Gold] 국내 금현물 = {price:,.0f} 원/g  (네이버 API)")
        return price
    except Exception as e:
        print(f"  [KRX Gold] 네이버 API 실패: {e}")

    try:
        url  = "https://finance.naver.com/marketindex/goldDetail.naver"
        resp = _requests().get(url, headers=headers, timeout=15)
        resp.raise_for_status()
        text = resp.text
        for pattern in [r"([\d,]+\.\d+)\s*원/g", r"([\d,]+)\s*원/g"]:
            match = re.search(pattern, text)
            if match:
                price = valid_number(match.group(1).replace(",", ""), "KRX KRW/g", positive=True)
                print(f"  [KRX Gold] 국내 금현물 = {price:,.0f} 원/g  (데스크톱 파싱)")
                return price
    except Exception as e:
        print(f"  [KRX Gold] 데스크톱 파싱 실패: {e}")

    raise RuntimeError("KRX 금현물 가격을 파싱할 수 없습니다.")


def get_international_gold_usd_per_oz() -> float:
    try:
        url  = "https://forex-data-feed.swissquote.com/public-quotes/bboquotes/instrument/XAU/USD"
        resp = _requests().get(url, timeout=10)
        resp.raise_for_status()
        data   = resp.json()
        prices = data[0]["spreadProfilePrices"][0]
        bid    = valid_number(prices["bid"], "gold bid USD/oz", positive=True)
        ask    = valid_number(prices["ask"], "gold ask USD/oz", positive=True)
        if bid > ask:
            raise ValueError("gold bid exceeds ask")
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
        ticker = _ticker("GC=F")
        try:
            price = valid_gold_price(ticker.fast_info.last_price)
        except Exception:
            hist = ticker.history(period="1d")
            if hist.empty:
                raise RuntimeError("yfinance 히스토리 데이터 없음")
            price = valid_gold_price(hist["Close"].iloc[-1])

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
    upbit_usdt = valid_number(upbit_usdt, "Upbit KRW/USDT", positive=True)
    usd_krw = valid_number(usd_krw, "KRW/USD", positive=True)
    return valid_number(((upbit_usdt - usd_krw) / usd_krw) * 100, "USDT premium")


def valid_gold_price(value):
    price = valid_number(value, "international gold USD/oz", positive=True)
    if not GOLD_PRICE_MIN_USD < price < GOLD_PRICE_MAX_USD:
        raise ValueError("international gold outside allowed USD/oz range")
    return price


def calc_gold_kimp(
    krx_gold_krw_g: float,
    intl_gold_usd_oz: float,
    usd_krw: float,
) -> tuple:
    krx_gold_krw_g = valid_number(krx_gold_krw_g, "KRX KRW/g", positive=True)
    intl_gold_usd_oz = valid_gold_price(intl_gold_usd_oz)
    usd_krw = valid_number(usd_krw, "KRW/USD", positive=True)
    intl_gold_krw_g = (intl_gold_usd_oz * usd_krw) / TROY_OUNCE_TO_GRAM
    intl_gold_krw_g = valid_number(intl_gold_krw_g, "international gold KRW/g", positive=True)
    kimp = ((krx_gold_krw_g - intl_gold_krw_g) / intl_gold_krw_g) * 100
    return valid_number(kimp, "gold premium"), intl_gold_krw_g


# ═══════════════════════════════════════════════════════
#  알림
# ═══════════════════════════════════════════════════════

def send_telegram(message: str) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("  [Telegram] 토큰/채팅ID 미설정 — 알림 건너뜀")
        return False
    url     = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id":    TELEGRAM_CHAT_ID,
        "text":       message,
        "parse_mode": "HTML",
    }
    try:
        resp = _requests().post(url, json=payload, timeout=10)
        if resp.ok and resp.json().get("ok") is True:
            print("  [Telegram] 알림 전송 성공")
            return True
        else:
            print(f"  [Telegram] 전송 실패: HTTP {resp.status_code}")
    except Exception as e:
        # 예외 문자열에 요청 URL(봇 토큰 포함)이 실릴 수 있어 타입만 남긴다 (2026-08-27 보안점검)
        print(f"  [Telegram] 전송 오류: {type(e).__name__}")
    return False


# ═══════════════════════════════════════════════════════
#  메인
# ═══════════════════════════════════════════════════════


def record_source_result(state, source, now, error=None):
    health = state.setdefault("health", {}).setdefault(source, {})
    if error is None:
        health.update(status="ok", last_success=now.isoformat(), consecutive_failures=0)
        for key in ("first_failure", "last_warning", "error_type"):
            health.pop(key, None)
    else:
        health.update(status="error", last_failure=now.isoformat(), error_type=type(error).__name__)
        health["consecutive_failures"] = health.get("consecutive_failures", 0) + 1
        health.setdefault("first_failure", now.isoformat())


def finish_run(state, now, failed_sources, failed_alerts, *, send_alerts,
               persist_state, publish, state_file):
    for source in failed_sources:
        health = state["health"][source]
        threshold = 1 if source == "fx" else SOURCE_FAILURE_ALERT_AFTER
        if health["consecutive_failures"] >= threshold and "last_warning" not in health:
            warning = (f"⚠ 시세 수집 실패: {source} "
                       f"({health['consecutive_failures']}회 연속, {health['error_type']})")
            if send_alerts:
                if send_telegram(warning):
                    health["last_warning"] = now.isoformat()
                else:
                    failed_alerts += 1
            else:
                print(f"  [Dry run] {warning}")
    status = "ok"
    if failed_sources or failed_alerts:
        status = "failed" if "fx" in failed_sources or len(failed_sources) == 2 else "partial_failure"
    state["run"] = {"time": now.isoformat(), "status": status,
                    "failed_sources": list(failed_sources), "failed_alerts": failed_alerts}
    if persist_state:
        try:
            save_state(state, publish=publish, state_file=state_file)
        except StateError as error:
            print(f"  [State] {error}")
            return 1
    else:
        print("  [Dry run] 알림 전송·상태 저장·Git 게시 없음")
    print(f"  결과: {status}")
    return int(status != "ok")


def run_monitor(*, send_alerts=False, persist_state=False, publish=False,
                state_file=None, run_mode="", now=None):
    options = dict(send_alerts=send_alerts, persist_state=persist_state, publish=publish,
                   state_file=state_file, run_mode=run_mode, now=now)
    if not (send_alerts or persist_state or publish):
        return _run_monitor(**options)
    try:
        # Hold one OS lock from the first state read through delivery and publication.
        with state_execution_lock(state_file or STATE_FILE) as canonical_state:
            options["state_file"] = canonical_state
            return _run_monitor(**options)
    except StateLockError as error:
        print(f"  [State] {error}")
        return 1


def _run_monitor(*, send_alerts=False, persist_state=False, publish=False,
                 state_file=None, run_mode="", now=None):
    now = now or datetime.now(KST)
    print(f"\n{'='*57}")
    print(f"  김치프리미엄 모니터  |  {now.strftime('%Y-%m-%d %H:%M:%S KST')}")
    print(f"  테더: 방향성 알림  |  금: 단계별 알림 ({GOLD_KIMP_STEP}%p 간격)")
    print(f"{'='*57}")

    # ── 0. 상태 로드 ────────────────────────────────────
    print("\n[0] 알림 상태 로드")
    try:
        state = load_state(state_file)
    except StateError as error:
        print(f"  [State] {error}")
        return 1
    alerts = []
    failed_sources = []

    # ── 1. USD/KRW 환율 ─────────────────────────────────
    print("\n[1] USD/KRW 환율 조회")
    try:
        usd_krw = valid_number(get_usd_krw_rate(), "KRW/USD", positive=True)
        record_source_result(state, "fx", now)
    except Exception as e:
        print(f"❌ USD/KRW 환율 조회 실패: {type(e).__name__}")
        record_source_result(state, "fx", now, e)
        return finish_run(state, now, ["fx"], 0, send_alerts=send_alerts,
                          persist_state=persist_state, publish=publish, state_file=state_file)

    # ── 2. 테더 김프 (기존 로직 유지) ───────────────────
    print("\n[2] 테더 김프 계산")
    usdt_kimp  = None
    upbit_usdt = None
    try:
        upbit_usdt = valid_number(get_upbit_usdt_price(), "KRW/USDT", positive=True)
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
                alerts.append((alert_msg, "usdt_low", usdt_kimp, None, None))
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
                alerts.append((alert_msg, "usdt_high", usdt_kimp, None, None))
            state.get("last_alert", {}).pop("usdt_low", None)

        else:
            la = state.get("last_alert", {})
            if "usdt_low" in la or "usdt_high" in la:
                la.pop("usdt_low",  None)
                la.pop("usdt_high", None)
                print("  [State] 테더 정상 복귀 → 상태 초기화")

        record_source_result(state, "usdt", now)

    except Exception as e:
        usdt_kimp = upbit_usdt = None
        failed_sources.append("usdt")
        record_source_result(state, "usdt", now, e)
        print(f"  ⚠ 테더 김프 계산 실패: {type(e).__name__}")

    # ── 3. 금 김프 (★ 단계별 알림 + 원인 분석) ────────
    print("\n[3] 금 김프 계산")
    gold_kimp       = None
    krx_gold        = None
    intl_gold_oz    = None
    intl_gold_krw_g = None
    try:
        krx_gold                   = valid_number(get_krx_gold_price_per_gram(), "KRX KRW/g", positive=True)
        intl_gold_oz               = valid_gold_price(get_international_gold_usd_per_oz())
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
                alerts.append((
                    alert_msg, "gold_low", gold_kimp, step_level,
                    {
                        "usd_krw": round(usd_krw, 2),
                        "intl_gold_usd_oz": round(intl_gold_oz, 2),
                        "krx_gold_krw_g": round(krx_gold, 0),
                    }
                ))
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
                alerts.append((
                    alert_msg, "gold_high", gold_kimp, step_level,
                    {
                        "usd_krw": round(usd_krw, 2),
                        "intl_gold_usd_oz": round(intl_gold_oz, 2),
                        "krx_gold_krw_g": round(krx_gold, 0),
                    }
                ))
            state.get("last_alert", {}).pop("gold_low", None)

        # ── 정상 범위 복귀 ──
        else:
            la = state.get("last_alert", {})
            if "gold_low" in la or "gold_high" in la:
                la.pop("gold_low",  None)
                la.pop("gold_high", None)
                print("  [State] 금 김프 정상 복귀 → 상태 초기화")

        record_source_result(state, "gold", now)

    except Exception as e:
        gold_kimp = krx_gold = intl_gold_oz = intl_gold_krw_g = None
        failed_sources.append("gold")
        record_source_result(state, "gold", now, e)
        print(f"  ⚠ 금 김프 계산 실패: {type(e).__name__}")

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
        alerts.append((report, None, None, None, None))

    # ── 6. 알림 전송 ────────────────────────────────────
    print(f"\n[4] 알림 전송 ({len(alerts)}건)")
    failed_alerts = 0
    if alerts:
        for msg, key, value, step_level, extra in alerts:
            if not send_alerts:
                print(f"  [Dry run] 알림 후보: {key or 'manual_report'}")
                continue
            if send_telegram(msg):
                if key is not None:
                    update_alert_state(state, key, value, now, step_level=step_level, extra=extra)
            else:
                failed_alerts += 1
    else:
        print("  알림 없음 (조건 미충족 / 같은 단계 내 변동 / 개선 방향)")

    # ── 7. 상태 저장 ────────────────────────────────────
    print("\n[5] 상태 저장")
    result = finish_run(state, now, failed_sources, failed_alerts, send_alerts=send_alerts,
                        persist_state=persist_state, publish=publish, state_file=state_file)

    print(f"\n{'='*57}")
    print(f"  완료  |  {datetime.now(KST).strftime('%H:%M:%S KST')}")
    print(f"{'='*57}\n")
    return result


def main(argv=None, *, environ=None):
    parser = argparse.ArgumentParser(description="USDT/금 김프 모니터. 옵션 없이는 외부 작업을 실행하지 않습니다.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--live", action="store_true", help="시세 조회, 알림 전송, 상태 저장")
    mode.add_argument("--dry-run", action="store_true", help="시세 조회만 실행; 알림/저장/Git 없음")
    parser.add_argument("--publish-state", action="store_true", help="--live 상태를 Git commit/push (명시적 선택)")
    parser.add_argument("--state-file", default=STATE_FILE, help="로컬 상태 파일 경로")
    args = parser.parse_args(argv)
    if args.publish_state and (not args.live or os.path.abspath(args.state_file) != STATE_FILE):
        parser.error("--publish-state requires --live and the repository state.json")
    if not args.live and not args.dry_run:
        parser.print_help()
        return 0
    environ = os.environ if environ is None else environ
    try:
        configure(environ)
    except ValueError as error:
        print(f"  [Config] {error}")
        return 1
    return run_monitor(send_alerts=args.live, persist_state=args.live, publish=args.publish_state,
                       state_file=args.state_file, run_mode=environ.get("RUN_MODE") or "")


if __name__ == "__main__":
    sys.exit(main())
