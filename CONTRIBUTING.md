<!--
SPDX-FileCopyrightText: Copyright 2026 SK TELECOM CO., LTD.
SPDX-License-Identifier: Apache-2.0
-->

# Contributing

## Contribution status

This repository does not accept external contributions. Unsolicited patches
and pull requests will not be reviewed or merged.

## Internal changes and DCO

Changes by SK Telecom employees must be made within the approvals applicable
to this project and follow the
[SK Telecom Open Source Contribution Rule](https://sktelecom.github.io/guide/contribute/rule/)
and
[contribution process](https://sktelecom.github.io/guide/contribute/process/).
Use an SK Telecom company email address for commit authorship and sign-off.

Every commit must certify the
[Developer Certificate of Origin 1.1](https://developercertificate.org/)
with the contributor's own sign-off:

```text
Signed-off-by: Your Name <your.company.email@example.com>
```

Use:

```console
git commit --signoff
```

Do not sign off for another person. This project uses DCO, not a Contributor
License Agreement. Changes accepted into the repository are licensed under
Apache-2.0 unless a file states otherwise.

## Clean-room and data boundary

Only independently authored material, or third-party material with verified
compatible redistribution rights, may be added. Do not add:

- code, Git history, documents, paths, or other artifacts copied from an
  internal repository or evaluation system;
- private evaluation composition, split mappings, outcomes, model outputs,
  gold answers, reasoning traces, failed generations, serving logs, or
  operational errors;
- credentials, internal hostnames, storage paths, or non-public URLs;
- dataset content that is not approved for redistribution, including
  `source-fetch-only` material; or
- third-party code, data, or documentation without its exact source, license
  evidence, attribution, and required notices.

Dataset changes must follow [`DATA_LICENSES.md`](DATA_LICENSES.md) and
[`data/sources/README.md`](data/sources/README.md). The repository's
Apache-2.0 license does not relicense datasets.

## Verification

Keep changes within the public scope described in
[`docs/DATA_CARD.md`](docs/DATA_CARD.md), use the repository's SPDX conventions,
preserve third-party notices, and run the checks in
[`DEVELOPING.md`](DEVELOPING.md) before committing.
