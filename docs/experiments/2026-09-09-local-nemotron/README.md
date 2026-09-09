# 로컬 Nemotron 3 Nano 실측 — 2026-09-09

Offby를 **진짜 모델**에 처음 붙여본 기록. mock(가짜 서버)이 아니라 Ollama로 띄운 `nemotron-3-nano:4b`(Q4, 2.8GB)와 `nemotron-3-nano:30b`(Q4, 24GB)를 M4 Pro 48GB 노트북에서 돌렸다. API 비용 $0. 프롬프트·답변은 저장하지 않으므로 여기 파일엔 **usage 숫자만** 있다.

## 파일

| 파일 | 내용 |
|---|---|
| `calls-nano4b.csv` (283행) / `calls-nano30b.csv` (252행) | 콜마다 한 줄: 토큰 수, reasoning 추정, finish_reason, 지연, 단가, 비용. `run` 열로 세 실험을 구분 |
| `jobs.json` | 두 잡의 예보(terms)·상태·마지막 halt의 evidence·진단 텍스트 |
| `run-4b-run1-run2.log`, `run-4b-run3-forecast60.log`, `run-30b-run1-2-3.log` | 실행 당시 터미널 출력 그대로 (402 메시지, `offby report`, 분포 통계) |
| `proxy-halt-events.jsonl` | 프록시가 남긴 halt 이벤트 |

## 실험 설계

리뷰 120건(`examples/reviews.jsonl`, 합성)을 `examples/classify.py`로 분류. 시스템 프롬프트 "Classify the review sentiment and justify in one paragraph." `max_tokens=4000`, 동시성 2.

| run | 예보 (출력 tok/콜) | thinking | 보려는 것 |
|---|---|---|---|
| 1 | 250 | 기본 (ON) | 실제 출력 분포. 예보가 대충 맞을 때 **오탐이 없나** |
| 2 | 250 (accept 후 재개) | OFF (`reasoning_effort: none`) | think-on/off 배수 |
| 3 | **60** (새 run) | ON | 잘못 예보했을 때 **진짜 402가 몇 콜에 나오나**, 진단은 되나 |

Ollama는 `chat_template_kwargs`를 무시하고 `reasoning_effort`만 읽으므로 `--no-think`는 두 플래그를 모두 보낸다. Ollama의 `/v1/models`엔 가격이 없어 `--price 0.06,0.24`(Token Factory nano 단가)를 예보에 명시했고, 비용 열은 그 단가 기준(`price_source=forecast`).

## 결과

| | 4B think-on | 4B off | 30B think-on | 30B off |
|---|---|---|---|---|
| completion 중앙값 / 평균 | 144 / 217 | 67 / 67 | 205 / **404** | 66 / 89 |
| p95 / max | 559 / 848 | 101 / 127 | **2,092 / 4,000**(캡) | 326 / 695 |
| lognormal σ | 0.61 | — | **0.91** | 1.35 |
| reasoning 비중 (추정) | 85% | 0 | ~96% | 0 |
| think-on/off 배수 (평균) | **3.2×** | | **4.3×** | |
| run 1 판정 | 0.87×, t=−14.8, 안 멈춤 ✓ | | 1.62×, t=−0.4, 안 멈춤 ✓ | |
| run 3 (예보 60) | **4.7× → 42콜에서 402**, $0.003 | | **3.1× → 11콜에서 402**, $0.0006 | |
| 지연 중앙값 / p95 | 6.1s / 19.1s | 2.7s / 3.6s | 9.6s / 58.5s | 2.7s / 8.4s |

- **Nemotron은 기본 thinking ON** (공식 chat template `enable_thinking=True`; 실측 일치).
- Ollama는 usage에 `reasoning_tokens`를 주지 않는다 → Offby가 `message.reasoning` 글자수/4로 추정하고 `(estimated)` 표시. 추정치가 completion을 넘는 경우가 있어 completion으로 캡(이 실험 후 반영).
- **꼬리가 길다.** 같은 프롬프트에 30B는 56~4,000토큰. 예보를 대충 맞게 잡았을 때(run 1) 두 모델 모두 **오탐 없이** 완주 — 평균만 보던 구 규칙이었으면 4,000짜리 콜 하나에 멈췄을 가능성이 높다.
- run 3의 402 시점이 **42콜(4B) vs 11콜(30B)**로 다른 건 모델 차이가 아니라 초반 표본의 흔들림(σ 0.52 vs 더 큼). "t > 2.5 확신"의 대가.
- 4B는 문장→5항 파싱("120 reviews"→calls=1)과 진단(reasoning 88%를 못 짚음)에 실패. **30B는 파싱 5항 전부 정확**, 진단은 evidence에 산술 가설을 넣어준 뒤 정확("reasoning 96%가 출력으로 청구 → `reasoning_effort='none'`"). 이 결과로 `tf.hypotheses`가 추가됐다.
- run 1이 잡아낸 Offby 결함: `--price`가 계측에 안 쓰임(→ 예보 단가 폴백), accept 후 재실행 시 calls 두 배(→ 재개 이후만 셈), Offby 자체 호출이 thinking을 안 꺼 빈 답(→ 두 방언 플래그). 전부 같은 날 수정.

## 재현

```bash
brew install ollama && ollama serve &
ollama pull nemotron-3-nano:4b            # 또는 :30b (24GB)
uv run offby serve --port 8402 --upstream http://127.0.0.1:11434/v1
uv run offby job ensure local-nano4b --calls 120 --input 40 --output 250 --model nemotron-3-nano:4b --price 0.06,0.24 --budget 1 --upstream http://127.0.0.1:11434/v1
OPENAI_BASE_URL=http://localhost:8402/j/local-nano4b/v1 uv run python examples/classify.py --data examples/reviews.jsonl --model nemotron-3-nano:4b --concurrency 2
uv run offby report local-nano4b
uv run offby accept local-nano4b
OPENAI_BASE_URL=http://localhost:8402/j/local-nano4b/v1 uv run python examples/classify.py --data examples/reviews.jsonl --model nemotron-3-nano:4b --concurrency 2 --no-think
uv run offby job ensure local-nano4b --output 60 --upstream http://127.0.0.1:11434/v1   # run 3
OPENAI_BASE_URL=http://localhost:8402/j/local-nano4b/v1 uv run python examples/classify.py --data examples/reviews.jsonl --model nemotron-3-nano:4b --concurrency 2
```

## 이 실험이 말하지 않는 것

Token Factory의 30B가 같은 usage 모양·같은 플래그·같은 분포를 갖는지는 여기서 알 수 없다(양자화 Q4, Ollama 서빙). 20만 건 완주는 로컬에선 며칠 걸려 시도하지 않았다. 진단 품질은 30B 한 번의 결과일 뿐 통계가 아니다.
