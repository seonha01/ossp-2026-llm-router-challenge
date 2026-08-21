<!--
SPDX-FileCopyrightText: Copyright 2026 SK TELECOM CO., LTD.
SPDX-License-Identifier: Apache-2.0
-->

# Baseline 컨테이너

[`Dockerfile`](Dockerfile)은 약한 프롬프트 기반 baseline을 표준
`router-run` 인터페이스로 실행합니다. 라우터 실행 입력 JSON의 컨테이너 내부
경로는 `/challenge/input/inputs.json`, 선택 결과 JSON의 경로는
`/challenge/output/submission.json`, 임시 경로는 `/tmp`입니다.

구체적인 인자, 파일 권한, 제한 시간 초과와 비정상 종료, 출력 검증,
CPU, RAM, 프로세스·스레드 수의 최종 한도는
[`../docs/RUNTIME.md`](../docs/RUNTIME.md)에 정의합니다. 운영자 측 기술 장애,
최대 3회 실행, 첫 유효 결과와 전체 실격 사유는
[`../docs/ENFORCEMENT.md`](../docs/ENFORCEMENT.md)에 정의합니다.

컨테이너는 네트워크 없이, 비특권 UID/GID `65532:65532`, 읽기 전용 파일 시스템에서
실행하도록 설계했습니다. 참가자에게는 시도별 4 MiB 제한 출력 볼륨과
256 MiB `/tmp`만 쓰기 가능하며 GPU나 별도 device를 전달하지 않습니다.
공유 메모리는 제공하지 않고 이미지의 모든 `VOLUME` 선언은 실행 전에
거부합니다. 기반 이미지 출처와 다이제스트는
[`BASE_IMAGE.md`](BASE_IMAGE.md)에 기록합니다.

출력 회수, Docker 자원 정리와 장애 복구 방식은
[`../docs/OPERATIONS.md`](../docs/OPERATIONS.md)에 정의합니다.

Colima의 Docker 호환 실행기에서 실제 이미지 빌드와 네트워크 없음, GPU 없음,
비특권 사용자, 읽기 전용 루트 파일 시스템 조건을 검증했습니다. 통합 테스트는
`OSSP_RUN_CONTAINER_TESTS=1`로 켤 수 있습니다. 공개 Train/Dev 호스트·격리
컨테이너 측정 결과와 동결한 최종 자원 한도는
[`../docs/runtime-benchmark.md`](../docs/runtime-benchmark.md)에 있습니다.
측정과 한도 동결 절차는
[`../docs/APPLE_SILICON_MEASUREMENT.md`](../docs/APPLE_SILICON_MEASUREMENT.md)를
따릅니다.

참가자가 자신의 최종 이미지를 같은 공개 Train/Dev와 자원 제한으로 확인하는
명령은 [`../docs/RUNTIME.md`](../docs/RUNTIME.md#로컬-검증)에 안내합니다.
