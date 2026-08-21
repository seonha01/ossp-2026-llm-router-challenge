<!--
SPDX-FileCopyrightText: Copyright 2026 SK TELECOM CO., LTD.
SPDX-License-Identifier: Apache-2.0
-->

# Data licenses and distribution policy

## Current status

The repository distributes approved redistributable public Train/Dev prompts,
source-fetch-only selection records, and content-free outcomes. It does not
distribute AIME problem text, source archives, generations, model answers,
gold answers, or private evaluation material. Exact public counts and hashes
are recorded in [`data/public-data.v1.json`](data/public-data.v1.json), and
required attribution and modification notices are in
[`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md).

The root [`LICENSE`](LICENSE) covers original project code and documentation.
It does not relicense benchmark data. Each data source retains its own
copyright, license, attribution, notice, and distribution conditions.

Public Train/Dev material is eligible only when its approved distribution or
source-fetch mode permits commercial use and all attribution, notice,
share-alike, and other applicable conditions can be satisfied. Non-commercial,
research-only, access-restricted, or otherwise incompatible terms are not
accepted. Commercial use alone is not sufficient evidence of redistribution
rights; every released artifact still requires the source record and review
described below.

## Public source matrix

The following matrix covers the source families used for public Train/Dev
material. It does not describe the composition of the private final
evaluation. Every released entry is bound to the immutable public upstream
revision and evidence recorded under `data/sources/`.

| Task configuration | Declared upstream license | Distribution mode |
| --- | --- | --- |
| Belebele Korean | `CC-BY-SA-4.0` | Redistributable only with attribution and share-alike compliance |
| HRMCR | `Apache-2.0` | Redistributable |
| GSM8K | `MIT` | Redistributable |
| DeepMind Mathematics interpolation | `Apache-2.0` | Reproduce from a pinned generator |
| DeepMind Mathematics extrapolation | `Apache-2.0` | Reproduce from a pinned generator |
| AIME public development material | Dataset repository declares `Apache-2.0`; rights in problem text are not presumed | Source-fetch-only |
| TruthfulQA binary | `Apache-2.0` | Redistributable |
| RuleTaker | `Apache-2.0` | Redistributable |
| CRUXEval input prediction | `MIT` | Redistributable |
| CRUXEval output prediction | `MIT` | Redistributable |
| BABILong 4K/16K | `Apache-2.0 AND BSD-3-Clause` | Redistributable only with both notices |

Exact public counts and revisions are in the released data and source records.
Private final-evaluation source counts, selection criteria, and aggregate
outcomes remain operator-only until final evaluation is complete.

## License reference files

- [`LICENSES/Apache-2.0.txt`](LICENSES/Apache-2.0.txt), from the
  [Apache Software Foundation](https://www.apache.org/licenses/LICENSE-2.0.txt)
- [`LICENSES/BSD-3-Clause.txt`](LICENSES/BSD-3-Clause.txt), including the bAbI
  copyright notice and conditions
- [`LICENSES/CC-BY-SA-4.0.txt`](LICENSES/CC-BY-SA-4.0.txt), for Belebele-derived
  prompt adaptations
- [`LICENSES/MIT.txt`](LICENSES/MIT.txt), with source-specific copyright
  notices retained in `THIRD_PARTY_NOTICES.md`

The matrix records BABILong as `Apache-2.0 AND BSD-3-Clause`; `AND` is
intentional and must not be silently changed to an `OR` choice. The exact
artifact-to-license mapping, copyright holders, license evidence, and required
notices were verified file by file. This table alone is not source-level
license evidence; the released source records remain authoritative.

For Belebele Korean, this distribution preserves the approved creator
and source attribution, link the CC-BY-SA-4.0 license, indicate changes, and
apply the required share-alike terms to adaptations. Only the exact Korean
evaluation artifact covered by the approved source record may be included;
adjacent upstream material is out of scope unless separately reviewed.

For AIME material, a repository license declaration is not treated as
permission to redistribute underlying problem text. No AIME prompt may be
committed. This release provides only an approved, pinned user-run fetch and
materialization recipe with expected content hashes for public Train/Dev
material. Retrieved content, caches, fixtures, and container layers must
remain outside the repository and release artifacts.

## Required source record

Before any new source is materialized or a released source is changed, its
record under `data/sources/` must
include:

- stable source identifier and covered task configurations;
- canonical public upstream URL and immutable commit SHA;
- license evidence URL, evidence hash, and review status;
- exact input paths and expected content hashes;
- deterministic transformations and expected schema;
- distribution mode and files permitted for release; and
- approved attribution and notice wording.

Source names belong in provenance documentation, never in the router runtime
schema.

## Outcome separation

Public Train/Dev prompts and content-free outcomes must be separate files.
Outcomes may contain only the fields allowed by the public protocol. Model
answers, generated text, reasoning traces, raw
requests, gold answers, output hashes, and private serving errors must never be
published.

## Release gate

Released files have source pins, license evidence, attribution, modification
notices, and reproducible hashes. Changes to the public data remain subject to
the source-by-source review gate in
[`docs/REVIEW_GUIDE.md`](docs/REVIEW_GUIDE.md).
