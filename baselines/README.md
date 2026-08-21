<!--
SPDX-FileCopyrightText: Copyright 2026 SK TELECOM CO., LTD.
SPDX-License-Identifier: Apache-2.0
-->

# Baseline

README의 [Quickstart](../README.md#quickstart-baseline에서-시작하기)에서
실행과 검증 흐름을 먼저 확인한 뒤, 아래 예제 중 목적에 가까운 구현을 출발점으로
사용할 수 있습니다. 실제 제출에서는 `src/ossp_router/heuristic.py`를 바꾸거나
같은 `router-run` 인터페이스를 제공하는 구현으로 교체하면 됩니다.

## 모든 문항에 경량 모델 선택

[`always_light.py`](always_light.py)는 모든 문항에 `ax31-light`를 선택하고
세 등급 제출 파일을 한꺼번에 만듭니다. 점수와 비용 계산을 확인하기 위한
가장 단순한 baseline입니다.

## 약한 prompt-heuristic baseline

[`prompt_heuristic.py`](prompt_heuristic.py)는 한 번에 한 등급의 제출 파일을
만듭니다. 문항마다 prompt 또는 messages의 본문에서 다음 단순 특징을 직접
계산합니다.

- 문자·단어·문장 수와 메시지 수
- 한글 문자 비율
- 코드 형태와 수학 기호 수
- 숫자 밀도와 장문 문맥 여부
- 일부 일반적인 추론·분석 어휘

선택 점수는 길이, 코드, 수학, 숫자, 메시지 구조와 장문 여부에 고정된 작은
정수 가중치를 더해 계산합니다. Fast와 Balanced는 장문이 아닌 프롬프트 중
복잡도 임계값을 넘은 문항만 `ax31`로 보내고, Premium은 모든 문항에 `ax31`을
사용합니다. 학습된 출력 길이 예측 없이 K1 비용을 과소평가하지 않도록
`axk1-think`는 선택하지 않습니다. 이는 콘텐츠 기반 라우팅과 등급 전달 방식을
보여 주는 의도적으로 약한 예입니다.

이 라우터는 문항 ID, 입력 위치, 과제명, 출처, 모델 답변, 정답이나 평가
결과를 읽지 않습니다. 모델 선택 함수는 문항 내용과 실행 등급만 받습니다.
ID·순서 변경과 반복 실행의 결정성은
[`../tests/test_prompt_heuristic.py`](../tests/test_prompt_heuristic.py)에서
검사합니다.

```console
PYTHONPATH=src python3 baselines/prompt_heuristic.py \
  --input data/toy/inputs.json \
  --tier balanced \
  --output build/prompt-heuristic-balanced.json
```

실제 비용과 점수는 모델별 평가 결과가 있을 때 self-check로 확인합니다.
라우터 실행 시점에는 모델별 평가 결과를 실행 입력으로 전달하지 않습니다.

## 비용을 함께 배분하는 feature-budget baseline

[`feature_budget.py`](feature_budget.py)는 LM이나 학습 가중치 없이 위 특징에
형식 추론, 프로그램 분석, 다중 제약과 단순 변환 표지를 조금 더합니다. 각
문항을 독립 임계값으로만 처리하지 않고, 같은 특징 점수의 문항을 한 묶음으로
두어 등급별 추정 비용 안에서 전체 묶음을 승격합니다. 따라서 입력 순서나
문항 ID로 동률을 깨지 않습니다.

실제 모델별 생성 출력 토큰 수는 라우터에 제공되지 않으므로 비용 추정은 토큰
단가 비율과 프롬프트 길이에 기반한 대리값입니다. 예산의 85%만 사용해 여유를
둡니다. 학습된 출력 길이 예측 없이 K1 비용을 안전하게 추정하기 어려우므로 이
baseline은 `ax31-light`와 `ax31`만 배분합니다. 실제 Train/Dev의 `self-check`를
대체하지 않습니다.

```console
for tier in fast balanced premium; do
  PYTHONPATH=src python3 baselines/feature_budget.py \
    --input data/toy/inputs.json \
    --tier "$tier" \
    --output "build/feature-budget/$tier.json"
done
```

형식 확인용 toy 자료에서는 all-light가 `0.5`, `prompt-heuristic`과 이
baseline이 각각 `0.58`입니다.

## 학습형 hash-regex 선형 baseline

[`hash_regex.py`](hash_regex.py)는 명시적 정규식 특징과 단어 unigram·bigram의
signed feature hashing을 함께 사용합니다. [`train_hash_regex.py`](train_hash_regex.py)는
공개 Train의 모델별 score와 비용으로 여섯 개의 ridge 회귀 head를 학습합니다.
score와 log-cost를 각각 세 모델에 대해 예측하고, out-of-fold 예측에서 등급별
예산 안전계수를 고릅니다.

Premium에서는 비용 변동이 큰 K1 선택을 기본 안전계수로 먼저 확정합니다. 그
선택을 유지한 채, 전체 예측 비용이 Premium 한도의 65%를 넘지 않는 범위에서
예측 score가 개선되는 Light 선택만 AX31로 추가 승격합니다. Fast와 Balanced에는
이 추가 단계가 적용되지 않습니다.

학습에는 BSD-3-Clause 라이선스의 NumPy만 사용합니다. 생성된 JSON에는 전역
평균·스케일·회귀계수, 등급별 안전계수와 입력 파일 전체의 재현성 해시만
들어갑니다. prompt, 문항 ID, 문항별 특징이나 선택은 저장하지 않습니다.
실제 라우터는 표준 라이브러리만 사용하므로 NumPy를 제출 이미지에 넣을
필요가 없습니다.

공개 자료를 생성한 뒤 Train으로 회귀계수를 학습하고 Dev로 등급별 안전계수
세 값만 보정합니다.

```console
python3 -m pip install -r baselines/requirements-train.txt

PYTHONPATH=src python3 baselines/train_hash_regex.py \
  --input data/materialized/train/inputs.json \
  --outcomes data/train/outcomes.json \
  --validation-input data/materialized/dev/inputs.json \
  --validation-outcomes data/dev/outcomes.json \
  --artifact build/hash-regex/artifact.json \
  --report build/hash-regex/train-report.json

for tier in fast balanced premium; do
  PYTHONPATH=src python3 baselines/hash_regex.py \
    --input data/materialized/dev/inputs.json \
    --artifact build/hash-regex/artifact.json \
    --tier "$tier" \
    --output "build/hash-regex/dev-$tier.json"
done
```

같은 명령으로 만든 전역 계수 학습 파일
[`hash-regex-public.v1.json`](hash-regex-public.v1.json)을 함께 제공합니다.
따라서 학습 없이 아래처럼 바로 실행할 수도 있습니다.

```console
PYTHONPATH=src python3 baselines/hash_regex.py \
  --input data/materialized/dev/inputs.json \
  --artifact baselines/hash-regex-public.v1.json \
  --tier balanced \
  --output build/hash-regex/dev-balanced.json
```

## 공개 Dev 비교

현재 공개 Dev 880문항의 검증 결과입니다. 각 등급 칸은 `점수 / 실제 비용 비율`
이며, 비용 한도는 Fast `1.25`, Balanced `2.0`, Premium `4.0`입니다. 네
baseline 모두 세 등급의 예산을 통과합니다.

| Baseline | Fast | Balanced | Premium | 가중 최종 점수 |
| --- | ---: | ---: | ---: | ---: |
| all-light | 0.619318 / 1.000000 | 0.619318 / 1.000000 | 0.619318 / 1.000000 | 0.619318 |
| prompt-heuristic | 0.625852 / 1.072334 | 0.658239 / 1.367866 | 0.691761 / 2.102044 | 0.655341 |
| feature-budget | 0.621023 / 1.038210 | 0.623580 / 1.334059 | 0.691761 / 2.102044 | 0.643011 |
| hash-regex | 0.663068 / 1.235989 | 0.693750 / 1.961506 | 0.740057 / 3.985205 | 0.695369 |

hash-regex의 전체 보고서는
[`hash-regex-public-dev-report.v1.json`](hash-regex-public-dev-report.v1.json)에
있습니다. 안전계수를 이 Dev 자료로 보정했으므로 이 비교는 공개 자료에서의
동작 확인값이며 비공개 최종 평가 성능 추정치가 아닙니다.

공개 baseline은 구현과 학습 방법을 보여주는 예시이며, 채점용 평가셋에서의
예산 통과를 보증하지 않습니다. 공개 Dev에서 Premium 비용 비율이 `3.985`였던
hash-regex baseline은 채점용 평가셋을 사용한 사전 검증에서 비용 비율이
약 `4.2`로 나타나 `4.0` 한도를 초과했고, 규칙에 따라 Premium 등급 점수가 `0`으로
계산되었습니다. 입력 특성과 모델별 토큰 사용량에 따라 비용 비율이 달라질 수
있으므로, 한도에 근접한 정책에는 충분한 비용 여유를 두어야 합니다. 비용은
반올림 전 값으로 비교하며, 한도를 조금이라도 초과하면 해당 등급 점수는
`0`입니다.

학습기는 ridge alpha를 out-of-fold 평균 오차로 고르고, 등급별 안전계수 후보는
공식 Decimal scorer로 평가합니다. Train self-check는 학습 적합도 확인값일
뿐 일반화 점수가 아닙니다. 공개 Dev는 회귀계수 학습에 합치지 않고 안전계수와
예산 통과 여부를 정하는 데만 사용합니다. 학습 파일에는 전역 계수, 공개 파일
해시와 집계값만 남습니다.
