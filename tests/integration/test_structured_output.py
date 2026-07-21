from __future__ import annotations

import json
import unittest

from imagegen.services.structured_output import parse_json_object


class TestStructuredOutput(unittest.TestCase):
    def test_parses_one_object_with_supported_ai_wrappers(self):
        expected = {
            "status": "ready",
            "prompt": "Keep the literal {brace} text",
            "brief": {"subject": "shoe"},
        }
        encoded = json.dumps(expected)

        for content in (
            encoded,
            f"```JSON\n{encoded}\n```",
            f"Result follows:\n{encoded}\nEnd of result.",
        ):
            with self.subTest(content=content):
                self.assertEqual(parse_json_object(content), expected)

    def test_rejects_ambiguous_or_malformed_output(self):
        cases = (
            "",
            "no json here",
            '[{"status":"ready"}]',
            'Result: [{"status":"ready"}]',
            '{"status":"ready"}\n{"status":"needs_clarification"}',
            '{"status":"ready","status":"needs_clarification"}',
            '{"status":"ready","brief":{"subject":"a","subject":"b"}}',
            'prefix {not json} suffix {"status":"ready"}',
            '{"status":"ready"',
            '{"status":"ready"} trailing {',
        )

        for content in cases:
            with self.subTest(content=content):
                self.assertIsNone(parse_json_object(content))
