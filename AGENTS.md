> 이 문서는 Codex 등 외부 에이전트용 안내문이다. 2026-09-18 작성.

> 2026-09-22 후속: 아래 읽기 전용 역할·파일 현황·무옵션 실행 설명은 초기 진단 당시 기록이다. 이후 사용자의 수정 및 검증 후 push 승인으로 개선을 반영했다. 현재 실행 모드, 상태 보존, 로컬 실행 잠금, 게시 재시도와 오프라인 검사는 README.md를 따른다. 실제 운영 상태·자격증명·금융 정책은 별도 승인 범위를 지킨다.

> 2026-09-22 Q9: 사용자 ‘잔여 결함 수정까지 일괄 진행 — 거래·전략 정책 변경 제외’ 및 중간 commit/push 승인으로 게시/JSON/시각 증거/고정 환경을 보완했다. 현재 계약은 아래 최신 절과 README가 우선한다. 운영 state 본문·자격은 검증 입력에 넣지 않는다.

## Q9 유지할 계약

- 게시 전 branch/upstream/봇 전용 선형 이력과 새 commit 부모를 검증한다. 사용자 index·커밋을 reset/rebase/force로 없애지 않는다. 고정 SHA/refspec의 일반 push만 최대2회다. JSON 중복키·비유한 값·후보 bytes 불일치를 거절하며 보관 가격을 반올림해0으로 바꾸지 않는다.
- Actions는 공개 hash/wheel 설치 및 `--live` 조회/발송/저장 후 별도 `--publish-only`를 실행한다. publish-only는 기존 상태를 잠금 안에서 엄격 검증하고 stdlib만 import한다. token은 이 단계에만 전달하고 검증한 저장소 push URL용 Git 자식 환경에만 넣는다. 수집 실패 뒤의 저장된 진단 상태 게시가 원래 실패 종료를 덮어쓰지 않는다.
- `quote_evidence.py`는 가격과 같은 응답의 확인된 계약만 시간 증거로 사용한다. 미확인/미래/naive metadata는 가격 판단 정책을 새로 바꾸지 않는다. `run_timing.py`는 aware wall clock·monotonic을 분리하고 수동/재실행의 예약 baseline을 보존한다. 완료 시각은 저장/게시 전 수집·발송 구간이며 예정 슬롯 없이는 지연값을 만들지 않는다.
- stdlib 오프라인 CI와 실제 고정 패키지 native CI를 분리한다. 실제 state/.env/provider/Telegram/Git 원격은 시험 입력이 아니다. 새 price freshness·장외/휴일·금 level/해제밴드·cron 정책 및 실제 운영 dispatch는 이 코드 검증에 포함하지 않는다.

# AGENTS.md — Kimchi-Premium-Monitor

## 1. 요약
테더(USDT) 김치 프리미엄과 금 김치 프리미엄(KRX 금현물 vs 국제 금 시세 원화 환산)을 계산해, 임계값을 벗어나면 텔레그램으로 알리는 단일 파일 파이썬 봇이다. GitHub Actions 가 15분마다 실행하고, 직전 알림 상태와 최근 10건 이력을 `state.json` 에 적어 **매 실행마다 저장소에 커밋·push** 한다. 이 저장소는 **PUBLIC** 이다. 리뷰는 읽기 전용으로 진행한다(수정·커밋·push 금지).

## 2. 파일 지도
| 경로 | 역할 |
|---|---|
| `monitor.py` (830줄) | 전부. 상태 관리 → 데이터 수집 → 김프 계산 → 알림 판단 → 텔레그램 → 상태 커밋 |
| `.github/workflows/kimp-monitor.yml` | 유일한 워크플로. cron `*/15 * * * *` + `workflow_dispatch` |
| `state.json` | 봇이 쓰는 상태 파일. 키: `history`(최대 10건), `last_alert`(`usdt_low`·`usdt_high`·`gold_low`·`gold_high`) |
| `requirements.txt` | `requests`, `yfinance` 를 `==` 로 고정(해시 고정은 아님, 전이 의존성 미고정) |
| `README.md` | 2줄짜리. 문서 역할 없음 |
| `.gitignore`, `LICENSE`(MIT) | — |

테스트 코드·린트 설정·`.env.example` 은 **없다**.

## 3. 실행 구조
- **진입점**: `python monitor.py` → `main()`. 순서: `load_state()` → `get_usd_krw_rate()` → 테더 블록 → 금 블록 → `add_history()` → (수동 실행이면 현황 리포트) → `send_telegram()` → `save_state()`.
- **트리거**: GitHub Actions 예약(15분) 또는 수동. `RUN_MODE=${{ github.event_name }}` 이 `workflow_dispatch` 이면 알림 조건과 무관하게 현황 리포트를 1건 보낸다.
- **환경변수**(이름만): 시크릿 `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` / 저장소 변수 `USDT_KIMP_LOW`, `USDT_KIMP_HIGH`, `GOLD_KIMP_LOW`, `GOLD_KIMP_HIGH`. 미설정 시 코드 기본값 LOW=0, HIGH=10. 실제 운영 값은 **미확인**(저장소 설정에 있음).
- **데이터 소스와 폴백**: 환율 = 네이버 → Yahoo(`KRW=X`) → er-api / 국내 금 = 네이버 API → 네이버 데스크톱 페이지 정규식 / 국제 금 = Swissquote → Yahoo(`GC=F`) / USDT = Upbit(폴백 없음).
- **상태 저장**: `save_state()` 가 `state.json` 을 쓰고 `subprocess.run(["git", ...])` 로 `add` → `diff --cached --quiet` → `commit -m "update state [skip ci]"`(작성자 `kimp-bot`) → `push`.
- **알림 규칙**: 테더는 `should_alert()` — 같은 방향에서 직전 알림값보다 **악화될 때만** 재알림. 금은 `should_alert_gold_step()` — 임계값에서 1%p(`GOLD_KIMP_STEP`) 단위 레벨이 **올라갈 때만** 알림. 정상 범위로 돌아오면 `last_alert` 의 해당 키를 지운다.

## 4. 로컬 실행·테스트
```bash
pip install -r requirements.txt
python monitor.py
```
- 텔레그램 변수가 없으면 전송만 건너뛰고 나머지는 돈다.
- ⚠️ **로컬 실행도 `save_state()` 에서 실제로 `git commit` + `git push` 를 시도한다**(드라이런 스위치 없음). 리뷰 중에는 실행하지 말고 코드만 읽을 것.
- 자동 테스트는 없다. `pytest` 등 실행할 대상이 없다.

## 5. 절대 규칙·함정
- 공개 저장소다. 토큰·chat id·개인 정보를 코드·로그·이슈·리뷰 산출물 어디에도 적지 말 것.
- 로컬 `main` 은 봇의 상태 커밋만큼 origin 보다 뒤처져 있을 수 있다. **pull/fetch/rebase 하지 말 것** — 차이는 `state.json` 뿐이고 코드는 동일하다.
- `state.json` 을 손으로 고치지 말 것. 봇의 커밋과 충돌한다.
- 커밋 메시지의 `[skip ci]` 는 의미가 있다(아래 6절).
- 의존성 버전을 올리는 제안은 "CI 러너에 시크릿이 주입된다"는 전제(`requirements.txt` 주석)를 고려할 것.

## 6. 의도된 설계 — 결함으로 오인하지 말 것
- **상태를 저장소에 커밋**하는 구조(DB·아티팩트 대신). 하루 최대 96커밋이 쌓이는 것은 알고 택한 비용이다.
- `permissions: contents: write` 는 위 상태 push 에 필요하다. (범위가 최소인지는 7절에서 검토 대상.)
- 테더 알림이 "개선 방향"에서 침묵하는 것, 금 알림이 같은 레벨 안에서 침묵하는 것은 알림 피로를 줄이려는 의도다.
- 환율의 네이버 거래일이 오늘이 아니어도(주말·공휴일) 그대로 쓴다 — 코드가 명시적으로 허용하고 로그만 남긴다.
- `send_telegram()` 의 예외 출력이 `type(e).__name__` 뿐인 것은 예외 문자열에 토큰이 든 URL 이 실릴 수 있어서다. 더 자세히 찍자는 제안은 하지 말 것.
- `save_state()` 가 `subprocess` 리스트 인자를 쓰는 것은 과거 `os.system` 문자열 조합을 대체한 결과다.
- 환율 조회 실패는 치명(텔레그램 통지 후 `sys.exit(1)`), 테더·금 개별 실패는 비치명(로그만 남기고 계속)으로 나눠 둔 것은 의도다. 다만 그 결과는 7절에서 본다.

## 7. 리뷰 시 집중할 위험 지점
1. **워크플로 권한·공급망** — `.github/workflows/kimp-monitor.yml`: 액션이 SHA 가 아니라 태그(`@v4`, `@v5`)로 고정, `contents: write` 토큰이 체크아웃 자격증명으로 남은 상태에서 서드파티 패키지가 실행된다. `concurrency` 그룹 없음.
2. **상태 커밋 경쟁·push 실패** — `monitor.py` `save_state()`: push 실패 시 재시도·rebase 없이 로그 한 줄로 끝나고 종료코드는 0. `add`·`commit` 반환코드는 확인하지 않는다. 실패하면 `last_alert` 가 유실돼 다음 실행에서 같은 알림이 중복될 수 있다. 예약 실행과 수동 실행이 겹치는 경우를 볼 것.
3. **NaN·비정상값 전파** — `get_usd_krw_rate()` 의 Yahoo/er-api 폴백과 `get_upbit_usdt_price()` 에는 범위 검증이 없다(금은 `GOLD_PRICE_MIN_USD`~`MAX` 검증 있음). 환율이 NaN 이면 `main()` 테더 블록의 두 비교가 모두 False 가 되어 **else 분기가 `last_alert` 를 지우는지**, `add_history()` 가 NaN 을 JSON 에 쓰는지 확인. `import math` 는 미사용.
4. **시점 불일치로 인한 오경보** — 24시간 도는 Upbit·국제 금 시세를, 장 마감 후 멈춘 네이버 환율·KRX 금 종가와 비교한다. `main()` 에 장 시간 게이트가 없고 `get_krx_gold_price_per_gram()` 은 거래일을 확인하지 않는다.
5. **히스테리시스 부재** — `main()` 의 "정상 복귀 → 상태 초기화": 임계값 근처에서 오르내리면 매번 "첫 알림"이 된다. `should_alert()` 는 악화 폭 하한이 없어 0.01%p 악화에도 15분마다 재알림한다.
6. **금 레벨 상태가 개선 시 내려가지 않음** — `should_alert_gold_step()`: 레벨 3 → 0 으로 개선 후 다시 2 로 악화해도 저장된 `step_level` 이 3 이라 침묵한다. 의도인지 누락인지 판단 필요(`main()` 의 `else: pass` 분기 주석 참고).
7. **조용한 실패** — 테더·금 블록의 `except Exception` 은 출력만 한다. 소스가 며칠간 죽어도 워크플로는 초록색이고 알림도 없다. 반대로 환율 실패는 중복 억제 없이 15분마다 텔레그램을 보낸다.
8. **기동 시 파싱** — 모듈 상단 `float(os.environ.get(...) or "0")`: 저장소 변수에 숫자가 아닌 값이 들어가면 텔레그램 통지 전에 죽는다.
9. **HTML parse_mode** — `send_telegram()` 은 `parse_mode=HTML` 인데 메시지에 넣는 문자열을 이스케이프하지 않는다(`main()` 의 `f"❌ ... {e}"` 포함). 현재 문구에는 `<`·`&` 가 없어 동작하지만, 섞이는 순간 전송이 거부되고 폴백이 없다.
10. **잔가지** — `'driver_analysis' in dir()` 로 지역 변수 존재를 판정, `analyze_gold_kimp_driver()` 가 인자 `now` 대신 `datetime.now(KST)` 를 다시 호출, 네이버 데스크톱 페이지 정규식 폴백의 취약성, `KST` 고정 오프셋(한국은 서머타임 없음 — 문제 아님).
