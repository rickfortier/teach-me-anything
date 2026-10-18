import os
import unittest
from unittest.mock import Mock, patch

import requests
from summary_providers import SummaryProviders, ProviderError, http_summary, permanent_error


class ProviderTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-a", "OPENROUTER_API_KEY": "test-r", "OPENAI_API_KEY": "test-o"}, clear=True)
        env.start()
        self.addCleanup(env.stop)
        self.anthropic = Mock(return_value="Claude summary")
        self.service = SummaryProviders("system", self.anthropic, lambda e: str(e))

    def test_first_success_does_not_call_fallback(self):
        with patch("summary_providers.http_summary", return_value="Router summary") as http:
            result = self.service.summarize("id", "title", "text")
        self.assertEqual(result.provider, "openrouter")
        self.assertEqual(http.call_count, 1)
        self.anthropic.assert_not_called()

    def test_credit_errors_disable_provider_for_rest_of_batch(self):
        self.assertTrue(permanent_error(ProviderError("You have no credits remaining", 429)))
        self.assertTrue(permanent_error(ProviderError("billing failure", 200, 402)))
        self.assertFalse(permanent_error(ProviderError("Rate limit exceeded", 429)))

    def test_billing_failure_falls_back_and_disables_only_that_provider(self):
        with patch("summary_providers.http_summary", side_effect=ProviderError("credit balance is too low", 400)) as http:
            result = self.service.summarize("id", "title", "text")
            self.service.summarize("id2", "title", "text")
        self.assertEqual(result.provider, "anthropic")
        self.assertEqual(http.call_count, 1)
        self.assertEqual(self.anthropic.call_count, 2)
        self.assertIsNone(self.service.unavailable_reason())

    def test_openrouter_error_then_timeout_then_openai_success(self):
        self.anthropic.side_effect = requests.Timeout()
        with patch("summary_providers.http_summary", side_effect=[ProviderError("credits exhausted", 402), "OpenAI summary"]):
            result = self.service.summarize("id", "title", "text")
        self.assertEqual(result.provider, "openai")
        self.assertNotIn("anthropic", self.service.disabled)
        self.assertIn("openrouter", self.service.disabled)

    def test_missing_keys_are_skipped(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-o"}, clear=True):
            service = SummaryProviders("system", self.anthropic, str)
        with patch("summary_providers.http_summary", return_value="ok") as http:
            self.assertEqual(service.summarize("id", "title", "text").provider, "openai")
        self.anthropic.assert_not_called()
        self.assertEqual(http.call_args.args[0], "openai")

    def test_all_failed_has_useful_error(self):
        self.anthropic.side_effect = ProviderError("bad key", 401)
        with patch("summary_providers.http_summary", side_effect=ProviderError("no credit", 402)):
            with self.assertRaisesRegex(ProviderError, "Add credits or configure"):
                self.service.summarize("id", "title", "text")
        self.assertIsNotNone(self.service.unavailable_reason())

    def test_empty_response_tries_next_provider(self):
        with patch("summary_providers.http_summary", return_value=" "):
            self.assertEqual(self.service.summarize("id", "title", "text").provider, "anthropic")

    def test_custom_order_and_model(self):
        with patch.dict(os.environ, {"SUMMARY_PROVIDERS": "openai,anthropic", "OPENAI_SUMMARY_MODEL": "custom-model"}):
            service = SummaryProviders("system", self.anthropic, str)
        with patch("summary_providers.http_summary", return_value="ok") as http:
            service.summarize("id", "title", "text")
        self.assertEqual(http.call_args.args[:2], ("openai", "custom-model"))

    def test_openai_wire_format_and_output_parsing(self):
        response = Mock(status_code=200)
        response.json.return_value = {"status": "completed", "output": [
            {"type": "reasoning"}, {"type": "message", "content": [{"type": "output_text", "text": "Summary"}]},
        ]}
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        with patch("summary_providers.requests.post", return_value=response) as post:
            self.assertEqual(http_summary("openai", "model", "key", "system", "prompt"), "Summary")
        self.assertEqual(post.call_args.args[0], "https://api.openai.com/v1/responses")
        self.assertFalse(post.call_args.kwargs["json"]["store"])
        self.assertEqual(post.call_args.kwargs["json"]["input"], "prompt")

    def test_openrouter_wire_format_and_error_payload(self):
        response = Mock(status_code=200)
        response.json.return_value = {"error": {"message": "out of credits", "code": 402}}
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        with patch("summary_providers.requests.post", return_value=response) as post:
            with self.assertRaisesRegex(ProviderError, "out of credits"):
                http_summary("openrouter", "model", "key", "system", "prompt")
        self.assertEqual(post.call_args.args[0], "https://openrouter.ai/api/v1/chat/completions")
        self.assertEqual(post.call_args.kwargs["json"]["messages"][0]["role"], "system")


if __name__ == "__main__":
    unittest.main()
