<!--
SPDX-FileCopyrightText: Copyright 2026 SK TELECOM CO., LTD.
SPDX-License-Identifier: Apache-2.0
-->

# 기반 이미지 기록

제공하는 기준 컨테이너 예시는 Docker Official Image인
`python:3.11.15-alpine3.23`을 사용합니다.

- 다중 플랫폼 인덱스 다이제스트:
  `sha256:f73754c398b259dfbbe482361dca8b464dea57da74efe5214966ca2ee767ee12`
- 공식 선택 플랫폼: `linux/arm64`
- [Docker Official Images의 Python 항목](https://github.com/docker-library/official-images/blob/master/library/python)
- [고정한 빌드 조리법](https://github.com/docker-library/python/blob/4d216ad3beb5b697c4049071c82fc375acb8abad/3.11/alpine3.23/Dockerfile)

공식 이미지 항목은 이 태그가 여러 플랫폼을 지원하고 조리법이
`docker-library/python`의 커밋
`4d216ad3beb5b697c4049071c82fc375acb8abad`, 디렉터리
`3.11/alpine3.23`에서 왔음을 기록합니다.

이 저장소의 Apache-2.0 라이선스는 기반 이미지 안의 Python, Alpine과 개별
패키지를 재라이선스하지 않습니다. 이미지를 배포할 때는 이미지 안의
Python 저작권·라이선스와 APK 패키지 메타데이터의 고지 조건을 그대로
보존하고 별도로 검사해야 합니다. 이미지에 소형 분류기나 추가 패키지를
넣는 경우에도 각 구성 요소의 정확한 출처, 고정 버전, 라이선스와 고지를
추가로 기록해야 합니다.

다이제스트 고정은 자동 보안 갱신을 막으므로 공개 이미지 배포 전에는 해당
다이제스트의 운영체제·Python 패키지 취약점과 소프트웨어 자재 명세서(SBOM)를
다시 검사해야 합니다. 보안 갱신으로 기반 이미지 다이제스트를 바꾸는 경우
새 이미지의 출처·라이선스·취약점·재현 빌드를 함께 검증합니다. 이 변경은
v1 JSON 형식이나 평가 정책 ID를 바꾸지 않지만 제출 이미지 다이제스트는
새로 기록해야 합니다.

공식 실행기가 제한 출력 볼륨에서 결과를 꺼낼 때만 사용하는 운영자 도우미는
별도의 Docker Official Image
`python:3.14.6-alpine3.23@sha256:b165067c5afc37fa5608a3c05609cc3d51aafd808a30fbfd822ee594fef55ad4`로
고정합니다. 이 이미지는 참가자 라우터 이미지의 기반이나 제출 요건이 아닙니다.
