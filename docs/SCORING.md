<!--
SPDX-FileCopyrightText: Copyright 2026 SK TELECOM CO., LTD.
SPDX-License-Identifier: Apache-2.0
-->

# 점수 계산

확정된 평가 정책 ID는 `ossp-2026-prompt-router-v1`, JSON 형식 버전은
정수 `1`입니다. 정책은 비용 단위 `credits`, 토큰 단위 `1,000,000`, 공통
문맥 한도 `32,768`, 기준 모델 `ax31-light`를 함께 고정합니다.

정책의 `context_limit_tokens`는 세 모델의 사전 계산 평가 결과를 만들 때
적용한 공통 전체 문맥 조건을 기록하는 생성 이력(provenance)입니다. 점수
계산기는 이 필드를 정책 해시에 포함하지만, 참가자 라우터의 입력을 자르거나
컨테이너 실행 중 별도의 토큰 한도로 강제하지 않습니다.

## 비용 계수

| 모델 | 고정 비용 | 모델 입력 토큰 계수 | 모델 출력 토큰 계수 |
| --- | ---: | ---: | ---: |
| `ax31-light` | 0 | 1 | 4 |
| `ax31` | 0 | 2.127 | 8.509 |
| `axk1-think` | 0 | 6.565 | 26.260 |

문항 하나의 비용은 다음과 같습니다.

```text
episode_cost =
    fixed_cost
  + input_tokens  * input_token_rate  / token_unit
  + output_tokens * output_token_rate / token_unit
```

## 예산

각 등급의 기준 비용은 같은 데이터 구분(split)의 모든 문항에
`ax31-light`를 선택했을 때의 비용입니다.

```text
budget_ratio = total_cost / light_baseline_cost
budget_limit = light_baseline_cost * budget_multiplier
```

| 등급 | 최대 비용 비율 | 가중치 | 한도 초과 |
| --- | ---: | ---: | --- |
| Fast | 1.25 | 0.4 | 등급 점수 0 |
| Balanced | 2.0 | 0.3 | 등급 점수 0 |
| Premium | 4.0 | 0.3 | 등급 점수 0 |

예산은 실제 모델 서빙의 동시성, 대기열과 메모리 등 운영 용량을 추상화한
제약입니다. 용량을 넘기면 응답 시간 목표를 지키지 못하거나 서빙이 실패할 수
있으므로, 이 과제에서는 품질 점수에서 조금 감점하는 대신 반드시 지켜야 하는
한도로 평가합니다.

비용이 한도와 정확히 같으면 통과합니다. 한도를 조금이라도 초과하면 해당
등급의 점수는 0입니다. self-check는 등급 한도의 95% 이상을 사용하면
`near_budget`을 표시합니다.
공개 Train/Dev에서 예산을 통과한 라우터도 채점용 평가셋에서의 통과를
보장하지 않습니다. 한도 근접 baseline의 사전 검증 사례는
[`baselines/README.md`](../baselines/README.md#공개-dev-비교)를 참고하십시오.

## 품질 점수

예산 안에 있으면 선택한 모델의 문항별 `score` 평균이 등급 점수입니다. 예산을
초과하면 품질 점수 합계는 감사용으로 보존하되, 최종 점수에 반영하는 등급 점수
합계는 `0`입니다.

```text
quality_points_total = sum(selected outcome scores)
tier_points_total = quality_points_total if budget_passed else 0
tier_score = tier_points_total / number_of_episodes
```

최종 점수는 예산 초과 시 `0`을 적용한 세 등급의 정확한 점수 합계에 가중치를
적용한 뒤 전체 문항 수로 나눈 값입니다.

```text
final_score =
    (0.4 * fast_tier_points_total
   + 0.3 * balanced_tier_points_total
   + 0.3 * premium_tier_points_total)
    / number_of_episodes
```

## 동점 처리

공식 순위에서 점수가 같은 제출은 prompt-only 라우터의 공식 실행 레이턴시가
낮은 순서로 평가합니다. 이 레이턴시는 모델 추론이나 답변 비교 시간이 아니라,
제출 컨테이너가 주어진 등급의 입력을 받아 유효한 모델 선택 JSON을 만드는 데
걸린 시간입니다.

동점 여부는 표시용으로 반올림하기 전의 정확한 `Decimal` 최종 점수로
판단합니다. 동점이면서 전체 실격되지 않은 제출만 같은 비공개 최종 입력
스냅샷, 변경 불가능한 이미지, 동일한 Apple Silicon·Colima
`linux/arm64` 실행 장비와 확정 자원 한도에서 별도로 측정합니다.

각 등급은 기록에 포함하지 않는 준비 실행 한 번 뒤 5회 측정합니다. 여러 동점
제출의 실행 순서는 반복마다 교대합니다. 측정 구간은 참가자 `docker run`
프로세스를 시작하기 직전부터 출력 파일을 추출하여 v1 제출 검증에 통과한
직후까지이며, 컨테이너 시작과 참가자 초기화 시간을 포함합니다. 참가자 실행이
유효한 제출을 만들지 못하면 그 반복에는 해당 등급의 확정 실행 시간 한도를
부여합니다. 운영자 장애로 무효가 된 반복은 버리고 같은 조건으로 다시
측정합니다.

등급별 5회 측정의 중앙값을 구한 뒤 Fast·Balanced·Premium 중앙값을
합산합니다. 합계가 낮은 제출을 높은 순위로 정합니다. 채점 중 공식 재실행과
문항 ID·순서 감사 재실행은 동점 처리 표본에 포함하지 않습니다. 측정 합계도
타이머 정밀도에서 같으면 공동 순위로 처리합니다. 로컬 선별 측정값은 공식
동점 처리에 사용하지 않습니다. 운영 입력과 구조화 기록은
[`TIEBREAK_LATENCY.md`](TIEBREAK_LATENCY.md)에 정의합니다.

## 정밀도와 감사 정보

모든 비용·품질 연산은 160자리 `Decimal` 문맥에서 수행합니다. 비용 한도는
반올림하기 전 값으로 비교합니다. 표시용 비율과 점수는 소수점 12자리에서
half-even 방식으로 양자화한 뒤 불필요한 끝자리 0을 제거합니다.

self-check 보고서에는 정규화한 정책의 SHA-256, 모델별 선택 수,
선택된 모델의 입력·출력 토큰과 생성 수, 정확한 예산 한도, 비용·토큰 단위,
`quality_points_total`, `tier_points_total`, 소수점 자릿수와 반올림 규칙이
포함됩니다. 정책
SHA-256은 기본값을 채운 정책 객체를 십진 문자열로 정규화하고, 키를 정렬한
공백 없는 UTF-8 JSON의 SHA-256으로 계산합니다.
