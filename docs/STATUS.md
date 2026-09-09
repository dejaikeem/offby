# Offby — 지금 어디까지 왔나 (2026-09-09 기준)

처음 보는 사람을 위한 문서. README가 "이게 뭐고 왜 필요한가"라면, 이 문서는 "지금 실제로 뭐가 있고, 뭐가 검증됐고, 뭐가 안 됐나"다. 전부 이 저장소(`dejaikeem/offby`, main 브랜치)에 들어 있다.

## 1. 한 문단으로

Offby는 **LLM을 수천 번 부르는 배치 작업 옆에 서 있는 미터기**다. 작업을 돌리기 전에 "콜 몇 번, 콜당 입력·출력 토큰 얼마쯤, 모델은 뭐"라고 적어두면(예보), 작업의 API 요청이 Offby라는 작은 프록시를 거쳐 나가면서 응답마다 붙어오는 토큰 수(`usage`)를 기록하고, 실측이 예보와 항목별로 크게 어긋나면 — 예산이 다 나간 뒤가 아니라 **초반 10여 콜에서** — 작업에 HTTP 402 에러를 돌려줘 멈추고 "어느 항목이 몇 배 틀렸는지"를 말해준다. 택시 미터기가 "지금 페이스면 9만 원인데, 예상과 다른 건 거리가 아니라 톨게이트 요금"이라고 말하고 차를 세우는 것과 같다.

작업 쪽에서 바꿀 건 환경변수 한 줄(`OPENAI_BASE_URL`)뿐이고, Offby는 프롬프트나 답변 내용은 저장하지 않는다 — 숫자만.

## 2. 지금 저장소에 있는 것

```
README.md, docs/README.en.md   설계와 사용법 (한/영). "계약서" 역할 — 코드는 이 문서를 따라간다
docs/STATUS.md                 이 문서
src/offby/
  proxy.py     프록시 본체. 요청을 그대로 전달하고, 응답의 usage를 읽고, 심판을 부르고, 402를 낸다
  judge.py     심판. 순수 산술(모델 호출 없음). "예보 대비 몇 배인가"를 확신 있게 판단
  usage.py     응답(JSON/스트리밍)에서 토큰 수를 읽는 파서. reasoning 토큰이 없으면 추정하고 표시
  store.py     SQLite. jobs 테이블(예보·상태) + calls 테이블(콜마다 토큰·가격·상태)
  tf.py        모델을 부르는 곳 — 단, hot path 밖에서만: 가격표 조회, 문장→5항 파싱, 이탈 후 진단
  mock.py      가짜 업스트림. 진짜 모델 없이 Offby를 돌려보는 용도 (아래 3절)
  cli.py       offby 명령어: job ensure / forecast / serve / report / accept / lessons / jobs / mock
skills/offby/SKILL.md          Claude Code 같은 에이전트가 배치를 돌리기 전에 Offby를 쓰게 하는 스킬
examples/classify.py           샘플 배치 (리뷰 120건 감정 분류). reviews.jsonl은 합성 데이터
tests/                         46개. `uv run pytest`
```

저장소에 **없는** 것: `notes/`(설계 회의록·감사 결과·실험 로그 — `.gitignore`로 로컬 전용), 내 노트북의 Ollama 모델 파일.

## 3. 어떻게 검증했나 — 3층, 각각 증명하는 게 다르다

| 층 | 뭘로 | 증명하는 것 | 증명 못 하는 것 |
|---|---|---|---|
| **유닛테스트 46개** | `judge()`에 가짜 숫자 리스트, 프록시+mock을 한 프로세스 안에서 연결 | 배관이 맞게 이어졌나, 심판 산수가 맞나 | 실제 모델이 어떻게 행동하나 |
| **mock 업스트림** | `offby mock` — 모델 없이 "그럴듯한 usage 숫자"를 지어내는 가짜 서버 | 예보→실행→402→report→accept→재실행 흐름 전체. 에러·스트리밍·가격 모양이 이상할 때의 동작 | 실제 출력 길이 분포, thinking이 진짜 켜져 있나 |
| **진짜 모델 (로컬)** | Ollama + `nemotron-3-nano:4b`, 내 노트북, 비용 $0 | 실제 분포에서 오탐이 없나, 진짜 402가 나오나, usage 모양이 우리 파서와 맞나 | Token Factory의 30B가 같은지 (실측 아직) |

**주의해서 읽을 것:** 9/8까지의 "검증 완료"는 1·2층뿐이었고, 9/9 감사에서 "mock이 너무 얌전해서(출력 편차 ±5%, 지연 0, 에러 0) 실제 분포에선 건강한 작업의 36~49%를 잘못 멈추는 심판 규칙이 통과됐다"는 게 드러났다. 그래서 규칙을 바꾸고(아래 4절) mock을 현실처럼 만들고(꼬리 긴 분포·지연·에러·`--profile hostile`) 3층을 추가했다.

## 4. 9/9에 바뀐 것 — 왜

| 바뀐 것 | 이유 |
|---|---|
| 심판 규칙: "평균이 2배 넘으면" → **"평균이 2배를 t > 2.5의 확신으로 넘으면"** (판정창 전체 또는 최근 30콜) | 실제 출력 길이는 꼬리가 길어서(같은 프롬프트가 63토큰~848토큰) 평균만 보면 긴 답 하나에 건강한 작업을 죽인다. 시뮬: 오탐 0%, 8배 폭주는 10콜, 1,000콜 건강 뒤 8배 급변은 11콜(구 규칙 286콜) |
| **에러 응답과 usage 없는 응답은 콜로 안 센다** | 401이 121번 오고 200이 1번 오면 그 1건으로 halt하던 버그. 이제 `failed calls: 121 (401)`로 따로 표시 |
| **usage가 5번 연속 없으면 `usage` 항으로 멈춤** | `include_usage`를 무시하는 서버면 120콜이 $0으로 조용히 통과하던 구멍 |
| 가격 `"0"`이나 없음 → **UNPRICED** (절대 $0 아님); 서버에 가격표가 없으면 **예보 단가로 계산하고 표시** | 4B 로컬 첫 run에서 `--price`를 줬는데도 지출 $0으로 나온 버그 |
| accept 후 파일 전체 재실행 시 `calls` 두 배로 세던 것 → 재개 이후만 셈 | 121행짜리 파일이면 건강한 재실행이 멈췄을 것 |
| Offby 자체 모델 호출(파싱·진단)에 thinking 끄기 플래그 | thinking 켜진 모델이 reasoning만 하다 답을 못 내고 빈 문자열 반환 |
| mock 기본값이 현실적으로 | 위 3절 |

## 5. 진짜 모델에서 오늘 본 것 (Ollama `nemotron-3-nano:4b`, 리뷰 120건, $0)

- **Nemotron은 기본이 thinking ON.** 공식 chat template에 `enable_thinking=True`. 실측도 동일.
- 끄는 방법이 **서버마다 다르다**: Ollama는 `chat_template_kwargs`를 무시하고 `reasoning_effort: "none"`을 읽는다. vLLM/Token Factory는 반대일 가능성. 그래서 샘플 배치의 `--no-think`는 둘 다 보낸다.
- Ollama는 usage에 `reasoning_tokens`를 안 준다 → Offby가 `reasoning` 필드 글자수로 추정하고 `(estimated)` 표시. 이 경로가 실제로 쓰였다.
- **thinking on/off 배수: 평균 3.2×** (on: 중앙값 144·평균 217·p95 559·max 848, reasoning 비중 85% / off: 67). README의 "8.6배"는 30B·긴 답변 가정의 **예시**이지 실측이 아니다.
- **오탐 없음**: 예보 250에 max 848(3.4배)이 섞여 들어와도 t = −14.8, 멈추지 않음. 새 규칙의 첫 실물 통과.
- **진짜 첫 402**: 출력을 60으로 잘못 예보한 run → `4.7x (60→285, t=3.1) — halted at 42/120`, 그때까지 $0.003. 꼬리가 긴 분포에서 확신을 얻느라 10콜이 아니라 42콜. 더 빠른 규칙(CUSUM)은 로드맵.
- **4B는 진단·문장 파싱엔 부족**: 진단은 reasoning 88%를 못 보고 지연 탓, 파싱은 "120 reviews"를 1건으로.
- **30B (`nemotron-3-nano:30b`, 24GB, 같은 날 저녁):** 속도는 4B와 같음(~50 tok/s, MoE). think-on 평균 404·p95 2,092·max 4,000(캡) — 꼬리가 훨씬 김. 예보 250 대비 1.6배라 안 멈춤(옳음). **배수 4.3×.** 오예보 60은 **11콜에서 402**. **문장 파싱 5항 전부 정확**, 진단도 evidence에 산술 가설을 넣어주자 정확("reasoning 96% → `reasoning_effort='none'`"). 즉 파싱·진단 자리는 30B급이면 된다.

## 6. 아직 안 된 것 — 정직하게

- **Token Factory 실측 0건.** 키가 없다. 정본 모델 id, `reasoning_tokens`를 주는지, thinking 끄는 플래그, 가격표 모양 — 전부 미확인. 이게 해커톤 제출의 전제.
- 멈춘 걸 사람에게 알리는 수단 없음(로그 한 줄뿐) — `alert` 모드와 웹훅 필요.
- **halt가 sticky하지 않다**: `offby job ensure`를 다시 부르면 halt가 풀린다. 끝내고 싶은 에이전트가 우회 가능. 예산(`--budget`)도 표시만 되고 강제되지 않는다.
- 심판이 콜마다 run 전체를 다시 계산(O(n)) — 1만 콜 이후 느려진다. 프록시 2개가 같은 DB를 보면 각자 따로 센다.
- 진단·문장 파싱은 로컬 30B로만 확인했다(4B 실패, 30B 성공). Token Factory의 Super에서는 아직.
- 이력 기반 자동 예보(같은 이름의 지난 run이 예보), UI, LiteLLM 플러그인 — 로드맵.

## 7. 직접 돌려보기 (5분, 돈 안 듦)

```bash
git clone https://github.com/dejaikeem/offby && cd offby && uv sync
uv run offby mock  --port 8499                                        # 터미널 1: 가짜 모델 서버
uv run offby serve --port 8402 --upstream http://127.0.0.1:8499/v1    # 터미널 2: Offby 프록시
# 터미널 3
uv run offby job ensure demo --calls 120 --input 40 --output 250 --model mock/nemotron-3-nano-30b-a3b --budget 1 --upstream http://127.0.0.1:8499/v1 -y
OPENAI_BASE_URL=http://localhost:8402/j/demo/v1 uv run python examples/classify.py --data examples/reviews.jsonl
#   → 유효 10콜쯤에서 402 (출력이 예보의 ~8배: 가짜 모델이 "thinking 켜진 Nemotron"을 흉내내므로)
uv run offby report demo                    # 항별 표, 증거, 진단, resume 명령
uv run offby accept demo                    # "원인 고쳤다" → 다음 콜부터 다시 판정
OPENAI_BASE_URL=http://localhost:8402/j/demo/v1 uv run python examples/classify.py --data examples/reviews.jsonl --no-think
#   → 120/120 통과
uv run offby lessons                        # 모델별로 "thinking이 출력을 몇 배로 만드나"
```

진짜 모델로 하려면 `brew install ollama && ollama pull nemotron-3-nano:4b`, `--upstream http://127.0.0.1:11434/v1`, 그리고 `job ensure`에 `--price 0.06,0.24`(Ollama엔 가격표가 없음).

## 8. 다음 단계와 결정 필요한 것

1. ~~`nemotron-3-nano:30b` 로컬 3-run~~ 완료 (위 5절)
2. 야간 1만 건 배치 + 중간 급변 주입 → 장기 run 감지 속도, 심판 성능 측정
3. Nebius 키 → Token Factory 실측 (해커톤 전제)
4. 밋업(9/11) 후: alert 모드·웹훅 → sticky halt·예산 강제 → O(1) 심판 → LiteLLM 플러그인 스파이크

**결정 필요:** 심판이 "오탐 0" 대신 "더 빨리"를 택해야 하나(4.7배를 42콜에 잡는 게 느린가), halt 기본값을 alert로 바꿀 것인가, 해커톤 트랙(Best Apps & Agents 유지), 레포 공개 시점(지금 private).

**서사 결정 (2026-09-10):** 토큰을 파는 스폰서 앞에서 "청구서를 깎아준다"는 정반대로 읽힌다. Offby는 지출을 줄이는 도구가 아니라 **배치가 계획대로 끝나게 하는 도구**이고, Nemotron의 thinking 손잡이를 잊었을 때 알려주는 도구다. 데모는 halt가 아니라 alert로, 그리고 Token Factory에 없는 키별 캡 기능 요청을 같이 낸다. 시장 조사 전문은 `notes/2026-09-09-market-survey.md`(로컬).
