<!--
SPDX-FileCopyrightText: Copyright 2026 SK TELECOM CO., LTD.
SPDX-License-Identifier: Apache-2.0
-->

# 평가 운영 절차

이 문서는 평가 운영자가 공개 실행기를 안전하게 운용하고 결과를 감사하기
위한 구현·복구 절차입니다. 참가자가 구현해야 하는 컨테이너 인터페이스와
격리·자원 조건은 [`RUNTIME.md`](RUNTIME.md), 참가자에게 적용하는 실패·실격
판정은 [`ENFORCEMENT.md`](ENFORCEMENT.md)가 권위 있는 문서입니다.

## 공식 등급 실행

공식 실행 경로는 `router-evaluate-tier`입니다. 다음 값은 형식 예시이며 현재
저장소에는 평가 커밋이나 제출 이미지가 없습니다.

```console
router-evaluate-tier \
  --runtime docker \
  --image registry.example/challenge/router@sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef \
  --commit-sha 0123456789abcdef0123456789abcdef01234567 \
  --input /operator/input/inputs.json \
  --tier fast \
  --output-directory /operator/runs/fast \
  --private-work-directory /operator/private/fast \
  --image-size-evidence /operator/image-evidence/linux-arm64.json \
  --record /operator/runs/fast/execution-record.json
```

명령행 종료 코드는 운영 스케줄러의 제어 신호입니다. 채점, 등급 0점, 실격과
수동 검토의 세부 판정은 구조화된 실행 기록의 `status`를 기준으로 합니다.

| 종료 코드 | 운영 의미 | 스케줄러 처리 |
| ---: | --- | --- |
| `0` | 자동 처리 가능한 종결 상태 | 기록의 `valid`, `tier_score_zero`, `disqualified`에 따라 처리 |
| `2` | 운영 입력 또는 설정 오류 | 설정을 고친 뒤 새 작업으로 실행 |
| `4` | 실행기 또는 감사의 인프라 보류 | 운영 장애를 복구한 뒤 재실행 |
| `5` | `review_required` 또는 감사 결과 누락 | 자동 채점과 자동 재시도를 중단하고 수동 검토 |
| `6` | 이미지 사전검증 탈락 | 참가자 컨테이너를 실행하거나 공식 시도 횟수를 소비하지 않고 접수 결과로 기록 |

종료 코드만으로 점수나 실격 여부를 정하지 않습니다. 특히 코드 `5`는 참가자
실행 실패 횟수에 포함하거나 자동 실격으로 처리하는 값이 아닙니다.

## 운영 경로와 입력 스냅샷

`--output-directory`는 운영자가 소유하고 권한이 정확히 `0700`인 실제
디렉터리여야 합니다. 실행기는 심볼릭 링크로 만든 별칭을 실제 경로로 한 번
해소한 뒤 그 경로만 사용합니다. 파일 시스템 최상위 디렉터리부터 대상
디렉터리까지 소유자는 `root` 또는 현재 운영자여야 합니다. 그룹이나 다른
사용자에게 쓰기를 허용한 상위 디렉터리는 sticky bit가 있어야 합니다.
따라서 `root` 소유 `01777` `/private/tmp`는 허용하지만 sticky bit가 없는
공유 경로는 실행 전에 거부합니다. macOS의 `/var`처럼 시스템이 제공하는
별칭은 `/private/var`의 실제 상위 경로를 검사합니다.

실행기가 만드는 `official/`과 `audit/` 하위 디렉터리도 같은 소유권과
권한을 사용합니다. `--record`는 실제 출력 루트 바로 아래의 JSON 파일로
고정합니다. `--private-work-directory`는 결과 출력 경로와 겹치지 않는
등급별 전용 디렉터리여야 합니다. 운영자가 소유하고 권한이 정확히 `0700`이며,
실행 시작 전 비어 있어야 합니다. 강제 종료로 파일이 남아 있으면 새 평가를
시작하지 않고 운영자가 비공개 영역에서 확인·정리합니다.

공식 실행기는 출력 루트 잠금을 얻은 뒤 원본 입력을 이 비공개 작업 경로에
한 번만 고정합니다. 스냅샷은 권한 `0600`의 임시 파일로 완성한 뒤
컨테이너의 비특권 UID가 읽을 수 있도록 파일 자체만 `0444`로 게시합니다.
권한 `0700`인 부모 디렉터리 때문에 호스트의 다른 사용자는 접근할 수 없고,
컨테이너에는 이 파일을 읽기 전용으로만 바인드합니다.

복사한 파일을 v1 입력 형식으로 다시 읽어 호출 시 전달한 입력 객체와
일치하는지 확인합니다. 모든 공식 재시도는 이 파일만 읽기 전용으로
마운트하고 실행 기록의 입력 SHA-256도 같은 바이트에서 계산합니다. 원본
경로가 실행 중 교체돼도 재시도별 입력과 기록 해시는 달라지지 않습니다.

ID·순서 변경 감사 입력도 같은 비공개 작업공간에서 `0600` 임시 파일로 원자
작성한 뒤 읽기 전용 바인드를 위해 `0444`로 게시합니다. 공식 실행과 감사가
끝나면 구조화된 기록을 게시하기 전에 작업공간을 삭제합니다. 결과 트리의
`audit/`에는 프롬프트 본문을 담은 입력 파일을 보존하지 않으며, 기록에는
감사 입력의 SHA-256과 원본·감사 ID 대응만 남깁니다.

각 제출·등급에는 재사용하지 않는 고유 출력 디렉터리를 배정합니다. 실행
기록은 그 디렉터리 바로 아래의 `.json` 파일이어야 합니다. 공식 실행과 감사
전체의 루트 잠금을 보유한 상태에서 임시 파일 `fsync`와 원자적 교체로
게시합니다. 잠금을 얻지 못한 경쟁 프로세스는 기존 실행 기록을 덮지 않습니다.

## 이미지 식별과 크기 증거

변경 가능한 태그와 짧거나 대문자인 다이제스트는 실행 전에 거부합니다.
운영자는 제출한 repository digest를 `docker image inspect`로 한 번 확인하고,
`RepoDigests`의 대응 관계, 로컬 content-addressed image ID와
운영체제·아키텍처를 기록합니다. 관측된 `.Id`는 Docker 저장 방식에 따라
image config digest 또는 선택한 플랫폼 manifest digest일 수 있으므로 두
경우를 모두 증거와 대조합니다.

모든 공식 재시도와 감사 재실행은 제출한 repository digest에
`--platform linux/arm64 --pull never`를 적용합니다. 로컬 통합 테스트에서만
레지스트리 다이제스트가 없는 전체 `sha256:<64자리 image ID>`를 하위
실행기에 직접 전달합니다. 검사 명령은 필요한 필드만 요청하고 stdout
1 MiB, stderr 64 KiB의 스트리밍 한도를 적용합니다.

이미지가 선언한 `VOLUME`은 명시한 출력·임시 공간 밖에 쓰기 가능한 익명
볼륨을 만들 수 있으므로 경로와 관계없이 거부합니다. 이 검사는 공식 이미지
확인과 매 시도 하위 실행 경계에 모두 적용합니다. 검사 응답에는 참가자가
늘릴 수 있는 전체 `VOLUME` 경로나 컨테이너 런타임 오류 문자열을 싣지 않고
존재 여부만 불리언으로 요청합니다.

Docker의 로컬 이미지 검사 API가 보고하는 `Size`는 선택한 OCI
매니페스트의 압축 계층 합계나 병합된 rootfs의 겉보기 크기가 아닙니다.
공식 실행기는 이 값을 대신 사용하지 않습니다. 운영자는 참가자 컨테이너를
실행하기 전, 네트워크 접근이 허용된 통제된 전처리 단계에서 제출 이미지를
OCI layout으로 고정합니다.

`router-measure-image`는 layout의 index와 `linux/arm64` 매니페스트 원문을
다이제스트로 검증하고 계층 descriptor의 `size` 합계를 계산합니다. 제출
repository digest와 플랫폼을 고정해 만든 컨테이너를 `docker export`하고,
tar 헤더의 일반 파일 크기를 합산해 병합 rootfs의 겉보기 크기를 측정합니다.
매니페스트 응답과 export 스트림에는 운영자 상한을 적용합니다. tar를
디스크에 풀지 않고 순차 검사하며 최종 한도 초과가 확인되면 즉시 중단합니다.

```console
router-measure-image \
  --runtime docker \
  --oci-layout /operator/oci-layout/submission \
  --image registry.example/challenge/router@sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef \
  --output /operator/image-evidence/linux-arm64.json
```

결과는 `operator-image-size-evidence` v1 JSON으로 기록합니다. 제출 repository
digest, 선택한 플랫폼 매니페스트 digest, config digest, `linux`/`arm64`, 두
바이트 측정값과 고정된 측정 방식
`oci-manifest-layer-descriptors-v1`,
`docker-export-tar-apparent-size-v1`을 포함합니다.

`--image-size-evidence` 경로의 상위 디렉터리는 운영자 소유 `0700`
디렉터리여야 하고 위에서 정의한 실제 상위 경로 검사를 적용합니다. 증거는
운영자 소유 `0600`, 하드 링크가 없는 16 KiB 이하 일반 파일이어야 합니다.
심볼릭 링크, 중복 JSON 키, 추가·누락 필드, 대응하지 않는 관측
ID·플랫폼·측정 방식 또는 증거 누락은 실행 전에 운영자 인프라 장애로
처리합니다.

측정값이 최종 한도를 넘으면 `submission_preflight_rejected`로 기록하고 종료
코드 `6`을 반환합니다. 선언된 `VOLUME`도 종료 코드 `6`으로 처리하되 기록의
`reason_code`는 `declared_volume_not_allowed`로 구분합니다. 두 경우 모두
참가자 컨테이너를 시작하거나 공식 시도 횟수를 소비하지 않습니다.

측정용 `docker create` 전에 증거 출력 경로에 운영자 소유 `0600` 자원
저널을 게시합니다. 생성 결과가 불확실하면 고유 이름의 컨테이너가 곧바로
보이지 않더라도 저널을 자동 삭제하지 않습니다. 다음 측정은 저널이 있는 동안
새 Docker 자원을 만들지 않습니다. 운영자는 이름·라벨·Docker 실행
명령·context·Engine ID를 대조해 잔여 컨테이너를 정리하거나, Docker 데몬
재시작 뒤 부재를 확인해야 합니다.

```console
router-measure-image \
  --runtime docker \
  --output /operator/image-evidence/linux-arm64.json \
  --cleanup-pending
```

불확실한 생성 요청 뒤 컨테이너가 관측되지 않는 경우에는 Docker 데몬을
재시작해 지연 요청이 남지 않았음을 확인한 뒤에만
`--confirm-daemon-restarted`를 함께 사용합니다.

## 출력 볼륨과 도우미

시도별 출력 경계는 다음과 같습니다.

1. 호스트 결과 경로의 이전 `submission.json`을 제거해 실패한 재실행이 과거
   유효 결과를 남기지 않게 합니다.
2. `size=4m`, `nr_inodes=64`, `noexec`, `nosuid`, `nodev`인 tmpfs 기반
   Docker 볼륨을 실행 식별 라벨과 함께 만듭니다.
3. 고정 다이제스트 기반의 운영자 도우미와 참가자 컨테이너만 이 볼륨을
   공유합니다. 참가자에게 호스트 결과 디렉터리는 보이지 않습니다.
4. 참가자 종료 후 도우미가 루트 항목이 `submission.json` 하나뿐이고
   심볼릭 링크·하드 링크·FIFO가 아닌 일반 파일인지 확인합니다.
5. 최대 4 MiB만 읽어 운영자 전용 시도 디렉터리로 옮긴 뒤 공개 v1 형식을
   검증합니다. 신뢰 도우미 스크립트는 권한 `0700` 상태에서 먼저 복사합니다.
   그 뒤 디렉터리를 sticky bit가 있는 `01733`으로 바꿔 목록 조회는 막고
   도우미 UID의 결과 쓰기만 허용합니다.
6. 참가자 컨테이너, 도우미와 볼륨의 실행 식별 라벨이 해당 시도와 일치할
   때만 제거하고 검증된 결과를 게시합니다.

참가자 프로세스와 운영자 도우미에는 `--ulimit core=0:0`을 적용합니다.
Linux는 `core_pattern`이 `|`로 시작하는 파이프 수집기일 때 이 한도를
적용하지 않습니다. 따라서 정리 저널을 먼저 기록하고 운영자 도우미를 시작한
뒤, 도우미에서 Docker 데몬 커널의 `/proc/sys/kernel/core_pattern`을
검사합니다. 파이프 수집기가 설정된 호스트에서는 참가자 컨테이너를 시작하지
않고 운영자 인프라 장애로 평가를 보류합니다.

macOS에서 Colima를 사용할 때 운영체제가 보호하는 폴더의 바인드 마운트가
거절될 수 있습니다. 입력과 출력 디렉터리를 홈 바로 아래 Colima가 공유할 수
있는 임시 디렉터리에 두고 절대 경로를 지정합니다. 컨테이너 내부 경로와 공개
실행 형식은 바꾸지 않습니다. tmpfs 옵션을 사용하는 Docker `local` 볼륨을
지원하지 않는 rootless 실행기나 Windows 컨테이너는 공식 실행 환경이
아닙니다.

## 시간 초과와 상태 확인

운영자 실행기는 등급별 실행 시간 한도가 지나면 호스트 프로세스 그룹을
종료합니다. 참가자 컨테이너에는 고유 이름과 실행 식별 라벨을 붙입니다.
제한 시간이나 로그 출력량을 넘긴 뒤에도 이름으로 컨테이너를 찾고, 라벨과
ID가 해당 시도와 일치할 때만 `docker stop --timeout 5`와
`docker rm --force`를 호출합니다. 생성 명령 직후 자원이 아직 보이지 않는
경합을 고려해 0.1·0.3·0.6초 간격으로 다시 확인합니다.

`docker run`이 끝나면 제한된 형식으로 `State`, 실행 식별 라벨과 ID를 다시
읽어 `StartedAt`, `State.Error` 존재 여부, OOM 여부와 실제 종료 코드를
확인합니다. 시작되지 않았거나 런타임 오류가 남은 종료 코드 `125`는 운영자
인프라 장애이고, 실제로 시작·종료한 참가자 프로세스의 `125`는 실행
실패입니다. Docker 규약의 `126`, `127`도 참가자 이미지의 명령 실행
실패입니다.

## 늦게 생성되는 Docker 자원 복구

Docker 명령행 프로세스를 시간 초과로 종료해도 데몬에 전달된 생성 요청이
나중에 완료될 수 있습니다. 운영 실행기는 첫 Docker 변경 명령 전에 출력
디렉터리에 권한 `0600`의 영속 정리 저널을 원자적으로 기록하고 파일과
디렉터리를 `fsync`합니다. 저널에는 무작위 실행 식별값, 참가자 컨테이너,
운영자 도우미와 출력 볼륨의 이름·역할, Docker 실행 명령·context와 Engine
ID를 기록합니다. 프롬프트, 제출 내용과 로그는 기록하지 않습니다.

Engine ID는 첫 Docker 변경 요청 전, 참가자·도우미·볼륨 정리 때와 최종 저널
삭제 직전에 확인합니다. 실행 중 ID가 달라지면 제거를 중단하고 저널을
보존합니다. 복구할 때 실행 명령·context와 Engine ID가 모두 일치해야 하며,
같은 context 이름이 다른 데몬 endpoint를 가리키면 어떤 자원도 검사하거나
제거하지 않습니다. 같은 출력 디렉터리는 별도 잠금 파일로 직렬화합니다.
단순 잠금 경합은 `cleanup_pending`으로 표시하지 않습니다.

생성 여부나 제거 완료 여부가 불확실하면 저널을 남기고 결과에
`cleanup_pending`을 표시합니다. 같은 등급의 공식 재시도와 감사 재실행을
즉시 중단하고, 해당 실행 장비와 출력 루트를 복구가 끝날 때까지 다른 작업에
사용하지 않습니다. 복구 도구는 저널의 정확한 이름과 실행 식별 라벨이 모두
일치할 때만 참가자 컨테이너, 도우미, 볼륨 순서로 제거합니다. 다른 라벨과
충돌하면 어떤 자원도 추측해 제거하지 않습니다.

불확실한 생성 요청 뒤 자원이 계속 보이지 않는다는 이유만으로 저널을
삭제하지 않습니다. 실제로 나타난 자원을 제거했거나 Docker 데몬을 재시작해
이전 요청이 더는 완료될 수 없음을 확인한 뒤 다음 명령을 사용합니다.

```console
router-cleanup-resources \
  --output-directory /operator/runs/fast/official \
  --docker-context colima \
  --confirm-daemon-restarted

router-cleanup-resources \
  --output-directory /operator/runs/fast/audit \
  --docker-context colima \
  --confirm-daemon-restarted
```

데몬 재시작 확인 없이 실행해도 이미 나타난 동일 라벨 자원은 안전하게
제거합니다. 한 번도 관찰되지 않은 역할이 남아 있으면 종료 코드 `4`로 복구
보류를 유지합니다. `--docker-context`는 저널을 만들 때 사용한 context와
같아야 합니다. 정상 실행에서는 자원 제거를 확인한 뒤 저널을 삭제합니다.

새 실행은 Docker 자원을 변경하기 전에 `official/`과 `audit/` 양쪽의 잔여
저널을 확인합니다. 잔여 저널이 있으면 그 저널의 무작위 시도 ID와 실행
기록의 `cleanup_attempt_id`를 비교합니다. 같은 시도의 `cleanup_pending`
상세 기록만 보존합니다. 이전 `valid` 기록, 다른 시도의 기록 또는 ID를
확인할 수 없는 기록은 `operator_cleanup_pending`,
`cleanup_pending: true`와 현재 시도 ID만 담은 최소 기록으로 원자 교체합니다.
따라서 오래된 성공 기록이 현재 정리 보류 상태를 가릴 수 없습니다.

## 감사와 구조화된 기록

공개 오케스트레이터는 문항이 둘 이상인 배치에서 비밀값으로 원본 ID 집합을
재배정합니다. 어떤 문항도 원래 ID를 그대로 유지하지 않게 배정하고 순서를
별도로 섞습니다. 감사 전용 접두사나 새 ID 형식은 만들지 않습니다. 운영자
전용 원본·감사 ID 대응표를 기준으로 각 문항의 선택을 비교하며, 같은
프롬프트가 여러 번 있어도 발생 순서를 덮어쓰지 않습니다.

공식 `router-evaluate-tier` 명령은 ID·순서 변경 감사를 생략하는 옵션을
제공하지 않습니다. 내부 API를 시험 목적으로 감사 없이 호출한 기록은
`valid`가 아니라 `audit_skipped`이며 공식 채점에 사용할 수 없습니다.

선택 차이나 감사 실행 오류는 `review_required`, 운영자 장애로 감사를 완료하지
못한 경우는 `audit_inconclusive_infrastructure`로 기록합니다. 전자는 종료 코드
`5`로 자동 채점과 자동 재시도를 중단하고 수동 검토하며, 후자는 종료 코드 `4`로
운영 장애를 복구한 뒤 같은 조건에서 다시 실행합니다.

[`src/ossp_router/runtime.py`](../src/ossp_router/runtime.py)는 실행·형식 오류,
운영자 인프라 장애, 공정성 위반과 유효 결과를 서로 다른 상태로 표현합니다.
[`src/ossp_router/orchestrator.py`](../src/ossp_router/orchestrator.py)는 제출
repository digest를 플랫폼별 증거와 확인한 뒤 같은 digest와 명시적
플랫폼으로 공식 재시도와 감사를 수행합니다. 각 등급 기록에는 다음을
남깁니다.

- 평가할 커밋 SHA와 컨테이너 이미지 다이제스트
- 선택한 플랫폼 매니페스트 digest, 압축 계층 합계, rootfs 겉보기 크기와
  운영자 증거 파일 SHA-256
- 등급, 입력 식별 정보와 확정된 평가 정책 ID
- 공식 실행 번호와 별도로 센 운영 장애 횟수
- 종료 코드, 제한 시간과 보관·UTF-8 정규화한 stdout·stderr 텍스트의
  바이트 수·SHA-256
- 출력 파일 크기, 형식 검증 결과와 첫 유효 결과
- 감사 재실행 여부와 실격 판단 근거
- 감사 입력의 SHA-256과 문항 발생별 원본·감사 ID 대응표
- 정리 보류가 발생한 시도의 `cleanup_attempt_id`

표준 출력·오류는 실행 중 각각 256 KiB 이하만 메모리에 보관하며, 각
스트림의 총 출력량이 1 MiB를 넘으면 실행 실패로 종료합니다. 구조화된 공식
기록에는 보관하고 UTF-8로 정규화한 로그 텍스트의 바이트 수와 SHA-256만
남기며 원문은 넣지 않습니다. 이 값은 원시 로그 바이트의 다이제스트가
아닙니다. 기록에는 프롬프트나 모델 답변 본문을 포함하지 않습니다. 참가자가
제어하는 모델 ID, 파일명 또는 오류 문자열이 섞일 수 있는 상세 설명도
직렬화하지 않고 운영자가 정의한 고정 `reason_code`만 남깁니다.

## 운영 검증

표준 라이브러리 테스트와 Docker 통합 테스트 명령은
[`RUNTIME.md`](RUNTIME.md)의 로컬 검증 절차를 따릅니다. 공식 Apple Silicon
장비에서는 [`APPLE_SILICON_MEASUREMENT.md`](APPLE_SILICON_MEASUREMENT.md)의
사전 조건, cgroup v2 확인과 공개 Train/Dev 전체 측정을 추가로 수행합니다.
