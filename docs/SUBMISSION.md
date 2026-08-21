<!--
SPDX-FileCopyrightText: Copyright 2026 SK TELECOM CO., LTD.
SPDX-License-Identifier: Apache-2.0
-->

# 제출 안내

## 제출 흐름

1. 이 저장소를 참가 팀의 GitHub 계정이나 조직으로 fork합니다.
2. 참가 fork에서 라우터를 구현하고 로컬 점검을 마칩니다.
3. 최종 코드 커밋을 공개하고 그 커밋에서 `linux/arm64` 이미지를 빌드합니다.
4. 이미지를 공개 레지스트리에 push하고 전체 다이제스트를 확인합니다.
5. 저장소 루트에 `submission-ossp-skt.json`을 작성하고 검증한 뒤 별도
   커밋합니다.
6. JSON을 포함한 커밋의 고정된 GitHub 스냅샷 URL을 결과보고서의 `프로젝트
   등록 URL`에 기재합니다.
7. 공식 결과보고서 원본 파일과 PDF를 대회 사이트에 업로드합니다.

로컬 clone 등 개발 방법과 브랜치 이름은 자유입니다. 최종 제출 시점부터 평가가
끝날 때까지는 평가할 fork와 커밋을 별도 권한 없이 열 수 있어야 합니다.
브랜치 이름이나 움직일 수 있는 기본 브랜치 URL이 아니라 결과보고서에 적은
전체 커밋 SHA의 스냅샷 URL과 JSON의 `commit_sha`가 제출 대상을 고정합니다.

심사 전에는 원본 과제 저장소로 pull request(PR)를 보낼 필요가 없습니다.
심사가 끝난 뒤 수상작에는 공개 예제로 소개할 수 있도록 별도의 PR을 요청할 수
있으며, 이 PR은 공식 제출이나 심사 조건이 아닙니다.

## 최종 제출 체크리스트

- 공개 fork에서 제출 커밋을 별도 권한 없이 열 수 있습니다.
- 심사에 필요한 전체 소스코드가 제출 커밋에 포함되어 있습니다.
- 같은 커밋에서 빌드한 `linux/arm64` 이미지가 공개 레지스트리에 있습니다.
- 이미지 참조는 태그가 아니라 `@sha256:...` 전체 다이제스트입니다.
- 공개 Train+Dev 검사에서 세 등급의 실행 시간과 출력 형식을 확인했습니다.
- 저장소와 이미지에 포함한 파일의 라이선스 근거가 공개되어 있습니다.
- 저장소 루트의 `submission-ossp-skt.json`이 스키마 검증을 통과하고 최종
  제출 커밋에 포함되어 있습니다.
- 결과보고서의 `프로젝트 등록 URL`이 JSON을 포함한 정확한 커밋 스냅샷을
  가리킵니다.

## 라우터 선택 결과 확인

라우터는 로컬 실행 시 등급별 선택 결과 JSON을 만듭니다.

```text
submission/
├── fast.json
├── balanced.json
└── premium.json
```

각 파일의 `decisions`에는 실행 입력의 모든 `episode_id`와 선택한 `model_id`가
정확히 한 번씩 있어야 합니다. 전체 형식은
[`submission.v1.schema.json`](../schemas/submission.v1.schema.json)에
정의합니다.

toy 자료로 세 파일의 형식, 문항 범위, 모델 ID, 예산과 점수를 확인할 수
있습니다.

```console
PYTHONPATH=src python3 -m ossp_router.cli self-check \
  --input data/toy/inputs.json \
  --outcomes data/toy/outcomes.json \
  --submissions build/toy-submission \
  --report build/toy-report.json
```

이 등급별 JSON은 로컬 점검과 공식 평가 실행 중 생성되는 결과이며, 대회
사이트에 직접 제출하는 기술 정보 파일과 다릅니다.

## 컨테이너 이미지

컨테이너 실행 명령과 경로는 [`RUNTIME.md`](RUNTIME.md)를 따릅니다. 이미지는
`registry/repository@sha256:<64자리 소문자 16진수>` 형태로 제출합니다.
`latest` 같은 태그만 있는 참조와 축약 다이제스트는 받지 않습니다.

예를 들어 Buildx에서는 공식 플랫폼을 다음처럼 지정할 수 있습니다.

```console
docker buildx build --platform linux/arm64 --push \
  --file container/Dockerfile \
  --tag registry.example.com/team/router:submission .
```

사전, 토크나이저, 학습한 분류기나 소형 언어 처리 모델을 포함했다면 저장소에
다음 정보를 기록합니다.

- 이름, 용도와 공개 업스트림 URL
- 고정한 버전 또는 리비전과 포함 파일의 SHA-256
- 라이선스 근거와 필요한 NOTICE·저작권 고지
- 직접 변환했다면 원본과 결과의 SHA-256, 변환 도구·옵션

AI 모델을 사용한다면 최소한 가중치를 공개한 모델이어야 합니다. 파인튜닝한
모델이나 직접 개발한 모델은 원본 라이선스가 허용하는 범위에서 별도 승인 없이
받을 수 있는 공개 위치에 가중치와 실행에 필요한 코드를 게시해야 합니다.

실행 중 다운로드하거나 비공개·접근 제한 파일에 의존할 수 없습니다.
허용 라이선스는 [`CHALLENGE_RULES.md`](CHALLENGE_RULES.md)를 따릅니다.

이미지를 push하기 전에는 공개 Train/Dev 전체로 공식 자원 한도에 가까운
로컬 검사를 실행하는 것을 권장합니다. 사용법과 로컬·공식 환경의 차이는
[`RUNTIME.md`](RUNTIME.md#로컬-검증)를 참고하십시오.

## 기술 제출 정보 파일

SKT 평가에 사용하는 기술 제출 정보 파일의 이름은 `submission-ossp-skt.json`
입니다. 이 파일은 제출 저장소의 루트에 반드시 커밋해야 하며 다음 여섯 필드만
허용합니다.

```json
{
  "schema_version": 1,
  "challenge_id": "ossp-2026-llm-router-challenge",
  "repository_url": "https://github.com/example/router",
  "commit_sha": "0123456789abcdef0123456789abcdef01234567",
  "image_digest": "registry.example.com/team/router@sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
  "primary_license": "Apache-2.0"
}
```

`commit_sha`는 이미지 빌드에 사용한 최종 코드 커밋의 전체 40자리 소문자
16진수입니다. `image_digest`는 저장소 이름과 전체 이미지 다이제스트를
포함합니다. `repository_url`에는 커밋 경로가 없는 공개 저장소 기본 URL을,
`primary_license`에는 주된 코드 라이선스를 기록합니다. 정확한 형식은
[`technical-submission.v1.schema.json`](../schemas/technical-submission.v1.schema.json)에
정의합니다.

JSON에는 이미 확정된 코드 커밋 SHA와 그 커밋에서 빌드한 이미지 다이제스트를
적습니다. 먼저 코드 커밋에서 이미지를 빌드하고 다이제스트를 확인한 뒤, JSON만
추가한 별도 커밋을 만드십시오. 결과보고서에는 JSON을 포함한 뒤쪽 커밋의
`https://github.com/<계정>/<저장소>/tree/<전체-커밋-SHA>` 형태 스냅샷 URL을
적습니다. 평가 코드는 그 URL의 JSON을 읽고, JSON이 가리키는 앞쪽 코드 커밋과
이미지를 사용합니다. JSON을 추가한 뒤 이미지를 다시 빌드할 필요는 없습니다.

작성한 파일은 다음 명령으로 검증할 수 있습니다. `--file`을 생략하면 현재
위치와 관계없이 검증기 파일이 속한 저장소 루트의 `submission-ossp-skt.json`을
검사합니다.

```console
python3 tools/validate_technical_submission.py
```

## 일정과 접수

출품작 제출 기간은 2026년 7월 18일부터 8월 27일 18:00(대한민국 표준시)까지
이며, [공식 대회 접수 사이트](https://osscontest.kr/)의 출품작 제출 절차를
따릅니다. 공식 양식으로 작성한 한글 또는 Word 원본 파일 1개와 같은 내용의
PDF 1개를 업로드합니다. 결과보고서 본문은 양식 안내에 따라 5페이지 이내로
작성하고, 첫 쪽의 작성 안내와 회색 안내 문구는 제출 전에 삭제합니다.
파일 이름은 양식에 안내된
`2026 오픈소스 개발자대회 결과보고서_접수번호(팀명)`을 사용합니다.

결과보고서의 `프로젝트 등록 URL`에는 위에서 설명한 정확한 커밋 스냅샷 URL을
적습니다. `submission-ossp-skt.json`은 대회 사이트에 별도로 업로드하지
않으며, 해당 URL이 가리키는 저장소 루트에서 확인합니다. 결과보고서에 요구된
SBOM, AI 모델 활용·라이선스 정보와 데모 영상 URL도 사용하는 구성에 맞게
작성합니다. 실행 이미지에 AI 모델을 포함하지 않은 라우터는 AI 모델 항목에
`해당 없음 — 실행 이미지에 AI 모델을 탑재하지 않음`이라고 밝히고, 코드 작성에
AI 도구를 사용했다면 양식의 AI 코딩 도구 항목에 사용 범위를 기록합니다.

마감 전에는 결과보고서를 복수로 제출하거나 자유롭게 다시 업로드할 수 있으며
마지막으로 접수된 파일을 심사합니다. 8월 27일 18:00 이후에는 새 제출과 수정이
모두 차단됩니다.

공개 저장소의 정확한 스냅샷 URL과 변경 불가능한 이미지 다이제스트가 모두
있어야 유효합니다. 접수 완료 여부와 최종 접수 시각은 공식 사이트의 확인
기록을 기준으로 합니다. 실시간 비공개 순위표나 중간 점수 확인은 제공하지
않습니다.

수상팀은 수상일로부터 5년 동안 제출 저장소를 공개 상태로 유지해야 합니다.

공개 질문과 하네스 오류는 GitHub Issues에서 받습니다. 출품 파일이나 민감한
내용은 공개 이슈에 첨부하지 마십시오.
