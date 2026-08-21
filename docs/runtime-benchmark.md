<!--
SPDX-FileCopyrightText: Copyright 2026 SK TELECOM CO., LTD.
SPDX-License-Identifier: Apache-2.0
-->

# Baseline 실행 성능 측정

이 문서는 공개 Train/Dev 입력으로 수행한 Apple Silicon·Colima 공식 플랫폼 참고 측정입니다.
공개 모델별 outcome, 정답, 모델 출력과 최종 평가 자료는 사용하지 않았습니다.

- 문항 수: 2,640
- 반복 횟수: 구현·등급 조합마다 5
- 환경: Darwin 25.3.0 arm64, Python 3.9.6
- 실행 입력 크기: 11.23 MiB
- 반복 사이 운영체제 파일 페이지 캐시 강제 비우기: 아니요

## 호스트 측정 결과

| 구현 | 등급 | 경과 시간 중앙값/최댓값(초) | CPU 중앙값/최댓값(초) | 최대 RSS | 출력 크기 | 결정적 결과 | ID·순서 감사 |
| --- | --- | ---: | ---: | ---: | ---: | --- | --- |
| always-light | fast | 0.2617 / 0.2930 | 0.1460 / 0.1528 | 94.45 MiB | 0.19 MiB | 예 | 통과 |
| always-light | balanced | 0.2125 / 0.3157 | 0.1361 / 0.1398 | 94.38 MiB | 0.19 MiB | 예 | 통과 |
| always-light | premium | 0.2376 / 0.3069 | 0.1373 / 0.1391 | 94.16 MiB | 0.19 MiB | 예 | 통과 |
| prompt-heuristic | fast | 2.2636 / 2.5490 | 2.1759 / 2.2189 | 94.38 MiB | 0.19 MiB | 예 | 통과 |
| prompt-heuristic | balanced | 2.3567 / 2.3962 | 2.2550 / 2.2582 | 94.56 MiB | 0.19 MiB | 예 | 통과 |
| prompt-heuristic | premium | 2.3198 / 2.4319 | 2.1761 / 2.2198 | 94.38 MiB | 0.18 MiB | 예 | 통과 |
| feature-budget | fast | 5.2227 / 5.4443 | 5.0777 / 5.2140 | 94.77 MiB | 0.19 MiB | 예 | 통과 |
| feature-budget | balanced | 5.8679 / 7.3665 | 5.2184 / 5.2784 | 92.84 MiB | 0.19 MiB | 예 | 통과 |
| feature-budget | premium | 5.3139 / 5.4846 | 5.1478 / 5.2428 | 94.22 MiB | 0.18 MiB | 예 | 통과 |
| hash-regex | fast | 9.8828 / 10.5630 | 9.6480 / 9.9655 | 96.55 MiB | 0.19 MiB | 예 | 통과 |
| hash-regex | balanced | 9.4259 / 9.7278 | 9.1329 / 9.3670 | 96.80 MiB | 0.19 MiB | 예 | 통과 |
| hash-regex | premium | 8.9528 / 9.0911 | 8.8895 / 9.0343 | 96.89 MiB | 0.19 MiB | 예 | 통과 |

## 격리 컨테이너 측정 결과

컨테이너 시작 시간을 포함한 경과 시간입니다. 모든 구현을 같은 이미지에서
비교하기 위해 측정 중에만 진입점을 바꾸었으며, 공개 `router-run`
인터페이스 검증은 별도의 통합 테스트에서 수행했습니다.

| 구현 | 등급 | 경과 시간 중앙값/최댓값(초) | CPU 중앙값/최댓값(초) | 최대 RSS | 출력 크기 | 결정적 결과 | ID·순서 감사 |
| --- | --- | ---: | ---: | ---: | ---: | --- | --- |
| always-light | fast | 0.2121 / 0.3167 | 0.1177 / 0.1291 | 72.76 MiB | 0.19 MiB | 예 | 통과 |
| always-light | balanced | 0.2008 / 0.2034 | 0.1079 / 0.1095 | 72.75 MiB | 0.19 MiB | 예 | 통과 |
| always-light | premium | 0.1974 / 0.2112 | 0.1079 / 0.1127 | 72.76 MiB | 0.19 MiB | 예 | 통과 |
| prompt-heuristic | fast | 1.6637 / 1.6680 | 1.5709 / 1.5779 | 72.76 MiB | 0.19 MiB | 예 | 통과 |
| prompt-heuristic | balanced | 1.6601 / 1.6705 | 1.5731 / 1.5780 | 72.70 MiB | 0.19 MiB | 예 | 통과 |
| prompt-heuristic | premium | 1.6659 / 1.6838 | 1.5615 / 1.5887 | 72.70 MiB | 0.18 MiB | 예 | 통과 |
| feature-budget | fast | 3.8301 / 3.8378 | 3.7295 / 3.7415 | 72.76 MiB | 0.19 MiB | 예 | 통과 |
| feature-budget | balanced | 3.8356 / 3.8388 | 3.7315 / 3.7456 | 72.81 MiB | 0.19 MiB | 예 | 통과 |
| feature-budget | premium | 3.8282 / 3.8594 | 3.7316 / 3.7459 | 72.75 MiB | 0.18 MiB | 예 | 통과 |
| hash-regex | fast | 7.3152 / 7.4688 | 7.2086 / 7.3700 | 72.76 MiB | 0.19 MiB | 예 | 통과 |
| hash-regex | balanced | 7.3433 / 7.4958 | 7.2324 / 7.3977 | 72.76 MiB | 0.19 MiB | 예 | 통과 |
| hash-regex | premium | 7.3906 / 7.5791 | 7.2898 / 7.4794 | 72.88 MiB | 0.19 MiB | 예 | 통과 |

위 컨테이너 결과는 CPU 2코어, 메모리 2.00 GiB, `--memory-swap` 합계 2.00 GiB, PID·스레드 합계 32개로 측정했습니다.
임시 공간은 256.00 MiB였으며, 이 값들은 아래 최종 프로필을 적용한 공식 플랫폼 참고 측정 조건입니다.

공식 플랫폼 cgroup v2 반복 측정의 관측 최댓값:

- `memory.peak`: 76.66 MiB
- `pids.peak`: 6
- CPU 사용량 증가분: 7441739 µs
- `cpu.stat` 최종 사용량/제한 횟수/제한 시간: 7479884 µs / 0 / 0 µs

## 최종 자원 한도

아래 값은 대표 라우터 측정과 공식 출력·이미지 경계 검증을 통과해 동결한 등급별 컨테이너 한 번의 최종 상한입니다.
현재 baseline 측정일은 2026-08-07입니다.

- CPU: 2코어
- 전체 실행 시간: 90초
- 메모리: 2.00 GiB, 추가 스왑 없음
- 프로세스·스레드: 합계 32개
- 제한 출력 볼륨: 4.00 MiB, inode 64개
- 실행 로그: 스트림별 0.25 MiB 보관, 1.00 MiB 출력량 한도
- 임시 디렉터리: 256.00 MiB
- 선택한 플랫폼의 OCI 매니페스트(manifest)에 기록된 압축 계층(layer) 크기 합계 최종 한도: 1.00 GiB
- 풀린 실행 루트 파일 시스템(rootfs)의 겉보기 크기(apparent size) 최종 한도: 2.00 GiB

Apple Silicon 호스트·Colima·linux/arm64 대표 측정과 공식 출력·이미지 경계 통합 검증을 마쳐 위 한도를 동결했습니다.

## 컨테이너 측정 상태

Docker 컨텍스트 `colima`에서 네트워크 없음, 읽기 전용 루트, 비특권 사용자, CPU·메모리·프로세스 제한을 적용해 실제 실행했습니다.
컨테이너 서버 환경은 linux/arm64입니다.

- `docker image inspect`에서 얻은 이미지 `Size`: `ossp-router:measurement` (21.92 MiB)
- 컨테이너 안에서 측정한 읽기 전용 루트 파일 시스템 겉보기 크기: 51.77 MiB

위 로컬 값, OCI 매니페스트의 압축 계층 합계와 풀린 루트 파일 시스템은
서로 다른 지표이므로 바꿔 쓸 수 없습니다.

고정한 기반 이미지는 `python:3.11.15-alpine3.23`이며 다중 플랫폼
인덱스 다이제스트는 `sha256:f73754c398b259dfbbe482361dca8b464dea57da74efe5214966ca2ee767ee12`입니다.
세부 반복값과 해시는 [runtime-benchmark.json](runtime-benchmark.json)에
기록했습니다.
