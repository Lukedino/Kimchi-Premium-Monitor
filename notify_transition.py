"""워크플로 성공↔실패 전이 때만 Telegram 1통 (무상태, 표준 라이브러리 전용).

`python -B -S monitor.py --notify-transition` 으로 실행된다. 설치 단계가 실패해도
돌도록 서드파티 패키지와 monitor.py 의 나머지 코드를 import 하지 않는다.

- 이번 결과: MONITOR_OUTCOME(success|failure|cancelled|skipped) + JOB_STATUS.
  skipped(앞 스텝 실패로 수집이 건너뛰어짐)는 실패로 본다. cancelled 는 보내지 않는다
  (사람이 취소했거나 다음 실행에 밀린 것이라 실패도 회복도 아니다). 빈 값·알 수 없는 값은
  `skipped (unknown outcome)` 로 따로 남긴다.
- 직전 결과: 익명 GitHub REST 로 같은 브랜치(GITHUB_REF_NAME, 없으면 main)의 완료 run 을 받아 자기 run 을 뺀 뒤
  success/failure 로 판정되는 첫 건. 조회 실패면 보내지 않는다(PA GHA 감시가 백업).
- GH_API_TOKEN(잡의 GITHUB_TOKEN, actions: read)이 있으면 api.github.com 조회에만 Bearer 로
  붙인다(익명 한도 60회/시 회피). 리다이렉트를 따르지 않는 opener 를 쓰고 헤더는 unredirected 로
  넣어 다른 호스트로 넘어가지 않게 한다. Telegram 요청·로그에는 싣지 않는다. 없으면 익명.
- 공개 저장소다. 로그는 `notify: <sent|skipped|failed> (<고정 사유>)` 한 줄뿐이고
  시세·임계값·URL·토큰·예외 문구를 싣지 않는다. 예외는 클래스 이름만 남긴다.
- state.json 을 읽거나 쓰지 않는다. 종료 코드는 항상 0.
"""
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))
TIMEOUT_SECONDS = 10
MAX_RESPONSE_BYTES = 1_000_000
RUNS_URL = ("https://api.github.com/repos/Lukedino/Kimchi-Premium-Monitor/actions/workflows/"
            "kimp-monitor.yml/runs?status=completed&per_page=5")
HEADER = "김프 모니터(자체 알림)"
# GitHub run conclusion → 판정. 여기 없는 값(cancelled·skipped·neutral 등)은 건너뛴다.
_PREVIOUS = {"success": "success", "failure": "failure", "timed_out": "failure",
             "startup_failure": "failure"}
_STAGES = {"install": "설치", "collect": "수집", "publish": "게시"}


class HistoryUnavailable(Exception):
    """reason 은 고정 문구 또는 `http <숫자>` 뿐이다(URL·본문 금지)."""

    def __init__(self, reason=""):
        super().__init__(reason)
        self.reason = reason


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """3xx 를 따르지 않는다(HTTPError 로 끝나 조회 실패로 처리된다)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def _default_urlopen(request, timeout):
    return _OPENER.open(request, timeout=timeout)


def runs_url(branch):
    return f"{RUNS_URL}&branch={urllib.parse.quote(branch or 'main', safe='')}"


def _read_json(resp):
    return json.loads(resp.read(MAX_RESPONSE_BYTES).decode("utf-8"))


def current_result(monitor_outcome, job_status):
    """(결과, 단계). 결과는 success|failure|cancelled|unknown."""
    outcome = (monitor_outcome or "").strip().lower()
    job = (job_status or "").strip().lower()
    if outcome == "cancelled" or job == "cancelled":
        return "cancelled", None
    if job not in {"success", "failure"}:
        return "unknown", None
    if outcome == "skipped":
        return "failure", "install"
    if outcome == "failure":
        return "failure", "collect"
    if outcome == "success":
        if job == "failure":
            return "failure", "publish"
        return "success", None
    return "unknown", None  # 빈 값·알 수 없는 값은 판단하지 않는다


def previous_conclusion(run_id, branch, *, urlopen, api_token=""):
    request = urllib.request.Request(runs_url(branch), headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": "kimp-monitor-notify",
        "X-GitHub-Api-Version": "2022-11-28",
    })
    if api_token:
        request.add_unredirected_header("Authorization", f"Bearer {api_token}")
    try:
        with urlopen(request, timeout=TIMEOUT_SECONDS) as resp:
            payload = _read_json(resp)
        runs = payload["workflow_runs"]
        if not isinstance(runs, list):
            raise TypeError
    except urllib.error.HTTPError as error:
        code = error.code if isinstance(error.code, int) else 0
        raise HistoryUnavailable(f"http {code}") from None
    except Exception:
        raise HistoryUnavailable from None
    own = str(run_id or "")
    for run in runs:
        if not isinstance(run, dict) or str(run.get("id")) == own:
            continue
        verdict = _PREVIOUS.get(run.get("conclusion"))
        if verdict:
            return verdict
    raise HistoryUnavailable


def build_message(result, stage, now, run_number):
    when = now.astimezone(KST).strftime("%Y-%m-%d %H:%M KST")
    if result == "failure":
        number = run_number if str(run_number or "").isdigit() else "?"
        body = f"⚠️ 김프 모니터 실행 실패 · {when} · 단계 {_STAGES[stage]} · run #{number}"
    else:
        body = f"✅ 김프 모니터 회복 · {when}"
    return f"{HEADER}\n{body}"


def send(token, chat_id, text, *, urlopen):
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": text,
                                   "disable_web_page_preview": "true"}).encode("utf-8")
    request = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=data,
                                     headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urlopen(request, timeout=TIMEOUT_SECONDS) as resp:
        payload = _read_json(resp)
    return isinstance(payload, dict) and payload.get("ok") is True


def run(environ=None, *, urlopen=None, now=None):
    """로그 한 줄 문자열을 돌려준다. 예외를 밖으로 내보내지 않는다."""
    environ = os.environ if environ is None else environ
    urlopen = urlopen or _default_urlopen
    try:
        result, stage = current_result(environ.get("MONITOR_OUTCOME"), environ.get("JOB_STATUS"))
        if result in ("cancelled", "unknown"):
            return "notify: skipped (cancelled)" if result == "cancelled" else "notify: skipped (unknown outcome)"
        try:
            previous = previous_conclusion(environ.get("GITHUB_RUN_ID"), environ.get("GITHUB_REF_NAME"),
                                           urlopen=urlopen, api_token=environ.get("GH_API_TOKEN") or "")
        except HistoryUnavailable as missing:
            detail = f" ({missing.reason})" if missing.reason else ""
            return f"notify: skipped (history unavailable{detail})"
        if previous == result:
            return "notify: skipped (no transition)"
        token = environ.get("TELEGRAM_BOT_TOKEN") or ""
        chat_id = environ.get("TELEGRAM_CHAT_ID") or ""
        if not token or not chat_id:
            return "notify: skipped (no credentials)"
        text = build_message(result, stage, now or datetime.now(KST), environ.get("GITHUB_RUN_NUMBER"))
        kind = "failure" if result == "failure" else "recovery"
        if send(token, chat_id, text, urlopen=urlopen):
            return f"notify: sent ({kind})"
        return "notify: failed (not ok)"
    except Exception as error:
        # 예외 문자열에 토큰이 든 요청 URL 이 실릴 수 있어 클래스 이름만 남긴다.
        return f"notify: failed ({type(error).__name__})"


def main(environ=None, *, urlopen=None, now=None):
    print(run(environ, urlopen=urlopen, now=now))
    sys.stdout.flush()
    return 0
