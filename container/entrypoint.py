# SPDX-FileCopyrightText: Copyright 2026 SK TELECOM CO., LTD.
# SPDX-License-Identifier: Apache-2.0

"""What the container runs. Routes one tier and writes its decisions."""

from __future__ import annotations

from ossp_router.model_router import main


if __name__ == "__main__":
    raise SystemExit(main())
