<!--
SPDX-FileCopyrightText: Copyright 2026 SK TELECOM CO., LTD.
SPDX-License-Identifier: Apache-2.0
-->

# Apple Silicon 측정과 자원 한도 동결

최종 평가는 Apple Silicon 장비에서 Colima의 `linux/arm64` Linux VM과 Docker
runtime을 사용합니다. 이 문서는 플랫폼을 정당화하기 위한 참가자 요구사항이
아니라, 운영자가 같은 조건으로 자원 한도와 동점 레이턴시를 확정하는
절차입니다.

## 참가자에게 의미하는 것

- 개발 장비의 운영체제와 CPU는 자유입니다.
- 제출 이미지는 반드시 `linux/arm64`로 실행되어야 합니다.
- 품질 점수는 라우팅 결정으로 계산하므로 참가자 장비 성능은 점수에 영향을
  주지 않습니다.
- 동점 레이턴시는 모든 동점 제출을 동일한 공식 장비와 조건에서 다시
  측정합니다.

## 공식 장비와 동결 결과

공식 측정은 Apple M3 Pro, 메모리 18 GB, macOS 26.3.1, Colima 0.10.3에서
완료했습니다. Colima `default` profile은 aarch64, CPU 4개, 메모리 6 GiB,
디스크 20 GiB와 Docker runtime을 사용했습니다. Docker 서버는
`linux/arm64`, cgroup v2였습니다.

네 baseline의 공개 Train/Dev 전체 반복 측정과 83개 Docker 실행 경계 통합 테스트를
통과해 참가 컨테이너 한도를 CPU 2개, 메모리 2 GiB, 추가 스왑 없음,
프로세스·스레드 합계 32개와 등급별 90초로 동결했습니다. 출력·임시 공간,
로그와 이미지 크기를 포함한 전체 값은 [`RUNTIME.md`](RUNTIME.md)에
정의합니다.

## 재측정 전 조건

1. 공개 준비 커밋과 작업 트리가 확정되어 있어야 합니다.
2. Docker 이미지가 그 소스 파일 목록 SHA-256을 OCI label로 포함해야 합니다.
3. Colima profile과 Docker daemon은 `aarch64`/`linux/arm64`, cgroup v2여야
   합니다.
4. Docker context는 해당 Mac의 로컬 Unix socket이어야 합니다.
5. 공개 Train/Dev materialization을 완료하고 두 입력 파일의 SHA-256과 문항
   수가 `data/public-data.v1.json`과 일치해야 합니다. 모델별 outcome과 최종
   평가 자료는 측정에 사용하지 않습니다.

## Baseline 측정과 경계 검증

소스 결속 label을 넣어 이미지를 빌드합니다.

```console
SOURCE_MANIFEST_SHA256="$(PYTHONPATH=src python3 tools/benchmark_runtime.py \
  --print-source-manifest-sha256)"

docker build --pull --platform linux/arm64 \
  --build-arg "SOURCE_MANIFEST_SHA256=${SOURCE_MANIFEST_SHA256}" \
  --tag ossp-router:measurement \
  --file container/measurement.Dockerfile .
```

공식 출력 tmpfs·inode·격리·이미지 경계 통합 테스트를 먼저 실행합니다.

```console
OSSP_RUN_CONTAINER_TESTS=1 PYTHONPATH=src \
  python3 -m unittest tests.test_runtime
```

이 테스트가 `Ran 83 tests; OK`로 끝난 뒤 공개 Train/Dev 전체를
구현·등급 조합별 5회로 측정합니다. 두 확인 문자열은 운영자가 각 조건을
직접 확인한 경우에만 지정합니다.

```console
PYTHONPATH=src python3 tools/benchmark_runtime.py \
  --measurement-mode apple-silicon-colima \
  --apple-silicon-operator-attestation I_ATTEST_APPLE_SILICON_COLIMA \
  --final-runtime-boundary-attestation I_ATTEST_FINAL_RUNTIME_BOUNDARIES_PASSED \
  --apple-silicon-environment-label apple-m3-pro-colima-official \
  --colima-profile default \
  --container-image ossp-router:measurement
```

도구는 호스트·Colima·Docker 아키텍처, cgroup v2, 이미지·소스 결속,
결정성, ID·순서 불변성과 반복별 자원값이 없으면 완료 보고서를 만들지
않습니다. 보고서는 `docs/runtime-benchmark.json`과 같은 내용을 요약한
`docs/runtime-benchmark.md`로 함께 게시합니다.

## 최종 한도 동결 기준

baseline만 빠르다는 이유로 한도를 줄이지 않습니다. 공개 규칙에서 허용할
대표적인 프롬프트 특징 기반 구현과 학습형 선형 분류기 구현을 같은 이미지
경계에서 측정했습니다. 다음 조건을 모두 만족한 경우에만 보고서 상태를
`final-frozen`으로 기록합니다.

- 모든 대표 구현이 공개 Train/Dev 전체와 세 등급에서 제한에 걸리지 않음
- 반복 사이 결과가 결정적이고 ID·순서 감사를 통과함
- cgroup `memory.peak`, `pids.peak`, CPU throttling과 실행 시간 기록이 완전함
- 공식 tmpfs 출력 볼륨, 임시 공간과 OCI 이미지 크기 경계를 별도로 검증함
- 여유 폭과 반올림 원칙을 문서와 정책 파일에 같은 값으로 반영함

현재 보고서는 모든 조건을 충족해 `final-frozen`이며, `RUNTIME.md`,
`ENFORCEMENT.md`, 정책 파일, 실행기 상수와 테스트에 같은 값을 반영했습니다.
반복별 관측값과 환경 증거는 [`runtime-benchmark.md`](runtime-benchmark.md)와
JSON 원본에 기록했습니다.

## 동점 레이턴시

품질 점수가 같은 제출만 대상으로, 같은 공식 장비에서 준비 실행 후 각 등급을
5회 측정합니다. 등급별 중앙값의 합이 낮은 제출을 우선합니다. 네트워크,
감사 재실행, 이미지 pull, 운영자 장애와 준비 실행 시간은 포함하지 않습니다.
측정 중 CPU·메모리·프로세스 제한은 본 평가와 같게 유지합니다.
