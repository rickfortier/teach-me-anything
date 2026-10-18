"""Ollama (no keys), per-task model lists, and the setup status."""
import os
import unittest
from unittest.mock import Mock, patch

import llm as llm_mod
import ollama
from summary_providers import env_key, structured_http

SCHEMA = {"type": "object", "properties": {"title": {"type": "string"}}, "required": ["title"],
          "additionalProperties": False}
NO_KEYS = {k: "" for k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "OPENROUTER_API_KEY", "GEMINI_API_KEY",
                           "ANTHROPIC_MODEL", "OPENAI_MODEL", "OPENROUTER_MODEL", "GEMINI_MODEL", "OLLAMA_MODEL",
                           "YOUTUBE_API_KEY")}
CONFIG = {"tasks": {"plan": ["openai/gpt-6-luna@low", "ollama/auto"],
                    "lesson": ["openai/gpt-6-luna@low", "anthropic/claude-sonnet-5", "ollama/auto"],
                    "video_judge": ["gemini/gemini-3.5-flash-lite"]}}


def response(data):
    r = Mock(status_code=200)
    r.json.return_value = data
    r.__enter__ = Mock(return_value=r)
    r.__exit__ = Mock(return_value=False)
    return r


class LocalModelTests(unittest.TestCase):
    def setUp(self):
        ollama._cache.clear()
        self.addCleanup(ollama._cache.clear)

    def test_best_installed_model_is_picked(self):
        with patch.dict(os.environ, NO_KEYS), patch("ollama.installed", return_value=["llama3.1:8b", "qwen3:14b"]):
            self.assertEqual(ollama.pick_model("auto"), "qwen3:14b")
            self.assertEqual(ollama.pick_model("gemma3:4b"), "gemma3:4b")
            with patch.dict(os.environ, {"OLLAMA_MODEL": "mine:latest"}):
                self.assertEqual(ollama.pick_model("auto"), "mine:latest")
        with patch("ollama.installed", return_value=["custom:7b"]):
            self.assertEqual(ollama.pick_model("auto"), "custom:7b")

    def test_ollama_wire_format(self):
        data = {"message": {"content": '{"title": "t"}'}, "done_reason": "stop", "prompt_eval_count": 10,
                "eval_count": 20}
        with patch("ollama.installed", return_value=["qwen3:8b"]), \
                patch("summary_providers.requests.post", return_value=response(data)) as post:
            text = structured_http("ollama", "auto", "local", "sys", "prompt", SCHEMA, 4000, effort="none")
        self.assertEqual(text, '{"title": "t"}')
        self.assertEqual(post.call_args.args[0], "http://localhost:11434/api/chat")
        body = post.call_args.kwargs["json"]
        self.assertEqual((body["model"], body["format"], body["stream"], body["think"]), ("qwen3:8b", SCHEMA, False, False))
        self.assertEqual(post.call_args.kwargs["headers"], {})                 # no key sent anywhere
        meter = llm_mod.Meter()
        meter.http("day 1 lesson", "qwen3:8b", data)
        self.assertEqual((meter.calls[0]["provider"], meter.calls[0]["cost"]), ("ollama", 0.0))

    def test_no_keys_uses_the_local_model_in_the_list(self):
        calls = []

        def http(provider, model, key, system, prompt, schema, max_tokens, on_usage=None, effort=None):
            calls.append((provider, key))
            return '{"title": "t"}'
        with patch.dict(os.environ, NO_KEYS), patch("ollama.installed", return_value=["qwen3:8b"]), \
                patch("summary_providers.structured_http", http):
            real = llm_mod.LLM(CONFIG)
            self.assertEqual(real.json("day 1 lesson", "s", "p", SCHEMA), {"title": "t"})
        self.assertEqual(calls, [("ollama", "local")])

    def test_nothing_available_names_the_missing_keys(self):
        with patch.dict(os.environ, NO_KEYS), patch("ollama.installed", return_value=None):
            with self.assertRaisesRegex(Exception, "No task has a usable model.*no OPENAI_API_KEY in .env"):
                llm_mod.LLM(CONFIG, progress=lambda m: None)

    def test_placeholder_keys_count_as_missing(self):
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-ant-...", "OPENAI_API_KEY": " real-key "}):
            self.assertEqual(env_key("anthropic"), "")
            self.assertEqual(env_key("openai"), "real-key")


class TaskListTests(unittest.TestCase):
    def setUp(self):
        ollama._cache.clear()
        self.addCleanup(ollama._cache.clear)

    def run_lesson(self, env):
        seen = []

        def anthropic(effort, on_message=None):
            def call(system, prompt, schema, model):
                seen.append(("anthropic", model))
                return '{"title": "t"}'
            return call

        def http(provider, model, key, system, prompt, schema, max_tokens, on_usage=None, effort=None):
            seen.append((provider, model))
            return '{"title": "t"}'
        with patch.dict(os.environ, NO_KEYS | env), patch("ollama.installed", return_value=None), \
                patch("llm.anthropic_call", anthropic), patch("summary_providers.structured_http", http):
            real = llm_mod.LLM(CONFIG, progress=lambda m: None)
            real.json("day 1 lesson", "s", "p", SCHEMA)
        return seen

    def test_first_model_with_a_key_is_used(self):
        self.assertEqual(self.run_lesson({"ANTHROPIC_API_KEY": "a", "OPENAI_API_KEY": "o"}),
                         [("openai", "gpt-6-luna")])
        self.assertEqual(self.run_lesson({"ANTHROPIC_API_KEY": "a"}), [("anthropic", "claude-sonnet-5")])

    def test_model_in_env_no_longer_overrides_the_list(self):
        seen = self.run_lesson({"ANTHROPIC_API_KEY": "a", "ANTHROPIC_MODEL": "claude-opus-5", "OPENAI_API_KEY": "o"})
        self.assertEqual(seen, [("openai", "gpt-6-luna")])
        with patch.dict(os.environ, NO_KEYS | {"OPENROUTER_MODEL": "z-ai/glm-5.3"}):
            self.assertEqual(llm_mod.ignored_env_models(), ["OPENROUTER_MODEL"])

    def test_status_reports_without_secrets(self):
        with patch.dict(os.environ, NO_KEYS | {"OPENAI_API_KEY": "secret-value"}), \
                patch("ollama.installed", return_value=[]), patch("llm.check_key", llm_mod.missing_key), \
                patch("llm.extra_problems", return_value=[]):
            status = llm_mod.provider_status(CONFIG)
        self.assertNotIn("secret-value", str(status))
        self.assertTrue(status["can_build"])
        self.assertFalse(status["keys"]["gemini"])
        self.assertEqual(status["tasks"]["lesson"][0], {"model": "openai/gpt-6-luna@low", "ok": True, "problem": None})
        self.assertTrue(any(p.startswith("video_judge: no usable model (no GEMINI_API_KEY") for p in status["problems"]))
        self.assertTrue(any(p.startswith("quiz: no usable model (no models listed") for p in status["problems"]))
        self.assertFalse(any(p.startswith(("plan", "lesson")) for p in status["problems"]))
        with patch.dict(os.environ, NO_KEYS), patch("ollama.installed", return_value=["qwen3:8b"]), \
                patch("llm.check_key", llm_mod.missing_key), patch("llm.extra_problems", return_value=[]):
            local = llm_mod.provider_status(CONFIG)
        self.assertEqual((local["can_build"], local["local_only"], local["ollama"]["model"]), (True, True, "qwen3:8b"))

    def test_key_check_reports_rejected_and_empty_keys(self):
        def get(status, data=None):
            r = Mock(status_code=status)
            r.json.return_value = data or {}
            return r
        with patch.dict(os.environ, NO_KEYS | {"OPENROUTER_API_KEY": "r", "GEMINI_API_KEY": "g"}):
            with patch("requests.get", return_value=get(401)):
                self.assertIn("was rejected", llm_mod.check_key("openrouter"))
            with patch("requests.get", return_value=get(200, {"data": {"limit_remaining": 0}})):
                self.assertEqual(llm_mod.check_key("openrouter"), "the OpenRouter key has no credit left")
            with patch("requests.get", return_value=get(200, {"data": {"limit_remaining": None}})):
                self.assertIsNone(llm_mod.check_key("openrouter"))
            self.assertEqual(llm_mod.check_key("openai"), "no OPENAI_API_KEY in .env")


class VideoJudgeTests(unittest.TestCase):
    def test_gemini_through_openrouter_watches_the_video(self):
        config = {"tasks": {"plan": ["openrouter/openai/gpt-6-luna"],
                            "video_judge": ["gemini/gemini-3.5-flash-lite", "openrouter/google/gemini-3.5-flash-lite@minimal",
                                            "openrouter/openai/gpt-6-luna"]}}
        data = {"choices": [{"finish_reason": "stop", "message": {"content": '{"title": "t"}'}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "cost": 0.01}}
        with patch.dict(os.environ, NO_KEYS | {"OPENROUTER_API_KEY": "r"}), patch("ollama.installed", return_value=None),                 patch("summary_providers.requests.post", return_value=response(data)) as post:
            real = llm_mod.LLM(config, progress=lambda m: None)
            self.assertEqual([r[1] for r in real.judges()], ["google/gemini-3.5-flash-lite"])   # no Gemini key; Luna can't watch
            self.assertEqual(real.video_json("day 1 video judge", "s", "p", SCHEMA, "https://www.youtube.com/watch?v=x"),
                             {"title": "t"})
        body = post.call_args.kwargs["json"]
        self.assertEqual(body["provider"], {"only": ["google-ai-studio"], "allow_fallbacks": False})
        self.assertEqual(body["messages"][1]["content"][0],
                         {"type": "video_url", "video_url": {"url": "https://www.youtube.com/watch?v=x"}})
        self.assertEqual(body["reasoning"], {"effort": "minimal"})
        self.assertAlmostEqual(real.spent(), 0.01)


class EnvFileTests(unittest.TestCase):
    def test_missing_env_is_created_blank_and_never_overwritten(self):
        import tempfile
        from pathlib import Path
        import course_builder as cb
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".env.example").write_text("# help\nGEMINI_API_KEY=\n", encoding="utf-8")
            path = cb.ensure_env(root)
            self.assertEqual(path.read_text(encoding="utf-8"), "# help\nGEMINI_API_KEY=\n")
            path.write_text("GEMINI_API_KEY=AIzaMine\n", encoding="utf-8")
            cb.ensure_env(root)
            self.assertEqual(path.read_text(encoding="utf-8"), "GEMINI_API_KEY=AIzaMine\n")   # untouched
        with tempfile.TemporaryDirectory() as tmp:                     # no .env.example either
            text = cb.ensure_env(Path(tmp)).read_text(encoding="utf-8")
            for name in ("ANTHROPIC_API_KEY=", "OPENAI_API_KEY=", "OPENROUTER_API_KEY=",
                         "GEMINI_API_KEY="):
                self.assertIn(f"\n{name}\n", text)


if __name__ == "__main__":
    unittest.main()


