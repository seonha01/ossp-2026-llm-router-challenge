# SPDX-FileCopyrightText: Copyright 2026 SK TELECOM CO., LTD.
# SPDX-License-Identifier: Apache-2.0

FROM python:3.11.15-alpine3.23@sha256:f73754c398b259dfbbe482361dca8b464dea57da74efe5214966ca2ee767ee12

# 실행 시 패키지를 설치하지 않으므로 빌드 전용 도구를 최종 이미지에서 제거합니다.
RUN python3 -m pip uninstall --yes pip setuptools wheel

ARG SOURCE_MANIFEST_SHA256=unbound

LABEL io.sktelecom.ossp.source-manifest-sha256="${SOURCE_MANIFEST_SHA256}"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/opt/router \
    PYTHONUNBUFFERED=1 \
    TMPDIR=/tmp

COPY --chown=65532:65532 src container/entrypoint.py /opt/router/
COPY --chown=65532:65532 \
    baselines/feature_budget.py \
    baselines/hash_regex.py \
    baselines/hash-regex-public.v1.json \
    /opt/router/baselines/

WORKDIR /opt/router

USER 65532:65532

ENTRYPOINT ["python3", "/opt/router/entrypoint.py"]
