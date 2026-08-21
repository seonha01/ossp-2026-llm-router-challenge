<!--
SPDX-FileCopyrightText: Copyright 2026 SK TELECOM CO., LTD.
SPDX-License-Identifier: Apache-2.0
-->

# Third-party data notices

This notice applies to the adapted public prompts in
`data/train/inputs-base.json` and `data/dev/inputs-base.json`. Those files are
collections. Each source-derived part retains the license below; the project
Apache-2.0 license does not relicense third-party material. Exact revisions,
artifact hashes, and license-evidence hashes are in
`data/sources/source-pins.v1.json`.

## Belebele Korean

Source: Meta's Belebele `kor_Hang` configuration. Licensed under
[CC BY-SA 4.0](LICENSES/CC-BY-SA-4.0.txt). The released adaptation selects a
subset, formats passage, question, and choices as a prompt, omits answer
labels, and assigns opaque episode IDs. No endorsement is implied.

Attribution: Lucas Bandarkar, Davis Liang, Benjamin Muller, Mikel Artetxe,
Satya Narayan Shukla, Donald Husa, Naman Goyal, Abhinandan Krishnan, Luke
Zettlemoyer, and Madian Khabsa, *The Belebele Benchmark: a Parallel Reading
Comprehension Dataset in 122 Language Variants*, ACL 2024.

## CRUXEval

Copyright (c) 2023 Meta. Licensed under the [MIT License](LICENSES/MIT.txt).
The adaptation selects public examples, applies the direct input-prediction or
output-prediction prompt, omits reference inputs or outputs, and assigns opaque
episode IDs.

## GSM8K

Copyright (c) 2021 OpenAI. Licensed under the
[MIT License](LICENSES/MIT.txt). The adaptation selects public test questions,
omits solutions and answers, and assigns opaque episode IDs.

## BABILong 4K/16K components

The bAbI tasks component is copyright (c) 2015-present Facebook, Inc. and is
licensed under [BSD-3-Clause](LICENSES/BSD-3-Clause.txt). BABILong code and the
PG-19 component are licensed under [Apache-2.0](LICENSES/Apache-2.0.txt).
The adaptation uses only approved 4K and 16K configurations, adds a zero-shot task
instruction, omits targets, and assigns opaque episode IDs. Neither Facebook
nor any contributor endorses this project.

## Apache-2.0 sources

The following adapted public prompts are licensed under
[Apache-2.0](LICENSES/Apache-2.0.txt): DeepMind Mathematics, HRMCR, RuleTaker,
and TruthfulQA. Each adaptation selects an approved subset, formats only the
question-side prompt, omits gold answers and solutions, and assigns opaque
episode IDs. DeepMind Mathematics prompts are independently reproduced from
the pinned upstream generator and verified against the reference hashes in the
source record.

## Source-fetch-only material

AIME problem text is not included in this repository or release archive.
`data/train/aime-selection.json` and `data/dev/aime-selection.json` contain
only public source keys and expected prompt hashes. Users fetch the pinned
public sources and materialize those prompts locally.
