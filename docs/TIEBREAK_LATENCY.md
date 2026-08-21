<!--
SPDX-FileCopyrightText: Copyright 2026 SK TELECOM CO., LTD.
SPDX-License-Identifier: Apache-2.0
-->

# 동점 레이턴시 운영 측정

이 문서는 정확한 최종 점수가 같은 제출을 운영자가 별도 측정할 때 사용하는
절차입니다. 참가자가 제출 컨테이너에 추가로 구현해야 하는 인터페이스는
없습니다. 공식 규칙은 `SCORING.md`의 동점 처리 절을 따릅니다.

## 측정 계약

- 동점이면서 전체 실격되지 않은 제출만 한 번에 측정합니다.
- 모든 제출은 같은 공식 입력 스냅샷, 변경 불가능한 이미지, 정책, 실행 장비,
  플랫폼과 자원 한도를 사용합니다.
- 각 등급에서 제출별 준비 실행 1회를 기록에서 제외한 뒤 5회 측정합니다.
- 측정 반복마다 제출 순서를 한 칸씩 순환해 시작 순서를 교대합니다.
- 공용 격리 실행기가 `docker run` 시작 직전부터 출력 추출과 v1 검증 직후까지
  `perf_counter` 단조 시계로 측정한 나노초만 사용합니다.
- 참가자 실행 실패에는 해당 등급 실행 시간 한도를 부여합니다. 운영자 장애는
  표본에서 버리고 같은 제출과 등급을 다시 실행합니다.
- 등급별 5회 중앙값을 합산하고, 합계를 타이머 정밀도로 반올림한 값이 같으면
  공동 순위로 기록합니다.

공식 기록에는 제출 식별 정보, 측정값, 중앙값, 합계, 순위 근거와 운영자 장애로
버린 실행 수만 포함합니다. 입력 내용·입력 해시, 라우터 출력 내용, 로그와 실패
상세는 기록하지 않습니다.

## 운영자 입력 파일

`--candidates` 파일은 공개 제출 접수 자료와 이미지 크기 증거를 연결하는 운영자
전용 JSON입니다. 이미지 크기 증거 파일은 기존 이미지 측정 절차가 만든 운영자
소유 권한 `0600` 파일이어야 합니다.

```json
{
  "schema_version": 1,
  "candidates": [
    {
      "submission_id": "entry-a",
      "commit_sha": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
      "image": "registry.example/router-a@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
      "image_size_evidence": "evidence/entry-a.json"
    },
    {
      "submission_id": "entry-b",
      "commit_sha": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
      "image": "registry.example/router-b@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
      "image_size_evidence": "evidence/entry-b.json"
    }
  ]
}
```

상대적인 이미지 크기 증거 경로는 이 JSON 파일의 상위 디렉터리를 기준으로
해석합니다. `submission_id`는 기록에서 제출을 구분하기 위한 불투명 식별자이며
라우팅 입력으로 전달되지 않습니다.

## 실행

출력 디렉터리와 비공개 작업 디렉터리는 서로 분리된 운영자 소유 권한 `0700`
경로여야 합니다. 비공개 작업 디렉터리는 실행 전에 비어 있어야 합니다.

```bash
router-measure-tiebreak \
  --candidates operator-candidates.json \
  --input final-input.json \
  --output-directory latency-output \
  --private-work-directory latency-work \
  --record latency-output/official-latency.json
```

기본 정책 대신 별도 고정 정책을 사용할 때만 `--policy`를 지정합니다. 특정 Docker
context를 고정하려면 `--docker-context`를 사용합니다. 완료된 기록의
`report_type`은 `official-tiebreak-latency-record`, `status`는 `complete`입니다.

종료 코드는 성공 `0`, 운영 설정 오류 `2`, 운영자 인프라 사용 불가 `4`, 이미지
사전 검사 거부 `6`입니다. 운영자 장애가 연속 3회 발생하거나 정리 대기 상태가
되면 불완전한 순위 기록을 게시하지 않고 종료합니다.
