# SPDX-FileCopyrightText: Copyright 2026 SK TELECOM CO., LTD.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import copy
import pathlib
import re
import unittest
from decimal import Decimal

from ossp_router.protocol import (
    ProtocolError,
    load_bundled_policy,
    load_input,
    load_json,
    load_outcomes,
    load_policy,
    loads_json,
    parse_input,
    parse_outcomes,
    parse_policy,
    parse_submission,
    policy_sha256,
)


ROOT = pathlib.Path(__file__).resolve().parents[1]
POLICY_SHA256 = "7c892c423da5fa762e7e1a93b9fa071be51e259b65d2b63a5ba434c4342d7a8e"


class ProtocolTest(unittest.TestCase):
    def setUp(self) -> None:
        self.input_value = load_json(ROOT / "data/toy/inputs.json")
        self.outcome_value = load_json(ROOT / "data/toy/outcomes.json")
        self.policy_value = load_json(ROOT / "configs/routing-policy.v1.json")
        self.submission_value = {
            "schema_version": 1,
            "challenge_id": self.input_value["challenge_id"],
            "policy_id": "ossp-2026-prompt-router-v1",
            "split": "toy",
            "tier": "fast",
            "decisions": [
                {"episode_id": item["episode_id"], "model_id": "ax31-light"}
                for item in self.input_value["episodes"]
            ],
        }

    def first_outcome(self, value=None):
        source = self.outcome_value if value is None else value
        return source["episodes"][0]["models"]["ax31-light"]

    def test_frozen_v1_toy_files_and_bundled_policy_validate(self) -> None:
        self.assertEqual(3, len(load_input(ROOT / "data/toy/inputs.json").episodes))
        self.assertEqual(
            9, len(load_outcomes(ROOT / "data/toy/outcomes.json").outcomes)
        )
        file_policy = load_policy(ROOT / "configs/routing-policy.v1.json")
        self.assertEqual(file_policy, load_bundled_policy())
        self.assertEqual(POLICY_SHA256, policy_sha256(file_policy))

    def test_challenge_id_is_nonempty_and_not_hard_coded(self) -> None:
        value = copy.deepcopy(self.input_value)
        value["challenge_id"] = "ossp-2026-router-toy"
        self.assertEqual("ossp-2026-router-toy", parse_input(value).challenge_id)

    def test_legacy_public_v1_shapes_are_rejected(self) -> None:
        old_input = copy.deepcopy(self.input_value)
        old_input["schema_version"] = "1.0"
        with self.assertRaises(ProtocolError):
            parse_input(old_input)

        numeric_float_input = copy.deepcopy(self.input_value)
        numeric_float_input["schema_version"] = loads_json('{"value":1.0}')["value"]
        with self.assertRaises(ProtocolError):
            parse_input(numeric_float_input)

        old_policy = {
            "schema_version": "1.0",
            "challenge_id": "ossp-2026-llm-router-challenge",
            "policy_id": "ossp-2026-prompt-router-v1",
            "models": {},
            "tiers": {},
            "warning_threshold": "0.95",
        }
        with self.assertRaises(ProtocolError):
            parse_policy(old_policy)

        old_outcomes = {
            "schema_version": 1,
            "challenge_id": self.input_value["challenge_id"],
            "split": "toy",
            "outcomes": [],
        }
        with self.assertRaises(ProtocolError):
            parse_outcomes(old_outcomes)

    def test_input_requires_exactly_prompt_or_messages(self) -> None:
        value = copy.deepcopy(self.input_value)
        value["episodes"][0]["messages"] = [{"role": "user", "content": "중복"}]
        with self.assertRaises(ProtocolError):
            parse_input(value)

        null_prompt = copy.deepcopy(self.input_value)
        null_prompt["episodes"][0]["prompt"] = None
        null_prompt["episodes"][0]["messages"] = [
            {"role": "user", "content": "prompt 키가 함께 있으면 안 됨"}
        ]
        with self.assertRaises(ProtocolError):
            parse_input(null_prompt)

        null_messages = copy.deepcopy(self.input_value)
        null_messages["episodes"][0]["messages"] = None
        with self.assertRaises(ProtocolError):
            parse_input(null_messages)

    def test_input_rejects_duplicate_episode_id(self) -> None:
        value = copy.deepcopy(self.input_value)
        value["episodes"].append(copy.deepcopy(value["episodes"][0]))
        with self.assertRaises(ProtocolError):
            parse_input(value)

    def test_input_rejects_hidden_context_fields(self) -> None:
        for field in ("task", "source_id", "gold", "outcome"):
            with self.subTest(field=field):
                value = copy.deepcopy(self.input_value)
                value["episodes"][0][field] = "hidden"
                with self.assertRaises(ProtocolError):
                    parse_input(value)

    def test_message_role_and_episode_id_are_bounded(self) -> None:
        bad_role = copy.deepcopy(self.input_value)
        bad_role["episodes"][1]["messages"][0]["role"] = "admin"
        with self.assertRaises(ProtocolError):
            parse_input(bad_role)

        long_id = copy.deepcopy(self.input_value)
        long_id["episodes"][0]["episode_id"] = "x" * 129
        with self.assertRaises(ProtocolError):
            parse_input(long_id)

    def test_outcome_rejects_model_content_and_hashes(self) -> None:
        for field in (
            "model_output",
            "reasoning",
            "output_hash",
            "gold_answer",
            "finish_reasons",
            "failed_generations",
        ):
            with self.subTest(field=field):
                value = copy.deepcopy(self.outcome_value)
                self.first_outcome(value)[field] = "not allowed"
                with self.assertRaises(ProtocolError):
                    parse_outcomes(value)

    def test_outcome_score_must_be_bounded_decimal_string(self) -> None:
        numeric = copy.deepcopy(self.outcome_value)
        self.first_outcome(numeric)["score"] = 0.6
        with self.assertRaises(ProtocolError):
            parse_outcomes(numeric)

        for invalid_score in (
            "1e-1",
            " 0.5 ",
            "+0.7",
            ".5",
            "00.5",
            "0.5_0",
            "0.",
            "0." + "1" * 31,
        ):
            with self.subTest(score=invalid_score):
                invalid = copy.deepcopy(self.outcome_value)
                self.first_outcome(invalid)["score"] = invalid_score
                with self.assertRaises(ProtocolError):
                    parse_outcomes(invalid)

    def test_fixed_decimal_grammar_matches_public_schemas(self) -> None:
        policy_schema = load_json(ROOT / "schemas/policy.v1.schema.json")
        policy_decimal = policy_schema["$defs"]["decimalString"]
        outcome_schema = load_json(ROOT / "schemas/outcome.v1.schema.json")
        score_decimal = outcome_schema["$defs"]["decimalScore"]

        def schema_accepts(schema, definition, text):
            if "$ref" in definition:
                referenced_name = definition["$ref"].rsplit("/", 1)[-1]
                if not schema_accepts(
                    schema, schema["$defs"][referenced_name], text
                ):
                    return False
            if "allOf" in definition and not all(
                schema_accepts(schema, item, text) for item in definition["allOf"]
            ):
                return False
            if "maxLength" in definition and len(text) > definition["maxLength"]:
                return False
            return "pattern" not in definition or re.search(
                definition["pattern"], text
            ) is not None

        valid_policy_decimals = (
            "0",
            "1",
            "1.25",
            "9" * 40,
            "9" * 39 + ".1",
            "0." + "1" * 30,
        )
        invalid_policy_decimals = (
            "1e-1",
            " 0.5 ",
            "+0.7",
            ".5",
            "00.5",
            "0_._5",
            "0.5_0",
            "0.",
            "0\n",
            "9" * 41,
            "9" * 40 + ".1",
            "0." + "1" * 31,
        )
        for text in valid_policy_decimals:
            with self.subTest(kind="valid-policy", text=text):
                self.assertTrue(schema_accepts(policy_schema, policy_decimal, text))
                value = copy.deepcopy(self.policy_value)
                value["models"]["ax31-light"]["input_token_rate"] = text
                self.assertEqual(
                    Decimal(text),
                    parse_policy(value).models["ax31-light"].input_token_rate,
                )
        for text in invalid_policy_decimals:
            with self.subTest(kind="invalid-policy", text=text):
                self.assertFalse(schema_accepts(policy_schema, policy_decimal, text))
                value = copy.deepcopy(self.policy_value)
                value["models"]["ax31-light"]["input_token_rate"] = text
                with self.assertRaises(ProtocolError):
                    parse_policy(value)

        positive_decimal = policy_schema["$defs"]["positiveDecimalString"]
        for text, expected in (("0", False), ("0.0", False), ("0.01", True), ("1", True)):
            with self.subTest(kind="positive-policy", text=text):
                self.assertEqual(
                    expected, schema_accepts(policy_schema, positive_decimal, text)
                )
                value = copy.deepcopy(self.policy_value)
                value["tiers"]["fast"]["budget_multiplier"] = text
                if expected:
                    self.assertEqual(
                        Decimal(text), parse_policy(value).tiers["fast"].budget_multiplier
                    )
                else:
                    with self.assertRaises(ProtocolError):
                        parse_policy(value)

        positive_unit_interval = policy_schema["$defs"][
            "positiveUnitIntervalDecimalString"
        ]
        for text, expected in (
            ("0", False),
            ("0.0", False),
            ("0.01", True),
            ("1.000", True),
            ("1.01", False),
        ):
            with self.subTest(kind="positive-unit-interval", text=text):
                self.assertEqual(
                    expected,
                    schema_accepts(policy_schema, positive_unit_interval, text),
                )
                value = copy.deepcopy(self.policy_value)
                value["budget_warning_ratio"] = text
                if expected:
                    self.assertEqual(
                        Decimal(text), parse_policy(value).budget_warning_ratio
                    )
                else:
                    with self.assertRaises(ProtocolError):
                        parse_policy(value)

        valid_scores = ("0", "0.5", "0." + "1" * 30, "1", "1." + "0" * 30)
        invalid_scores = (
            "1e-1",
            " 0.5 ",
            "+0.7",
            ".5",
            "00.5",
            "0.5_0",
            "0\n",
            "0." + "1" * 31,
            "1.01",
            "2",
        )
        for text in valid_scores:
            with self.subTest(kind="valid-score", text=text):
                self.assertTrue(schema_accepts(outcome_schema, score_decimal, text))
                value = copy.deepcopy(self.outcome_value)
                self.first_outcome(value)["score"] = text
                self.assertEqual(Decimal(text), parse_outcomes(value).outcomes[0].score)
        for text in invalid_scores:
            with self.subTest(kind="invalid-score", text=text):
                self.assertFalse(schema_accepts(outcome_schema, score_decimal, text))
                value = copy.deepcopy(self.outcome_value)
                self.first_outcome(value)["score"] = text
                with self.assertRaises(ProtocolError):
                    parse_outcomes(value)

    def test_outcome_rejects_duplicate_episode_id(self) -> None:
        value = copy.deepcopy(self.outcome_value)
        value["episodes"].append(copy.deepcopy(value["episodes"][0]))
        with self.assertRaises(ProtocolError):
            parse_outcomes(value)

    def test_outcome_generation_contract_is_strict(self) -> None:
        mutations = (
            ("input_tokens", 0),
            ("num_generations", 0),
            ("output_tokens", -1),
        )
        for field, bad_value in mutations:
            with self.subTest(field=field, value=bad_value):
                value = copy.deepcopy(self.outcome_value)
                self.first_outcome(value)[field] = bad_value
                with self.assertRaises(ProtocolError):
                    parse_outcomes(value)

    def test_policy_rejects_nonpositive_budget_values(self) -> None:
        zero_budget = copy.deepcopy(self.policy_value)
        zero_budget["tiers"]["fast"]["budget_multiplier"] = "0"
        with self.assertRaises(ProtocolError):
            parse_policy(zero_budget)

        zero_warning = copy.deepcopy(self.policy_value)
        zero_warning["budget_warning_ratio"] = "0"
        with self.assertRaises(ProtocolError):
            parse_policy(zero_warning)

    def test_policy_token_unit_is_frozen_in_schema_and_parser(self) -> None:
        schema = load_json(ROOT / "schemas/policy.v1.schema.json")
        self.assertEqual(1_000_000, schema["properties"]["token_unit"]["const"])
        value = copy.deepcopy(self.policy_value)
        value["token_unit"] = 3
        with self.assertRaises(ProtocolError):
            parse_policy(value)

    def test_submission_rejects_duplicate_episode(self) -> None:
        value = copy.deepcopy(self.submission_value)
        value["decisions"].append(copy.deepcopy(value["decisions"][0]))
        with self.assertRaises(ProtocolError):
            parse_submission(value)

    def test_submission_rejects_unknown_model(self) -> None:
        value = copy.deepcopy(self.submission_value)
        value["decisions"][0]["model_id"] = "unknown"
        with self.assertRaises(ProtocolError):
            parse_submission(value)

    def test_json_loader_rejects_duplicate_keys(self) -> None:
        with self.assertRaises(ProtocolError):
            loads_json('{"schema_version":1,"schema_version":2}')


if __name__ == "__main__":
    unittest.main()
