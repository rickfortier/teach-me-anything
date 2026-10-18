"""Narration for the walkthrough scenes: text in, MP3 plus word timings out.

Engines (config.yaml `tts_engine`):
  kokoro  - Kokoro-FastAPI on your own machine (docker run -p 8880:8880
            ghcr.io/remsky/kokoro-fastapi-cpu:latest, or the -gpu image). Returns
            word timestamps, which later drive captions and video scene timing.
  openai  - any OpenAI-compatible /audio/speech endpoint (a hosted Kokoro such as
            DeepInfra's, or Kokoro-FastAPI's own /v1). Audio only, no timings.
            The key comes from TTS_API_KEY in .env.
  off     - no recorded narration; the app reads scenes with the system voice.
"""
from __future__ import annotations

import base64
import os

import requests


class TTSUnavailable(Exception):
    friendly = True      # message is safe and readable as-is


class Narrator:
    def __init__(self, config: dict):
        self.engine = str(config.get("tts_engine", "off")).lower()
        if self.engine not in ("kokoro", "openai", "off"):
            raise ValueError("tts_engine must be kokoro, openai or off")
        default_url = "http://localhost:8880" if self.engine == "kokoro" else ""
        self.url = str(config.get("tts_url") or default_url).rstrip("/")
        self.model = config.get("tts_model", "kokoro")
        self.voice = config.get("tts_voice", "af_heart")
        self.speed = float(config.get("tts_speed", 1.0))
        self.key = os.getenv("TTS_API_KEY", "").strip()

    @property
    def enabled(self) -> bool:
        return self.engine != "off"

    @property
    def label(self) -> str:
        return f"{self.engine}:{self.voice}"

    def unavailable_reason(self) -> str | None:
        """None when the engine answers; otherwise why narration is skipped this build."""
        if not self.enabled:
            return "narration is off (tts_engine: off)"
        if not self.url:
            return "tts_url is not set in config.yaml"
        if self.engine == "kokoro":
            try:
                requests.get(f"{self.url}/v1/audio/voices", timeout=5).raise_for_status()
            except requests.RequestException as exc:
                return (f"the Kokoro server at {self.url} is not answering ({type(exc).__name__}). "
                        "Start it, then use \"Retry failed parts\"")
        return None

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.key}"} if self.key else {}

    def speak(self, text: str) -> tuple[bytes, list[dict] | None]:
        """(MP3 bytes, [{"word", "start", "end"}] in seconds or None)."""
        body = {"model": self.model, "input": text, "voice": self.voice, "response_format": "mp3",
                "speed": self.speed}
        try:
            if self.engine == "kokoro":
                resp = requests.post(f"{self.url}/dev/captioned_speech", timeout=(10, 300), headers=self._headers(),
                                     json=body | {"stream": False, "return_timestamps": True})
                resp.raise_for_status()
                data = resp.json()
                words = [{"word": t["word"], "start": round(float(t["start_time"]), 3),
                          "end": round(float(t["end_time"]), 3)} for t in data.get("timestamps") or []]
                return base64.b64decode(data["audio"]), words
            resp = requests.post(f"{self.url}/audio/speech", timeout=(10, 300), headers=self._headers(), json=body)
            resp.raise_for_status()
            return resp.content, None
        except requests.RequestException as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            raise TTSUnavailable(f"Text-to-speech failed ({status or type(exc).__name__})") from exc
        except (KeyError, ValueError) as exc:
            raise TTSUnavailable(f"Text-to-speech returned an unreadable response ({exc})") from exc
