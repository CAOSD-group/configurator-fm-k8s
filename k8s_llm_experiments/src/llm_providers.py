from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any
import time


@dataclass
class GenerationResult:
    text: str
    provider: str
    model: str
    latency_seconds: float
    usage: dict[str, Any]
    provider_metadata: dict[str, Any]
    thinking: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class LLMProvider:
    def generate(
        self,
        *,
        model: str,
        system_prompt: str,
        user_prompt: str,
        temperature: float,
        max_output_tokens: int,
    ) -> GenerationResult:
        raise NotImplementedError


class OllamaProvider(LLMProvider):
    def __init__(
        self,
        *,
        host: str = "http://127.0.0.1:11434",
        timeout: float = 180.0,
        num_ctx: int = 4096,
        think: bool = False,
    ) -> None:
        try:
            from ollama import Client
        except ImportError as exc:
            raise RuntimeError(
                "The Python package 'ollama' is not installed. "
                "Run: python -m pip install ollama"
            ) from exc

        self._client = Client(host=host, timeout=timeout)
        self.host = host
        self.num_ctx = num_ctx
        self.think = think

    @staticmethod
    def _get(response: Any, name: str, default: Any = None) -> Any:
        if isinstance(response, dict):
            return response.get(name, default)
        return getattr(response, name, default)

    def generate(
        self,
        *,
        model: str,
        system_prompt: str,
        user_prompt: str,
        temperature: float,
        max_output_tokens: int,
    ) -> GenerationResult:
        # max_output_tokens is intentionally not translated to num_predict here:
        # the caller can compare providers without silently changing provider
        # semantics. Set num_predict below if you want a hard Ollama cap.
        options = {
            "temperature": temperature,
            "num_ctx": self.num_ctx,
            "num_predict": max_output_tokens,
        }

        started = time.perf_counter()
        response = self._client.chat(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            think=self.think,
            options=options,
        )
        latency = time.perf_counter() - started

        message = self._get(response, "message", {})
        if isinstance(message, dict):
            text = message.get("content", "") or ""
            thinking = message.get("thinking")
        else:
            text = getattr(message, "content", "") or ""
            thinking = getattr(message, "thinking", None)

        usage = {
            "prompt_tokens": self._get(response, "prompt_eval_count"),
            "completion_tokens": self._get(response, "eval_count"),
        }
        provider_metadata = {
            "host": self.host,
            "num_ctx": self.num_ctx,
            "think": self.think,
            "total_duration_ns": self._get(response, "total_duration"),
            "load_duration_ns": self._get(response, "load_duration"),
            "prompt_eval_duration_ns": self._get(response, "prompt_eval_duration"),
            "eval_duration_ns": self._get(response, "eval_duration"),
            "done_reason": self._get(response, "done_reason"),
        }

        return GenerationResult(
            text=text,
            provider="ollama",
            model=model,
            latency_seconds=latency,
            usage=usage,
            provider_metadata=provider_metadata,
            thinking=thinking,
        )


class GeminiProvider(LLMProvider):
    def __init__(self, *, api_key: str, timeout: float = 180.0) -> None:
        try:
            from google import genai
            from google.genai import types
        except ImportError as exc:
            raise RuntimeError(
                "The package 'google-genai' is not installed. "
                "Run: python -m pip install -U google-genai"
            ) from exc

        if not api_key:
            raise RuntimeError("GEMINI_API_KEY is missing.")

        self._genai = genai
        self._types = types

        # The SDK handles HTTP transport internally. We keep timeout in metadata
        # because SDK timeout configuration can differ between releases.
        self.timeout = timeout
        self._client = genai.Client(api_key=api_key)

    @staticmethod
    def _usage_to_dict(usage: Any) -> dict[str, Any]:
        if usage is None:
            return {}

        result = {}
        fields = {
            "prompt_token_count": "prompt_tokens",
            "candidates_token_count": "completion_tokens",
            "total_token_count": "total_tokens",
            "thoughts_token_count": "thoughts_tokens",
        }
        for src, dst in fields.items():
            value = getattr(usage, src, None)
            if value is not None:
                result[dst] = value
        return result

    def generate(
        self,
        *,
        model: str,
        system_prompt: str,
        user_prompt: str,
        temperature: float,
        max_output_tokens: int,
    ) -> GenerationResult:
        config = self._types.GenerateContentConfig(
            system_instruction=system_prompt,
            temperature=temperature,
            max_output_tokens=max_output_tokens,
        )

        started = time.perf_counter()
        response = self._client.models.generate_content(
            model=model,
            contents=user_prompt,
            config=config,
        )
        latency = time.perf_counter() - started

        text = getattr(response, "text", None) or ""
        usage = self._usage_to_dict(getattr(response, "usage_metadata", None))

        provider_metadata: dict[str, Any] = {
            "timeout_seconds_recorded": self.timeout,
        }

        # Keep only compact, reproducibility-relevant response metadata.
        candidates = getattr(response, "candidates", None)
        if candidates:
            candidate = candidates[0]
            finish_reason = getattr(candidate, "finish_reason", None)
            if finish_reason is not None:
                provider_metadata["finish_reason"] = str(finish_reason)

        return GenerationResult(
            text=text,
            provider="gemini",
            model=model,
            latency_seconds=latency,
            usage=usage,
            provider_metadata=provider_metadata,
            thinking=None,
        )


def create_provider(
    provider_name: str,
    *,
    ollama_host: str,
    timeout: float,
    num_ctx: int,
    gemini_api_key: str | None,
    ollama_think: bool = False,
) -> LLMProvider:
    provider_name = provider_name.lower()

    if provider_name == "ollama":
        return OllamaProvider(
            host=ollama_host,
            timeout=timeout,
            num_ctx=num_ctx,
            think=ollama_think,
        )

    if provider_name == "gemini":
        return GeminiProvider(
            api_key=gemini_api_key or "",
            timeout=timeout,
        )

    raise ValueError(f"Unsupported provider: {provider_name}")