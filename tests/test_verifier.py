"""No-network behavior tests for the public standalone Hermes plugin."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("freeverifier_test_module", ROOT / "verifier.py")
verifier = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verifier)


class VerifierTests(unittest.TestCase):
    def test_openrouter_catalog_only_free(self):
        payload = {"data": [
            {"id": "free/a:free", "pricing": {"prompt": "0", "completion": "0"}},
            {"id": "free/b", "pricing": {"prompt": "0.0", "completion": "0.000"}},
            {"id": "paid/x", "pricing": {"prompt": "0", "completion": "0.01"}},
            {"id": "unknown/y", "pricing": {}},
        ]}
        with patch.object(verifier, "_request", return_value=payload) as send:
            self.assertEqual(verifier._openrouter_candidates("secret"), ["free/a:free", "free/b"])
            self.assertTrue(send.call_args.args[0].endswith("/models/user"))

    def test_openrouter_tool_cycle(self):
        first = {"choices": [{"message": {"role": "assistant", "content": None,
            "tool_calls": [{"id": "call_1", "type": "function", "function": {
                "name": "hermes_add", "arguments": '{"a":7,"b":5}'}}]}}]}
        second = {"choices": [{"message": {"role": "assistant", "content": "The answer is 12."}}]}
        with patch.object(verifier, "_request", side_effect=[first, second]) as send:
            self.assertEqual(verifier._probe_tools("openrouter", "x:free", "secret"), "full_pass")
            followup = send.call_args.args[3]
            self.assertEqual(followup["messages"][-1]["role"], "tool")
            self.assertEqual(followup["messages"][-1]["tool_call_id"], "call_1")
            self.assertEqual(followup["tool_choice"], "none")

    def test_gemini_preserves_thought_signature(self):
        first = {"candidates": [{"content": {"role": "model", "parts": [{
            "functionCall": {"name": "hermes_add", "args": {"a": 7, "b": 5}},
            "thoughtSignature": "opaque-signed-part"}]}}]}
        second = {"candidates": [{"content": {"role": "model", "parts": [{"text": "12"}]}}]}
        with patch.object(verifier, "_request", side_effect=[first, second]) as send:
            self.assertEqual(verifier._probe_tools("gemini", "gemini-test", "secret"), "full_pass")
            followup = send.call_args.args[3]
            self.assertEqual(followup["contents"][1]["parts"][0]["thoughtSignature"], "opaque-signed-part")
            self.assertEqual(followup["contents"][2]["parts"][0]["functionResponse"]["response"]["result"], 12)

    def test_no_false_positive_from_wrong_arguments(self):
        with self.assertRaisesRegex(verifier.ProbeError, "no_valid_tool_call"):
            verifier._verified_call("gemini", [{"name": "hermes_add", "args": {"a": 8, "b": 5}}])

    def test_state_does_not_contain_secrets_and_is_private(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(verifier, "_home", return_value=Path(temp)):
            state = verifier._load_state()
            token = "TOP_SECRET_TEST_API_KEY"
            state["providers"]["nvidia"] = {
                "identity": verifier._credential_id("nvidia", token, state["salt"]),
                "models": {"model": {"mode": "tools", "status": "full_pass", "checked_at": 1}},
            }
            verifier._save_state(state)
            p = Path(temp) / verifier.STATE_NAME
            self.assertEqual(p.stat().st_mode & 0o777, 0o600)
            self.assertNotIn(token, p.read_text())
            self.assertEqual(verifier._load_state(), state)
            self.assertNotEqual(state["providers"]["nvidia"]["identity"],
                                verifier._credential_id("nvidia", "new_token", state["salt"]))

    def test_no_network_or_credentials_for_chat_status(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(verifier, "_home", return_value=Path(temp)):
            with patch.object(verifier, "_request", side_effect=AssertionError("network")):
                self.assertIn("none saved", verifier.chat_status(""))

    def test_unpriced_denied_before_credential_reads(self):
        with patch.object(verifier, "_get_secret", side_effect=AssertionError("credential read")):
            with self.assertRaisesRegex(verifier.ProbeError, "cannot establish|do not prove"):
                verifier.run_scan("nvidia", [], "tools", 5, False)

    def test_error_does_not_print_secret(self):
        # Provider HTTP response bodies are deliberately not included in errors.
        from urllib.error import HTTPError
        error = HTTPError("https://example.com/", 429, "key=secret", {}, None)
        class FakeOpener:
            def open(self, *args, **kwargs):
                raise error
        with patch.object(verifier, "build_opener", return_value=FakeOpener()):
            with self.assertRaisesRegex(verifier.ProbeError, "^http_429$"):
                verifier._request("https://example.com/", "secret", "nvidia", {"hello": "world"})


if __name__ == "__main__":
    unittest.main()
