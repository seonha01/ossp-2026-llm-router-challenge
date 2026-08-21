<!--
SPDX-FileCopyrightText: Copyright 2026 SK TELECOM CO., LTD.
SPDX-License-Identifier: Apache-2.0
-->

# JSON 스키마

이 디렉터리의 Draft 2020-12 스키마는 공개 파일 형식 명세를 설명합니다.
실제 검증은 중복 JSON 키, 문항(episode) 완전성, 모델별 평가 결과(outcome)
행렬 완전성처럼 JSON Schema만으로 표현하기 어려운 조건까지
`ossp_router.protocol`과
`ossp_router.scoring`에서 확인합니다.

- [`input.v1.schema.json`](input.v1.schema.json): prompt-only 라우터 실행 입력
- [`outcome.v1.schema.json`](outcome.v1.schema.json): 모델별 공개 평가 결과
- [`submission.v1.schema.json`](submission.v1.schema.json): 등급별 모델 선택 결과
- [`technical-submission.v1.schema.json`](technical-submission.v1.schema.json):
  제출 저장소 루트에 커밋하는 `submission-ossp-skt.json`. 저장소의
  `tools/validate_technical_submission.py`로 검사할 수 있습니다.
- [`policy.v1.schema.json`](policy.v1.schema.json): 비용과 tier 정책
