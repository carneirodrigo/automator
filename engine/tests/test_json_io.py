"""Tests for engine.work.json_io — JSON extraction from LLM output."""

import json
import unittest

from engine.work.json_io import extract_json_payload


class TestExtractJsonPayload(unittest.TestCase):
    """Coverage for the bracket-matching JSON extractor used on every agent response."""

    def test_clean_json(self):
        result = extract_json_payload('{"status": "pass", "summary": "ok"}')
        self.assertEqual(result["status"], "pass")

    def test_markdown_fenced(self):
        text = '```json\n{"status": "pass"}\n```'
        result = extract_json_payload(text)
        self.assertEqual(result["status"], "pass")

    def test_triple_backtick_no_lang(self):
        text = '```\n{"status": "pass"}\n```'
        result = extract_json_payload(text)
        self.assertEqual(result["status"], "pass")

    def test_preamble_text_before_json(self):
        text = 'Here is the result:\n{"status": "pass", "summary": "done"}'
        result = extract_json_payload(text)
        self.assertEqual(result["status"], "pass")

    def test_trailing_comma_in_object(self):
        text = '{"status": "pass", "summary": "done",}'
        result = extract_json_payload(text)
        self.assertEqual(result["status"], "pass")

    def test_trailing_comma_in_array(self):
        text = '{"items": ["a", "b",]}'
        result = extract_json_payload(text)
        self.assertEqual(result["items"], ["a", "b"])

    def test_nested_response_unwrap(self):
        inner = '{"status": "pass"}'
        outer = f'{{"response": "{inner.replace(chr(34), chr(92)+chr(34))}"}}'
        result = extract_json_payload(outer)
        self.assertEqual(result["status"], "pass")

    def test_nested_result_unwrap(self):
        inner = '{"status": "fail"}'
        outer = f'{{"result": "{inner.replace(chr(34), chr(92)+chr(34))}"}}'
        result = extract_json_payload(outer)
        self.assertEqual(result["status"], "fail")

    def test_recursion_depth_cap(self):
        """Deeply nested response/result wrappers cap at 2 levels."""
        # At depth 2, unwrapping stops — the function returns the deepest
        # successfully parsed dict it can reach within the recursion limit.
        inner = '{"status": "deep"}'
        mid = f'{{"response": "{inner.replace(chr(34), chr(92)+chr(34))}"}}'
        outer = f'{{"response": "{mid.replace(chr(34), chr(92)+chr(34))}"}}'
        result = extract_json_payload(outer)
        # Should return a dict (even if not the innermost) — not crash or loop
        self.assertIsInstance(result, dict)

    def test_empty_string_returns_empty_dict(self):
        self.assertEqual(extract_json_payload(""), {})

    def test_non_json_returns_empty_dict(self):
        self.assertEqual(extract_json_payload("This is just text with no JSON"), {})

    def test_json_array_returns_empty_dict(self):
        """Top-level arrays are not valid agent output."""
        self.assertEqual(extract_json_payload('[1, 2, 3]'), {})

    def test_strings_with_braces(self):
        text = '{"msg": "use {curly} braces in text"}'
        result = extract_json_payload(text)
        self.assertEqual(result["msg"], "use {curly} braces in text")

    def test_escaped_quotes_in_strings(self):
        text = '{"msg": "she said \\"hello\\""}'
        result = extract_json_payload(text)
        self.assertEqual(result["msg"], 'she said "hello"')

    def test_multiple_json_objects_returns_first(self):
        text = '{"a": 1}\n{"b": 2}'
        result = extract_json_payload(text)
        self.assertEqual(result, {"a": 1})

    def test_no_unwrap_when_sibling_keys_present(self):
        """response/result unwrapping must not discard sibling keys like capability_requests."""
        text = json.dumps({
            "response": '{"status": "pass"}',
            "capability_requests": [{"capability": "read_file"}],
        })
        result = extract_json_payload(text)
        # Should return the outer dict intact, not unwrap into just {"status": "pass"}
        self.assertIn("capability_requests", result)
        self.assertIsInstance(result["capability_requests"], list)


if __name__ == "__main__":
    unittest.main()
