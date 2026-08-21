<!--
SPDX-FileCopyrightText: Copyright 2026 seon_ha_01
SPDX-License-Identifier: Apache-2.0
-->

# 학습 파이프라인

공개 데이터에서 배포 아티팩트(`src/ossp_router/model/rg2_artifact_p1.npz`,
`rg2_artifact_p2.npz`)까지 전 과정을 재현하는 스크립트 모음입니다.

## 실행

```bash
# 선행: AIME 원문 결합 (네트워크 필요, 저장소에는 커밋하지 않음)
python3 -m venv .venv-data
.venv-data/bin/pip install -r data/sources/requirements-materialize-public-data.txt
.venv-data/bin/python tools/materialize_public_data.py

# 전체 파이프라인 (약 7분, 시드 고정으로 결정적)
python3 scripts/train_all.py
```

## 단계

| 순서 | 스크립트 | 내용 |
|---|---|---|
| 1 | `train_featurizer.py` | 단어/문자 TF-IDF 어휘와 SVD, 수치특징 스케일러 적합 |
| 2 | `phase2_cache.py` | 특징 행렬과 결과 배열 캐시 (`work/cache`, `OSSP_CACHE`로 변경 가능) |
| 3 | `train_heads.py` | 품질 마진 헤드(유사도 kNN + 희소 ridge) 교차검증 예측 |
| 4 | `phase3_cost.py` | 입력/출력 토큰 비용 모델(평균 + 분위수 격자) |
| 5 | `phase4_lambda.py` | 등급별 벌점 상수 보정: 부트스트랩 안전 검사 4종을 통과하는 가장 공격적인 상수 선택, dev 1회 평가 |
| 6 | `build_runtime_artifact.py` | numpy 전용 런타임 아티팩트로 내보내기 + 5중 검증 (train 전용 세트와 train+dev 확장 세트 두 벌) |

보조 스크립트: `analyze.py`(공식 채점기 대조와 기준선 재현),
`phase1_report.py`(특징 추출기 검증), `ablation.py`(구성요소 절제 실험).

검증은 `tests/test_model_router_determinism.py`(순서 셔플·ID 재라벨·부분
배치·중복 프롬프트 결정성)와 대회 제공 `tools/check_runtime.py`로 합니다.
