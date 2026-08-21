<!--
SPDX-FileCopyrightText: Copyright 2026 SK TELECOM CO., LTD.
SPDX-License-Identifier: Apache-2.0
-->

# 데이터 디렉터리

- `toy/`: 이 저장소에서 새로 작성한 프로토콜 예제
- `train/`, `dev/`: 검토를 마친 공개 프롬프트와 모델 답변 본문을 제외한
  평가 결과 파일을 위한 위치
- `sources/`: 고정한 출처, 라이선스 근거, 귀속, 재현 가능한 변환 기록

Train/Dev에는 재배포 가능한 원본 프롬프트, 원문 직접 받기용 선택 정보와 모델
답변 본문을 제외한 평가 결과가 있습니다. 전체 실행 입력 생성 방법은 각 구분의
README와 [`public-data.v1.json`](public-data.v1.json)을 참고하십시오.
출처별 권리와 배포 조건은 [`DATA_LICENSES.md`](../DATA_LICENSES.md)에
기록합니다. 로컬에서 가져오거나 생성한 자료는
`data/materialized/` 또는 캐시 디렉터리인 `data/cache/`에 두며 Git 추적
대상이 아닙니다.
