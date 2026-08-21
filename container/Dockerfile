# SPDX-FileCopyrightText: Copyright 2026 SK TELECOM CO., LTD.
# SPDX-License-Identifier: Apache-2.0
#
# Image for the prompt-only router.
#
# Build it for the platform the judges use with:
#   docker buildx build --platform linux/arm64 -f container/Dockerfile -t <ref> --load .
#
# Nothing is fetched at run time. The dependencies and the fitted model are all
# baked in. The only thing added on top of Python is numpy (BSD-3-Clause);
# training-time libraries (scikit-learn, scipy) are not shipped — the boosted
# trees are exported to plain arrays and evaluated in numpy.

FROM python:3.11-slim-bookworm

# Version pinned so the exported arrays load against the version they were
# saved with.
RUN pip install --no-cache-dir \
        numpy==1.26.4 \
    && pip uninstall --yes pip setuptools wheel || true

ARG SOURCE_MANIFEST_SHA256=unbound
LABEL io.sktelecom.ossp.source-manifest-sha256="${SOURCE_MANIFEST_SHA256}"

# one BLAS thread per process: the router forks two workers, so two
# single-threaded processes exactly fill the 2-CPU quota without thrashing
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/opt/router \
    PYTHONUNBUFFERED=1 \
    OMP_NUM_THREADS=1 \
    OPENBLAS_NUM_THREADS=1 \
    TMPDIR=/tmp

COPY --chown=65532:65532 src container/entrypoint.py /opt/router/

WORKDIR /opt/router

USER 65532:65532

ENTRYPOINT ["python3", "/opt/router/entrypoint.py"]
