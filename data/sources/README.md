<!--
SPDX-FileCopyrightText: Copyright 2026 SK TELECOM CO., LTD.
SPDX-License-Identifier: Apache-2.0
-->

# 출처 기록

[`source-pins.v1.json`](source-pins.v1.json)은 공개 Train/Dev 후보의 고정
리비전, 입력 파일 해시와 라이선스 근거 해시를 기록합니다. 이 기록은 공개
자료의 출처를 재현하기 위한 것이며 비공개 최종 평가의 구성이나 대응 관계를
설명하지 않습니다.

출처 기록을 추가하거나 바꿀 때는 추정값을 사용하지 말고 다음을 공개
근거로 확인해야 합니다.

- 정식 출처 URL과 변경되지 않는 상위 커밋 SHA
- 라이선스 근거 URL과 근거 파일 해시
- 입력 파일별 내용 해시(content hash)
- 결정적인 변환 절차와 결과 스키마
- 배포 방식과 릴리스에 포함할 수 있는 파일
- 필요한 귀속과 고지 문구

자료 출처명은 provenance 문서에만 두고 라우터 런타임 입력에는 넣지 않습니다.

고정한 공개 원천은 다음 명령으로 저장소 밖의 Git 비추적 캐시에 내려받고
SHA-256을 확인할 수 있습니다. AIME 원문은 이 캐시에만 두고 커밋하거나 릴리스
아카이브에 넣지 않습니다.

```console
python3 tools/fetch_public_sources.py \
  --source aime24-public \
  --source aime25-public
```

일반 참가자는 fetch와 전체 입력 결합을 함께 수행하는 다음 명령만 실행하면
됩니다. 이 자료 생성 절차에는 Python 3.10 이상이 필요합니다. `pyarrow`는
고정 AIME 2024 Parquet 파일을 읽는 데만 사용합니다. 라우터 패키지와 제출
컨테이너의 Python 요구사항은 이 자료 생성용 요구사항과 별개입니다.

```console
python3 -m venv .venv-data
.venv-data/bin/pip install -r data/sources/requirements-materialize-public-data.txt
.venv-data/bin/python tools/materialize_public_data.py
```

DeepMind Mathematics는 저장소에 upstream 코드를 복사하지 않습니다. 공개
선택은 [`deepmind-mathematics-selection.v1.json`](deepmind-mathematics-selection.v1.json)에
있으며, 별도로 고정한 checkout을 다음 재현 절차로 검증할 수 있습니다. 두 regime의
900개 reference hash가 모두 일치한 뒤에만 선택된 prompt fragment를 씁니다.

```console
python3 -m venv .venv-math
.venv-math/bin/pip install -r data/sources/requirements-deepmind-mathematics.txt
git clone https://github.com/google-deepmind/mathematics_dataset.git \
  data/cache/mathematics_dataset
git -C data/cache/mathematics_dataset checkout \
  427f45075f84b8b9774950196ad63867ca20ffb3
.venv-math/bin/python tools/reproduce_deepmind_mathematics.py \
  --source-dir data/cache/mathematics_dataset
```
