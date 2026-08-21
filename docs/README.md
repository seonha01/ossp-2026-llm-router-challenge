<!--
SPDX-FileCopyrightText: Copyright 2026 SK TELECOM CO., LTD.
SPDX-License-Identifier: Apache-2.0
-->

# 문서

## 참가자 필수

- [CHALLENGE_RULES.md](CHALLENGE_RULES.md): 라우터 실행 입력·선택 결과와 참가 규칙
- [SUBMISSION.md](SUBMISSION.md): 제출 파일, 이미지와 로컬 점검 방법
- [RUNTIME.md](RUNTIME.md): 컨테이너 명령, 경로, 격리와 선택 결과 검증
- [DATA_CARD.md](DATA_CARD.md): 공개 자료와 평가 자료의 공개 범위

## 필요할 때 참고

- [SCORING.md](SCORING.md): 비용, 예산 한도, 등급별 점수 계산
- [ENFORCEMENT.md](ENFORCEMENT.md): 재실행, 실행 실패와 전체 실격의 구분
- [DATA_LICENSES.md](../DATA_LICENSES.md): 자료별 라이선스와 귀속

## 공개 운영 절차

아래 문서는 운영자와 검토자를 위한 공개 절차입니다. 비공개 최종 평가
스냅샷과 운영 환경의 실제 경로는 저장소 밖에 보관합니다.

- [OPERATIONS.md](OPERATIONS.md): 평가 실행, 이미지 증거, 기록과 안전한 복구
- [TIEBREAK_LATENCY.md](TIEBREAK_LATENCY.md): 동점 제출의 공식 레이턴시
  측정·순위 기록 절차

## 공개 재현성·검토 자료

- [runtime-benchmark.md](runtime-benchmark.md): 공개 Train/Dev 공식 플랫폼 참고 측정
- [APPLE_SILICON_MEASUREMENT.md](APPLE_SILICON_MEASUREMENT.md): 공식
  Apple Silicon·Colima 측정과 자원 한도 동결 절차
- [REVIEW_GUIDE.md](REVIEW_GUIDE.md): 관점별 검토 범위와 한국어 표현 확인 사항

참가자에게 적용되는 최종 자원 한도는 측정 문서가 아니라
[`RUNTIME.md`](RUNTIME.md)를 기준으로 합니다.
참가자 이미지는 `tools/check_runtime.py`로 공개 Train/Dev에서 미리 점검할 수
있습니다.
