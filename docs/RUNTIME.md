<!--
SPDX-FileCopyrightText: Copyright 2026 SK TELECOM CO., LTD.
SPDX-License-Identifier: Apache-2.0
-->

# 컨테이너 실행 규격

이 문서는 참가자 이미지가 지켜야 하는 실행 인터페이스와 격리 조건을
정의합니다. 이미지 증거 생성, 운영 경로 보호, 정리 저널과 복구 같은 평가
운영 절차는 [`OPERATIONS.md`](OPERATIONS.md)에 분리했습니다.

이 문서에서 **실행 입력**은 prompt-only 입력 JSON, **선택 결과**는 문항별
`model_id`를 기록한 제출 JSON을 뜻합니다. 모델 답변과 모델별 평가 결과는
참가자 컨테이너의 입력이나 출력이 아닙니다.

## 참가자 필수 요약

| 항목 | 요구사항 |
| --- | --- |
| 플랫폼 | `linux/arm64` |
| 진입 명령 | `router-run --input ... --tier ... --output ...` |
| 실행 단위 | 등급 하나와 입력 전체 |
| 네트워크·GPU | 사용 불가 |
| 파일 시스템 | 읽기 전용 루트 파일 시스템, `/tmp`와 출력 볼륨만 쓰기 가능 |
| 선택 결과 | `/challenge/output/submission.json` 한 개 |

공식 평가는 Apple Silicon 장비의 Colima Linux VM에서 같은 이미지
다이제스트를 세 등급에 각각 실행합니다. 참가자는 다른 운영체제에서 개발해도
되지만, 제출 이미지는 반드시 `linux/arm64`로 빌드하고 이 문서의
인터페이스를 지켜야 합니다.

## 한 번의 실행

컨테이너 한 번은 한 등급의 선택 결과 JSON 하나를 생성합니다. 표준 호출 형식은
다음과 같습니다.

```console
router-run \
  --input /challenge/input/inputs.json \
  --tier fast \
  --output /challenge/output/submission.json
```

`--tier`에는 `fast`, `balanced`, `premium` 중 하나를 사용합니다. 운영자는
같은 커밋에서 빌드한 동일한 이미지 다이제스트를 등급마다 실행합니다. 입력
전체를 한 배치로 전달하며 표준 경로는 다음과 같습니다.

| 용도 | 컨테이너 내부 경로 | 접근 |
| --- | --- | --- |
| 입력 파일 | `/challenge/input/inputs.json` | 읽기 전용 |
| 제출 파일 | `/challenge/output/submission.json` | 제한 출력 볼륨 안에서 쓰기 가능 |
| 임시 디렉터리 | `/tmp` | 실행마다 비우는 tmpfs |

출력은 v1 제출 형식의 JSON 하나여야 합니다. 성공 시 종료 코드는 `0`이고
출력 파일 권한은 `0644`입니다. 입력·인자·형식 또는 파일 쓰기 오류는 종료
코드 `2`와 표준 오류의 짧은 설명으로 알립니다. 표준 출력의 성공 메시지는
점수에 사용하지 않습니다.

출력은 같은 디렉터리의 임시 파일에 완전히 쓴 뒤 원자적으로 교체해야 합니다.
부분 JSON은 유효한 결과로 사용하지 않습니다. 출력 볼륨의 루트에는
`submission.json` 외의 파일, 디렉터리나 링크를 만들지 마십시오.

## 이미지 빌드와 제출

```console
docker build \
  --pull \
  --platform linux/arm64 \
  --file container/Dockerfile \
  --tag ossp-router:local \
  .
```

제공하는 기준 Dockerfile은 가장 단순한 제출 이미지 예시이며 반드시 이 기반
이미지를 사용할 필요는 없습니다. 이 예시는 `python:3.11.15-alpine3.23`의
다중 플랫폼 인덱스 다이제스트를 고정하고 저장소의 실행 코드만 복사합니다.
참가자 이미지는 필요한 공개 의존성을 빌드 단계에서 설치할 수 있지만, 버전과
라이선스를 기록하고 실행 중 다운로드 없이 동작해야 합니다. 기반 이미지
출처와 라이선스는
[`../container/BASE_IMAGE.md`](../container/BASE_IMAGE.md)에 기록했습니다.

최종 제출에서는 공개 저장소의 평가할 커밋 SHA에서 이미지를 빌드하고,
레지스트리에 푸시한 뒤 얻은 변경 불가능한 이미지 다이제스트와 커밋 SHA의
대응 관계를 제출합니다. 평가 대상 커밋 이후의 파일은 이미지에 넣지
않습니다. 변경 가능한 태그, 짧거나 대문자인 다이제스트, `linux/arm64`가
아닌 이미지는 받지 않습니다.

이미지가 `VOLUME`을 선언하면 접수 전에 거부합니다. 이미지 크기는 표의 두
한도를 모두 적용하며, 정확한 측정 방식은
[`OPERATIONS.md`](OPERATIONS.md#이미지-식별과-크기-증거)에 기록합니다.

## 격리와 자원 한도

참가자 컨테이너에는 네트워크, GPU 또는 별도 장치를 제공하지 않습니다.
실행 중 내려받기, 외부 추론 호출, 비공개 패키지 또는 비공개 모델 사용은
허용하지 않습니다. 컨테이너는 다음 조건에서 실행합니다.

- 숫자 UID/GID `65532:65532`, Linux 권한(capability) 없음
- 읽기 전용 루트 파일 시스템
- 시도마다 비우는 `/tmp`와 제한 출력 볼륨만 쓰기 가능
- `--ipc none`, `--cgroupns private`, `--ulimit core=0:0`
- `--log-driver none`, `--stop-signal SIGTERM`, `--no-healthcheck`

실제 참가자 명령에는 `--cpus 2`, `--memory 2g`, `--memory-swap 2g`,
`--pids-limit 32`와 `/tmp`의 `size=256m`을 적용합니다.
등급별 실행 시간 90초도 같은 자원 프로필에 포함합니다.

공식 평가에 적용하는 최종 자원 한도는 다음과 같습니다.

| 자원 | 최종 한도 |
| --- | ---: |
| CPU | 2코어 |
| 메모리 | 2 GiB, 추가 스왑 없음 |
| 프로세스·스레드 합계 | 32개 |
| 등급별 실행 시간 | 90초 |
| `/tmp` | 256 MiB |
| 출력 볼륨 | 4 MiB, inode 64개 |
| stdout·stderr | 스트림별 총 출력량 1 MiB, 보관량 256 KiB |
| OCI 압축 계층 합계 | 1 GiB |
| 병합 루트 파일 시스템 겉보기 크기 | 2 GiB |

공개 Train/Dev 전체 입력으로 네 baseline을 공식 Apple Silicon·Colima
환경에서 측정하고 Docker 출력·이미지 경계 통합 검증을 마쳐 위 값을
동결했습니다. 측정 절차와 환경은
[`APPLE_SILICON_MEASUREMENT.md`](APPLE_SILICON_MEASUREMENT.md), 반복별 결과와
증거는 [`runtime-benchmark.md`](runtime-benchmark.md)를 참고하십시오.

표준 출력과 표준 오류는 각각 총 1 MiB를 넘을 수 없고, 출력 파일은 4 MiB와
inode 64개 한도 안에서 `submission.json` 하나만 만들어야 합니다. 로그 수집과
제한 출력 볼륨의 구현은
[`OPERATIONS.md`](OPERATIONS.md#출력-볼륨과-도우미)에 기록합니다.

## 시간 초과와 종료

등급별 실행 시간 90초가 지나거나 로그 출력량 한도를 넘으면 운영자
실행기가 참가자 컨테이너를 종료합니다. Docker는 먼저 `SIGTERM`을 전달하고
5초 안에 끝나지 않으면 `SIGKILL`을 전달합니다. 메모리, 프로세스·스레드 수,
임시 공간 또는 출력 공간 한도를 넘겨 종료된 경우도 참가자 실행 실패입니다.

Docker 호스트, 이미지 레지스트리, 입력 마운트나 평가 실행기의 문제로
컨테이너를 정상적으로 시작하거나 검사할 수 없었던 경우는 운영자 인프라
장애입니다. 참가자의 공식 실행 횟수에 포함하지 않습니다. 자세한 분류와
재실행 규칙은 [`ENFORCEMENT.md`](ENFORCEMENT.md)를 따릅니다.

## 출력 검증

종료 코드 `0`만으로 성공으로 보지 않습니다. 운영자는 다음 항목을 모두
검사하고 첫 유효 결과만 사용합니다.

- 출력 파일 존재, 크기와 UTF-8 JSON 형식
- 출력 공간에 다른 파일·디렉터리·링크가 없는지 여부
- v1 `schema_version`, 실행 등급과 `policy_id`
- 입력과 같은 `challenge_id`와 `split`
- 모든 입력 문항의 결정이 정확히 한 번씩 존재하는지 여부
- 문항 누락·중복·추가와 알 수 없는 모델 ID

실행·선택 결과 오류는 동일한 실행 입력으로 최초 실행을 포함해 최대 3회
실행하고, 세 번 모두 실패하면 해당 등급 점수를 `0`으로 처리합니다. 문항 ID·순서 변경
감사는 공식 실행 횟수에 포함하지 않으며, 참가자가 생략할 수 없습니다.
감사 차이는 자동 실격이 아니라 운영자 검토 대상으로 분류합니다.

## 로컬 검증

최종 이미지는 공개 Train/Dev 전체로 세 등급의 90초 한도를 미리 확인할 수
있습니다.

```console
PYTHONPATH=src python3 tools/check_runtime.py \
  --image my-router:check \
  --report build/runtime-check-report.json
```

검사 도구는 이미지를 로컬 content ID로 고정한 뒤 `linux/arm64`, CPU 2개,
메모리 2 GiB, 추가 스왑 없음, 프로세스·스레드 32개, 네트워크 없음과 읽기 전용
루트 파일 시스템 조건으로 Fast/Balanced/Premium을 각각 실행합니다. 입력은
materialization을 마친 공개 Train 1,760문항과 Dev 880문항입니다. 공개
모델별 outcome과 최종 평가 자료는 컨테이너에 전달하지 않습니다.

Docker 서버가 공식 `linux/arm64` 장비와 다르면 도구가 경고합니다. 로컬
검사는 출력 파일을 사후 검증하고, 공식 평가는 4 MiB 제한 tmpfs와 운영자
보안 검사를 추가로 적용하므로 최종 판정은 공식 환경의 실행 결과를 따릅니다.

표준 라이브러리 테스트는 항상 실행합니다.

```console
PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_*.py'
```

Docker 호환 CLI와 실행기가 동작하는 환경에서는 격리 빌드·실행 테스트를
추가로 켭니다. macOS에서는 Colima로 이 검사를 수행할 수 있으며 Docker
Desktop은 필수 의존성이 아닙니다.

```console
OSSP_RUN_CONTAINER_TESTS=1 \
  PYTHONPATH=src python3 -m unittest tests.test_runtime
```

이 검사는 같은 소스로 두 번 깨끗하게 빌드했을 때 런타임 파일의 경로·권한·
내용과 주요 설정이 같은지 확인합니다. 네트워크 없음, 읽기 전용 루트 파일
시스템, 비특권 사용자 조건에서 toy 입력이 처리되는지와 허용하지 않은 보조
출력이 거부되는지도 시험합니다. 일반적인 이미지 빌드는 계층 생성 시각
때문에 이미지 다이제스트까지 같다고 보장하지 않으므로, 실제 제출에는 평가할
이미지 다이제스트를 커밋 SHA와 함께 기록해야 합니다.
