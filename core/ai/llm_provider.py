# ============================================================
# NEXUS TRADER — LLM Provider Abstraction
# Supports Anthropic Claude, OpenAI, and Google Gemini.
# ============================================================

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Iterator, Optional

logger = logging.getLogger(__name__)


@dataclass
class LLMMessage:
    """A single conversation message."""
    role: str     # "user" | "assistant"
    content: str


class LLMProvider(ABC):
    """Abstract base for all LLM providers."""

    @abstractmethod
    def stream_chat(
        self,
        messages: list[LLMMessage],
        system_prompt: str,
        max_tokens: int = 8192,
    ) -> Iterator[str]:
        """Yield text chunks as they stream from the model."""
        ...

    @property
    @abstractmethod
    def model_name(self) -> str:
        """Return the model identifier string."""
        ...

    @property
    @abstractmethod
    def provider_name(self) -> str:
        """Return a human-readable provider name."""
        ...


# ── Anthropic Claude ──────────────────────────────────────────
class ClaudeProvider(LLMProvider):
    """Anthropic Claude provider using the official SDK."""

    def __init__(self, api_key: str, model: str = "claude-opus-4-6"):
        import anthropic  # noqa: PLC0415 — lazy import
        self._client = anthropic.Anthropic(api_key=api_key)
        self._model  = model

    @property
    def model_name(self) -> str:
        return self._model

    @property
    def provider_name(self) -> str:
        return "Anthropic"

    def stream_chat(
        self,
        messages: list[LLMMessage],
        system_prompt: str,
        max_tokens: int = 8192,   # raised from 2048 — complex strategies need 3-6k tokens
    ) -> Iterator[str]:
        api_messages = [
            {"role": m.role, "content": m.content}
            for m in messages
            if m.role in ("user", "assistant")
        ]
        with self._client.messages.stream(
            model=self._model,
            max_tokens=max_tokens,
            system=system_prompt,
            messages=api_messages,
        ) as stream:
            for text in stream.text_stream:
                yield text


# ── OpenAI ───────────────────────────────────────────────────
class OpenAIProvider(LLMProvider):
    """OpenAI ChatCompletion provider."""

    def __init__(self, api_key: str, model: str = "gpt-4o"):
        import openai  # noqa: PLC0415 — lazy import
        self._client = openai.OpenAI(api_key=api_key)
        self._model  = model

    @property
    def model_name(self) -> str:
        return self._model

    @property
    def provider_name(self) -> str:
        return "OpenAI"

    def stream_chat(
        self,
        messages: list[LLMMessage],
        system_prompt: str,
        max_tokens: int = 8192,   # raised from 2048 — complex strategies need 3-6k tokens
    ) -> Iterator[str]:
        api_messages = [{"role": "system", "content": system_prompt}]
        for m in messages:
            if m.role in ("user", "assistant"):
                api_messages.append({"role": m.role, "content": m.content})

        stream = self._client.chat.completions.create(
            model=self._model,
            messages=api_messages,
            max_tokens=max_tokens,
            stream=True,
        )
        for chunk in stream:
            delta = chunk.choices[0].delta
            if delta.content:
                yield delta.content


# ── Local Ollama ──────────────────────────────────────────────
class OllamaProvider(LLMProvider):
    """
    Local Ollama provider — zero API cost, runs entirely on-device.
    Ollama exposes an OpenAI-compatible endpoint at http://localhost:11434/v1
    so we reuse the openai SDK with a custom base_url.

    GPU notes (RTX 4070 — 12 GB VRAM):
    - deepseek-r1:14b occupies ~9 GB at fp16.  The KV cache for the context
      window consumes additional VRAM.  num_ctx=32768 needed ~3–4 GB extra,
      pushing the total past 12 GB and forcing CPU fallback (GPU util → 1%).
    - num_ctx=4096 limits KV cache to ~400 MB, keeping everything on GPU and
      reducing inference from minutes → seconds.
    - The strategy optimizer uses larger prompts; callers that need more context
      can pass num_ctx explicitly via the extra_body kwarg override.
    - deepseek-r1 models emit <think>…</think> chains before answering.
      Keeping num_ctx small naturally bounds how long those chains grow.
    - A 90-second read timeout ensures a stuck LLM call never blocks a scan.
    """

    # Context window sent to Ollama.  4096 is sufficient for all NexusTrader
    # prompts (scan rationale ≈ 300 tokens, RLMF feedback ≈ 500 tokens) and
    # keeps the KV-cache well within the RTX 4070's 12 GB VRAM.
    _DEFAULT_NUM_CTX   = 4096
    # Hard wall on generated tokens for normal calls.  Callers that need more
    # (e.g. strategy optimizer) should pass max_tokens explicitly.
    _DEFAULT_MAX_TOKENS = 512
    # Seconds to wait for the first token; prevents a hung model from stalling
    # the scan thread indefinitely.
    _READ_TIMEOUT_S    = 90

    def __init__(self, model: str = "qwen2.5:14b",
                 base_url: str = "http://localhost:11434/v1"):
        import openai, httpx  # noqa: PLC0415 — lazy imports
        # Use a custom httpx client so we can set an explicit read timeout.
        # The default openai client has no timeout, which caused 5–10 min hangs
        # when Ollama ran on CPU due to the oversized context window.
        http_client = httpx.Client(timeout=httpx.Timeout(
            connect=10.0, read=self._READ_TIMEOUT_S, write=30.0, pool=5.0
        ))
        self._client   = openai.OpenAI(
            api_key="ollama", base_url=base_url, http_client=http_client
        )
        self._model    = model
        self._base_url = base_url

    @property
    def model_name(self) -> str:
        return self._model

    @property
    def provider_name(self) -> str:
        return "Ollama (Local)"

    def stream_chat(
        self,
        messages: list[LLMMessage],
        system_prompt: str,
        max_tokens: int = _DEFAULT_MAX_TOKENS,
        num_ctx: int    = _DEFAULT_NUM_CTX,
    ) -> Iterator[str]:
        api_messages = [{"role": "system", "content": system_prompt}]
        for m in messages:
            if m.role in ("user", "assistant"):
                api_messages.append({"role": m.role, "content": m.content})

        stream = self._client.chat.completions.create(
            model=self._model,
            messages=api_messages,
            max_tokens=max_tokens,
            stream=True,
            extra_body={"options": {"num_ctx": num_ctx}},
        )
        for chunk in stream:
            delta = chunk.choices[0].delta
            if delta.content:
                yield delta.content


# ── Google Gemini ──────────────────────────────────────────────
class GeminiProvider(LLMProvider):
    """
    Google Gemini provider using the official google-genai SDK (v1+).
    System instructions and multi-turn messages are both supported.
    """

    def __init__(self, api_key: str, model: str = "gemini-2.0-flash"):
        from google import genai as _genai          # noqa: PLC0415 — lazy import
        self._client = _genai.Client(api_key=api_key)
        self._model  = model

    @property
    def model_name(self) -> str:
        return self._model

    @property
    def provider_name(self) -> str:
        return "Google"

    def stream_chat(
        self,
        messages: list[LLMMessage],
        system_prompt: str,
        max_tokens: int = 8192,
    ) -> Iterator[str]:
        from google.genai import types as _types    # noqa: PLC0415 — lazy import

        # Build contents list — Gemini uses "model" instead of "assistant"
        contents = []
        for m in messages:
            if m.role not in ("user", "assistant"):
                continue
            role = "model" if m.role == "assistant" else "user"
            contents.append(
                _types.Content(
                    role=role,
                    parts=[_types.Part(text=m.content)],
                )
            )

        config = _types.GenerateContentConfig(
            system_instruction=system_prompt,
            max_output_tokens=max_tokens,
        )

        for chunk in self._client.models.generate_content_stream(
            model=self._model,
            contents=contents,
            config=config,
        ):
            if chunk.text:
                yield chunk.text


# ── Factory ───────────────────────────────────────────────────
def get_provider() -> Optional[LLMProvider]:
    """
    Create the active LLM provider based on the user's selection in Settings.

    If the user chose a specific provider, that provider is used (as long as
    its API key is configured).  If they chose "Auto" (the default), the
    priority chain Anthropic → OpenAI → Google Gemini is used.

    Returns None if no matching key is configured.
    """
    from config.settings import settings  # noqa: PLC0415

    active = settings.get("ai.active_provider", "Auto (Anthropic → OpenAI → Gemini)").strip()

    # ── helpers ──────────────────────────────────────────────
    def _ollama() -> Optional[LLMProvider]:
        model    = settings.get("ai.ollama_model", "qwen2.5:14b").strip()
        base_url = settings.get("ai.ollama_url",   "http://localhost:11434/v1").strip()
        if not model:
            return None
        logger.debug("Using local Ollama provider: %s @ %s", model, base_url)
        return OllamaProvider(model=model, base_url=base_url)

    def _get_api_key(setting_key: str) -> str:
        """Vault-first API key lookup with YAML fallback (backward compat)."""
        try:
            from core.security.key_vault import key_vault
            vk = key_vault.load(setting_key).strip()
            if vk:
                return vk
        except Exception:
            pass
        # Fallback: plain-text YAML (pre-D3 installs or vault unavailable)
        raw = settings.get(setting_key, "").strip()
        return raw if raw not in ("__vault__", "") else ""

    def _claude() -> Optional[LLMProvider]:
        key = _get_api_key("ai.anthropic_api_key")
        if not key:
            return None
        model = settings.get("ai.anthropic_model", "claude-opus-4-6")
        logger.debug("Using Anthropic Claude provider: %s", model)
        return ClaudeProvider(api_key=key, model=model)

    def _openai() -> Optional[LLMProvider]:
        key = _get_api_key("ai.openai_api_key")
        if not key:
            return None
        model = settings.get("ai.openai_model", "gpt-4o")
        logger.debug("Using OpenAI provider: %s", model)
        return OpenAIProvider(api_key=key, model=model)

    def _gemini() -> Optional[LLMProvider]:
        key = _get_api_key("ai.gemini_api_key")
        if not key:
            return None
        model = settings.get("ai.gemini_model", "gemini-2.0-flash")
        logger.debug("Using Google Gemini provider: %s", model)
        return GeminiProvider(api_key=key, model=model)

    # ── explicit selection ────────────────────────────────────
    if active == "Anthropic Claude":
        provider = _claude()
        if provider:
            return provider
        logger.info("Anthropic Claude selected but no API key configured — AI features inactive")
        return None

    if active == "OpenAI":
        provider = _openai()
        if provider:
            return provider
        logger.info("OpenAI selected but no API key configured — AI features inactive")
        return None

    if active == "Google Gemini":
        provider = _gemini()
        if provider:
            return provider
        logger.info("Google Gemini selected but no API key configured — AI features inactive")
        return None

    if active == "Local (Ollama)":
        provider = _ollama()
        if provider:
            return provider
        logger.info("Local Ollama selected but could not connect — is Ollama running?")
        return None

    # ── auto / fallback chain ─────────────────────────────────
    for factory in (_claude, _openai, _gemini):
        provider = factory()
        if provider:
            return provider

    logger.info("No LLM API key configured — AI features inactive")
    return None
