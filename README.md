# Offby

[![한국어](https://img.shields.io/badge/README-%ED%95%9C%EA%B5%AD%EC%96%B4-2f6feb?style=for-the-badge)](README.md) [![English](https://img.shields.io/badge/README-English-lightgrey?style=for-the-badge)](docs/README.en.md)

**LLM 배치 잡의 예보 심판.**

돌리기 전에 "얼마나 쓸지"를 적어둔다. Offby는 그 잡을 지켜보다가 숫자가 예보와 어긋나는 순간 — 예산이 다 나간 뒤가 아니라 **열 번째 호출쯤에서** — 잡을 세우고 **어느 가정이 틀렸는지** 이름을 붙여 돌려준다.

> **상태: PoC 동작 (모의 업스트림 기준).** Token Factory 실측 전. [Nebius × NVIDIA Global AI Hackathon](https://nebiusglobalaihackathon.devpost.com/) 출품, 제출 마감 2026-10-30. 로드맵 체크박스가 켜지기 전엔 아무것도 배송된 게 아니다.

> *English readers: use the button above or open [docs/README.en.md](docs/README.en.md).*

---

## 30초 요약

택시를 탈 때 "2만 원쯤 나오죠?"라고 말했다고 하자. 미터기가 3km 지점에서 *"지금 페이스면 9만 원입니다. 예상과 다른 건 거리가 아니라 **톨게이트 요금**이에요"*라고 말하고 차를 세운다. 그게 Offby다.

예산 캡은 돈을 센다. 돈은 가장 늦게 움직이는 숫자다.

슬랙에 이렇게 썼다: *"오늘 밤 nemotron-nano로 20만 행 분류, 짧은 프롬프트, 문단 답변, 20달러 안."* 출력은 콜당 ~250토큰이라고 가정한 셈이다. 모델이 답하기 전에 생각(reasoning)을 하니 실제로는 ~2,150 — 가정의 8.6배.

| | 멈추는 콜 | 나간 돈 | 알게 되는 것 |
|---|---|---|---|
| 예산 캡, 가격표에 없는 모델 | 안 멈춤 (200,000) | ~$107 | 없음 — 비용이 $0로 기록됨 |
| 예산 캡, 단가 등록, 캡 = $20 | ~37,000 | $20 | "Current cost: 20.0, Max budget: 20" |
| **Offby** | **10** | **~$0.01** | "`output_tokens` 예보의 8.6배 · completion 토큰의 87%가 reasoning · `enable_thinking: false`면 투영 $18.9" |

수치는 예시. 단가는 Nebius Token Factory에 게시된 Nemotron-3-Nano 기준.

## 어떻게 쓰나 — 입구 셋

**1. 에이전트 스킬 (권장).** 배치 코드를 쓰는 건 이제 대개 에이전트다. [`skills/offby/SKILL.md`](skills/offby/SKILL.md)를 Claude Code 같은 하네스에 넣어두면, 에이전트가 LLM 루프를 돌리기 직전에 스킬이 발동해서 **코드에서 예보를 뽑고 → 잡을 등록하고 → 프록시로 돌리고 → 402가 나면 진단을 읽고 코드를 고쳐 재실행**한다. 사람은 예보를 안 쓴다. 에이전트가 자기 코드에서 N·프롬프트 길이·모델 id를 읽으니 문장으로 추측하는 것보다 정확하다.

**2. CLI 직접.** 이름 붙은 잡을 만들고 URL을 받는다.
```bash
offby job ensure nightly-classify --calls 200000 --input 400 --output 250 --model nvidia/nemotron-3-nano-30b-a3b --budget 20
OPENAI_BASE_URL=http://localhost:8402/j/nightly-classify/v1 python classify.py
```
문장으로 쓰고 싶으면 `offby forecast "오늘 밤 20만 행 분류, 짧은 프롬프트, 문단 답변, 20달러 안"` — nano가 5항으로 옮기고, 문장에 없던 항은 `ASSUMED`로 표시해 집행 전에 확인받는다.

**3. cron / 컨테이너.** 잡 옆에 프록시를 사이드카로 두고, 실행 줄에 URL 한 줄. 같은 이름으로 다시 오면 새 run이다.

세 입구 모두 잡 쪽 변경은 **환경변수 한 줄**뿐이다. SDK도, 코드 수정도, 키 발급도 없다. 잡의 API 키는 그대로 통과한다.

## 동작

```mermaid
flowchart LR
  J[내 잡<br/>base_url → Offby] -->|요청| P[Offby 프록시<br/>/j/&lt;job&gt;/v1]
  P -->|그대로 전달| TF[Nebius Token Factory<br/>nemotron-3-nano / super]
  TF -->|응답 + usage| P --> J
  P --> M[(미터<br/>usage만, 프롬프트 없음)]
  M --> B{항별 대조<br/>10콜부터}
  B -->|이탈| H[잡에 HTTP 402<br/>깨진 항 이름]
  B -->|이탈| D[진단<br/>nemotron-3-super]
```

1. **예보 → 다섯 항.** `호출수 · 입력 tok/콜 · 출력 tok/콜 · 단가 · 창`. 단가는 `GET /v1/models?verbose=true`에서 라이브로 읽고, 모르는 모델은 `UNPRICED`로 맨 위에 뜬다 — 절대 $0이 아니다.
2. **잡을 Offby로.** `OPENAI_BASE_URL` 한 줄. 요청 본문은 손대지 않고 전달한다.
3. **10콜부터 심판.** 응답마다 붙어오는 `usage`를 읽어 항마다 실측 평균 ÷ 예보를 계산한다. **전체 평균과 최근 5콜 평균이 둘 다** 임계(기본 2배)를 넘어야 이탈 — 긴 답 하나로는 잡이 죽지 않는다. 이탈하면 그 다음 요청부터 진짜 에러가 나간다:

```http
HTTP/1.1 402 Payment Required
X-Offby-Halt: output_tokens
X-Offby-Diagnosis: http://localhost:8402/j/nightly-classify/diagnosis

{"error":{"type":"offby_term_breach","term":"output_tokens","ratio":8.6,
 "message":"term output_tokens breached 8.6x (250→2150) — halted at 25/200000; projected $103.20 vs $16.80",
 "resume":"offby accept nightly-classify output_tokens=2200"}}
```

4. **진단은 이탈 때만.** `nemotron-3-super`가 증거 묶음(reasoning 토큰 비중, 재시도·429, 캐시 비중, 언급한 적 없는 모델)을 읽고 **원인 · 최상위 unknown · 수정안**을 돌려준다. 원인을 고쳐 재실행하거나(`offby accept <job>`), 새 숫자를 받아들인다(`offby accept <job> output_tokens=2200`).

**hot path에 모델 호출은 0.** 모델은 잡당 두 번만 — 문장 파싱 한 번(문장을 썼을 때만), 이탈마다 진단 한 번.

## 용어 — job, call, run

- **call** = HTTP 요청 하나. `calls` 테이블 한 줄.
- **job** = 같은 URL(`/j/<이름>/v1`)로 들어온 call의 묶음. 예보 하나를 공유한다. 실체는 SQLite 한 줄과 URL 경로뿐이다 — 프로세스도 큐도 아니다.
- **run** = `offby job ensure <이름>`을 다시 부를 때마다 시작되는 실행 1회. 카운터·판정·상태는 리셋되고 이력은 남는다.
- **잡은 Offby를 모른다.** 잡이 만지는 건 base_url뿐이다. 프록시는 잡의 프로세스를 죽일 권한이 없다 — 402를 돌려주면 SDK가 예외를 던지고 잡이 그 예외로 멈춘다. 멈추지 않고 계속 부르면 계속 402를 받는데, halted 상태에선 업스트림에 가지 않으니 돈은 나가지 않는다.

## 언제 맞고 언제 안 맞나

핵심 가정은 하나다: **콜당 입력·출력 토큰이 대충 일정하다.** 이게 성립해야 "콜당 평균 대비 배수"가 의미 있다.

| 상황 | 맞나 | 이유 |
|---|---|---|
| 리뷰 20만 건 분류, 야간 임베딩, eval 스윕, 문서 OCR 배치, 합성데이터 생성 | **맞음** | 같은 템플릿 × N행 |
| cron으로 매일 도는 에이전트 루틴 | **맞음** | 같은 이름으로 다시 오면 새 run. 이력이 예보가 된다 (로드맵) |
| 티켓 5,000개를 같은 방식으로 처리하는 에이전트 플릿 | **맞음** | 반복 모양 |
| ReAct류 에이전트 루프 1회 | 애매함 | 콜 수 불명, 컨텍스트가 매 턴 커져 `input_tokens`가 자연히 몇 배가 됨. `--baseline`으로 "급변"만 |
| 사람이 보고 있는 인터랙티브 세션, 챗봇 서빙 트래픽 | **안 맞음** | 예보도 모양도 끝도 없다. 사람이 이미 루프 안에 있다. 이건 게이트웨이의 키별 예산 영역 |

Offby는 **아무도 안 보고 있는 실행**을 위한 것이다. 사람이 보고 있는 실행에 끼우면 심판이 아니라 방해다.

## 시나리오 — 잡 하나를 끝까지

**22:10** 데이터팀의 A: *"오늘 밤 리뷰 20만 건을 nemotron-nano로 분류할게요. 프롬프트 짧고, 답은 한 문단, 20달러 안일 거예요."*

**22:11** 그 문장을 그대로 넣는다.

```
$ offby forecast "오늘 밤 리뷰 20만 건을 nemotron-nano로 분류. 프롬프트 짧고, 답은 한 문단, 20달러 안" --budget 20

  항              예보        출처
  호출수          200,000     문장
  입력 tok/콜     400         ASSUMED ← "프롬프트 짧고"
  출력 tok/콜     250         ASSUMED ← "답은 한 문단"
  단가 $/M        0.06 / 0.24 token factory (live)
  창              오늘 밤     문장
  예상 총액       $16.80      (< $20 ✓)

  ASSUMED 2건은 Offby가 추측한 값입니다. 맞으면 Enter, 아니면 고치세요:  ↵
  job j_7f3a 생성. base_url → http://localhost:8402/j/j_7f3a/v1
```

**22:12** 환경변수 한 줄 붙여 실행. `OPENAI_BASE_URL=http://localhost:8402/j/j_7f3a/v1 python classify.py`

**22:12:40** 열 콜이 쌓였다. 출력 평균 2,137 ÷ 예보 250 = **8.5배**, 최근 5콜 2,140 ÷ 250 = **8.6배** — 둘 다 임계 초과 → 이탈. 호출수·입력·단가는 계획대로. 깨진 항은 `output_tokens` 하나. 지금까지 $0.005, 이대로면 **$107.6**.

**22:13** 다음 요청에 402. A의 터미널:

```
openai.APIStatusError: 402 offby: term output_tokens breached 8.6x (250→2150)
  — halted at 25/200000; projected $107.60 vs $16.80.
  resume: offby accept j_7f3a output_tokens=2200   diagnosis: http://localhost:8402/j/j_7f3a
```

**22:13** 진단(`nemotron-3-super`):

> 깨진 항: **출력**. completion 토큰의 87%가 reasoning. `nemotron-3-nano`는 기본이 think-on — 문단 하나를 값 매겼는데 사고 과정 + 문단이 청구됐다. **수정**: `chat_template_kwargs.enable_thinking=false` → 출력 ~290/콜, 투영 **$18.9**.

**22:15** 옵션을 넣고 `offby accept j_7f3a` 후 재실행. 60콜부터 초록. 새벽 3시에 끝났고 청구는 $18.7.

**같은 밤, Offby가 없었다면** — 캡 없음: 아침에 $107 청구서. LiteLLM 키에 `max_budget=20`: 새벽 1시 37,000번째 콜에서 정지, 19%만 처리, $20은 사라짐, 같은 설정으로 다시 돌리면 같은 일. 팀 공용 키에 캡: A의 잡이 팀 전체 예산을 소진해 남의 요청까지 거부.

### 예보 없이 — 베이스라인 모드

`offby job ensure <이름> --baseline 10`. 첫 10콜을 기준으로 삼고 그 뒤 급변(항별 2배)만 잡는다. "네 기대와 다르다"는 못 말하지만 "50콜부터 출력이 3배 뛰었다"는 잡힌다.

## 지금 돌려보기 (PoC)

키 없이, 돈 안 쓰고, 위 시나리오를 재현한다. 모의 업스트림이 Nemotron처럼 **think-on 기본**으로 답한다.

```bash
uv sync                                                              # Python 3.12, .venv
uv run offby mock  --port 8499                                       # 터미널 1: 가짜 Token Factory
uv run offby serve --port 8402 --upstream http://127.0.0.1:8499/v1   # 터미널 2: 프록시
```

터미널 3 — `examples/reviews.jsonl`(120행)을 분류하는 샘플 잡:

```bash
uv run offby job ensure classify-reviews --calls 120 --input 40 --output 250 \
  --model nvidia/nemotron-3-nano-30b-a3b --budget 1 --upstream http://127.0.0.1:8499/v1 -y
#   expected total $0.01 · base_url → http://localhost:8402/j/classify-reviews/v1

OPENAI_BASE_URL=http://localhost:8402/j/classify-reviews/v1 uv run python examples/classify.py --data examples/reviews.jsonl
#   10콜 뒤: 402 offby: term output_tokens breached 8.6x (250→2150) — halted at 12/120; projected $0.0620 vs $0.0075

uv run offby report classify-reviews       # 항별 표 · 증거(reasoning 87%) · 진단 · resume 명령
uv run offby accept classify-reviews       # 원인을 고쳤다 → 다음 콜부터 다시 판정
OPENAI_BASE_URL=http://localhost:8402/j/classify-reviews/v1 uv run python examples/classify.py --data examples/reviews.jsonl --no-think
#   enable_thinking=false → 출력 ≈290 → 120/120 통과

uv run offby lessons                       # 이 모델은 thinking이 출력을 ≈7배로 만든다 — 다음 예보에 쓸 것
uv run offby job ensure classify-reviews -y   # 내일: 같은 이름 = 새 run, 예보 유지
```

에이전트 스킬로 같은 흐름을 돌리려면 `skills/offby/SKILL.md`를 하네스의 스킬 폴더에 두면 된다(Claude Code: `.claude/skills/offby`). 실제 Token Factory로 가려면 `NEBIUS_API_KEY`를 두고 `--upstream`을 뺀다. 기록은 `~/.offby/offby.sqlite`(또는 `$OFFBY_DB`)에 usage만 남는다.

**있는 것** — 프록시(비스트리밍·스트리밍) · usage 미터(SQLite) · 가격 오라클(`/v1/models?verbose=true`를 관용적으로 읽고 모르면 `UNPRICED`) · 이탈 엔진(10콜 · 2배 · 두 평균) · 402 본문/헤더 · 이름 붙은 잡과 run(`job ensure`) · `accept` / `report` / `lessons` / `jobs` · 베이스라인 모드 · nano 예보 파싱 · super 진단 · 모의 업스트림 · 에이전트 스킬 · 테스트 24개.
**아직 없는 것** — Token Factory 실측(정본 모델 id, `reasoning_tokens`가 채워지는지, thinking을 끄는 플래그, verbose 가격 응답의 실제 모양) · 이력 기반 자동 예보 · `alert` 모드(402 없이 알림만) · 진단 스트리밍 · UI · LiteLLM 플러그인.

## 모델은 어디서 무게를 받나

| | 역할 | 왜 이 모델 |
|---|---|---|
| `nvidia/nemotron-3-nano-30b` | 캐주얼한 문장을 `ASSUMED` 배지 달린 다섯 항으로(구조화 출력) | 싸고 빠르고, 자연어가 불가피한 유일한 단계 |
| `nvidia/nemotron-3-super-120b` | 증거 묶음에서 이탈 원인 진단 + 수정 예보 | 콜마다가 아니라 이탈마다 한 번 |
| Nebius Token Factory | 라이브 단가, reasoning·캐시 토큰이 든 `usage`, rate-limit 헤더 | 측정 표면이 곧 스폰서 API |

스폰서가 곧 피험자다: Nemotron은 기본 think-on이고 trace를 출력 토큰으로 청구한다. 데모의 오버런은 연출이 아니라 측정이다.

## 왜 예산 캡만으로 안 되나

예산 캡은 있어야 한다. Offby는 캡을 대체하지 않는다 — **옆에 선다.** 캡이 스프링클러라면 Offby는 연기 감지기다.

| | LiteLLM 등 게이트웨이 예산 | Offby |
|---|---|---|
| 감지 단위 | 누적 달러 | **네 예보** 대비 속도, 항별 |
| 가장 빠른 정지 | 예산이 소진될 때(그것도 모델이 가격표에 있을 때만) | 10콜 |
| 에러가 말하는 것 | 현재 지출 vs 한도 | 어느 항이 몇 배 틀렸고 왜인지 |
| 정체성 | 키 (누가 썼나, 월 누적) | URL 경로 (이 실행이 예보대로 가나) |
| 저장하는 것 | 지출 로그 / 요청 로그 | `usage`만 — 프롬프트·응답 절대 저장 안 함 |

2026-09-06 LiteLLM 문서·소스 확인: 예산 집행은 누적 지출의 선호출 검사, 초과 메시지는 엔티티·현재 비용·한도만 말함, 번들 가격표에 Nemotron 3 항목이 없어 단가를 직접 넣지 않으면 비용이 `None`으로 기록됨. 이미 게이트웨이를 운영하는 팀이 두 번째 프록시 없이 심판을 얹을 수 있게 LiteLLM 플러그인 모드(`CustomLogger` pre-call hook)가 로드맵에 있다.

## 설계 규칙

- **usage만.** 미터는 토큰 수·모델 id·상태·지연·rate-limit 헤더를 저장한다. 프롬프트와 응답은 컬럼 자체가 없다.
- **$0 금지.** 가격 없는 모델은 `UNPRICED`로 첫 줄에 뜬다. 조용히 0이 되지 않는다.
- **가정은 보이게, 확인받고 집행.** 미기재 항마다 `ASSUMED`와 근거 문구가 붙는다.
- **10콜 전엔 판정 없음, 두 평균이 일치해야 한다.** 이력이 쌓인 뒤라면 롱테일 출력 하나로는 정지하지 않는다. (경계 조건: 정확히 10콜째에 수십 배짜리 답 하나가 들어오면 둘 다 넘길 수 있다.)
- **재개하면 판정 창을 리셋한다.** `accept` 뒤에는 그 이후 콜만 본다. 안 그러면 고친 뒤에도 오염된 평균 때문에 첫 콜에서 다시 멈춘다. 총액·투영은 run 전체로 센다.
- **402는 이탈을 만든 콜의 *다음* 요청부터.** 이미 돈이 나간 응답은 그대로 돌려준다. 동시성이 8이면 "10콜 판정, 16콜 정지"가 나온다.
- **Offby의 402는 업스트림의 402가 아니다.** Token Factory는 *네* 잔액이 소진되면 402를 낸다. Offby의 402는 `type: offby_term_breach`와 `X-Offby-Halt`를 달아 절대 섞이지 않는다.
- **원하면 fail-open.** `--fail-open`이면 미터가 죽어도 트래픽을 통과시킨다. 심판이 건강한 잡을 죽이는 물건이어선 안 된다.
- **reasoning 토큰은 관측 전엔 미지수.** `completion_tokens_details`가 null이면 `reasoning_content`나 `<think>` 태그로 추정하고 추정이라 표시한다.

## CLI

```bash
offby serve    --upstream https://api.tokenfactory.nebius.com/v1 [--fail-open]   # 프록시
offby mock     --port 8499                                                        # 가짜 업스트림 (PoC)
offby job ensure <name> --calls N --input I --output O --model <id> --budget B     # 이름 붙은 잡 · 다시 부르면 새 run
offby job ensure <name> --baseline 10                                             # 예보 없이
offby forecast "200k-row classification tonight on nemotron-nano, …" --budget 20  # 1회용 잡, 문장 파싱
offby report   <name>                                                             # 항별 예보 vs 관측, 비용, 진단
offby accept   <name> [output_tokens=2200]                                        # 항 수용 또는 원인 고친 뒤 재개
offby lessons                                                                     # 잡 횡단 교훈 — 다음 예보 전에 읽을 것
offby jobs
```

## 로드맵

- [x] **1주** — 프록시 + 미터 + 가격 오라클 E2E (모의 업스트림)
- [ ] **1주** — Token Factory day-1 측정(정본 모델 id, `reasoning_tokens` 채워지는지, 어떤 플래그가 thinking을 끄는지, nano에서 `json_schema`가 버티는지)
- [x] **2주** — 이탈 엔진, 402 본문, `accept`, CLI, report
- [x] **2주+** — 이름 붙은 잡과 run, `lessons`, 에이전트 스킬
- [ ] **3주** — 이력 기반 자동 예보(같은 이름의 직전 정상 run이 예보), `alert` 모드, 예보 파서 폴백 실측, 진단 스트리밍
- [ ] **4주** — 단일 페이지 UI: 예보 카드, 게이지, 터미널 로그, 진단 패널, run 드리프트
- [ ] **5주** — 호스팅 데모: 서로 다른 항이 깨지는 서버측 잡 3종, IP당 쿼터, 일일 지출 상한, 리플레이 폴백
- [ ] **6주** — LiteLLM 플러그인 모드, 클린 클론에서 README 검증, 3분 영상, 툴링 피드백
- [ ] **10/28 제출**

## 의도적으로 안 만드는 것

응답 중간 스트림 절단(판정은 요청 경계에서) · 멀티프로세스/Redis 카운터 · 사용자 계정·키 발급 · Batch API·캐시 할인 회계(증거로 기록만) · 프롬프트 저장·재실행 큐 · 프론트엔드 빌드 단계 · OpenAI 호환 외 업스트림 · 자동 `accept`.

## 해커톤

트랙: **Best Apps and Agents**. 이 레포가 충족할 요건: Nebius Token Factory 런타임 호출 · NVIDIA Nemotron 3 모델이 load-bearing(파싱 + 진단) · Apache-2.0 공개 레포 · 호스팅 데모 URL · 3분 미만 영상 · 툴링 피드백.

## 라이선스

Apache-2.0. [LICENSE](LICENSE) 참조.
