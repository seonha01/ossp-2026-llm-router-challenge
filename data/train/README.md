<!--
SPDX-FileCopyrightText: Copyright 2026 SK TELECOM CO., LTD.
SPDX-License-Identifier: Apache-2.0
-->

# Train 데이터

- `inputs-base.json`: 재배포 가능한 prompt 1,736개
- `aime-selection.json`: 원문을 포함하지 않는 source-fetch-only 선택 24개
- `outcomes.json`: 전체 1,760개 문항의 모델 답변 본문을 제외한 평가 결과

먼저 [자료 생성 절차](../sources/README.md)에 따라 Python 3.10 이상 환경과
고정 의존성을 준비합니다. 저장소 루트에서
`.venv-data/bin/python tools/materialize_public_data.py --split train`을 실행하면
검증된 전체 실행 입력이 `data/materialized/train/inputs.json`에 생깁니다.
AIME 문제문, 정답, 모델 답변은 이 디렉터리에 커밋하지 않습니다. 파일 해시는
[`../public-data.v1.json`](../public-data.v1.json), 제3자 조건은
[`../../THIRD_PARTY_NOTICES.md`](../../THIRD_PARTY_NOTICES.md)를 참고하십시오.
