# SPDX-FileCopyrightText: Copyright 2026 SK TELECOM CO., LTD.
# SPDX-License-Identifier: Apache-2.0

"""Generate the deterministic all-light reference submission."""

from __future__ import annotations

import sys

from ossp_router.cli import main


if __name__ == "__main__":
    raise SystemExit(main(["always-light", *sys.argv[1:]]))
