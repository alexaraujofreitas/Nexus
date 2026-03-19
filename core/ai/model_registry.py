# ============================================================
# NEXUS TRADER — AI Model Registry
#
# Central registry for per-agent AI model selection.
# Agents register their preferred model/provider here.
# The registry persists settings to config and allows runtime
# hot-swap of models without restarting the agent.
#
# Supported backends:
#   - finbert     : ProsusAI/finbert (local HuggingFace)
#   - distilbert  : distilbert-base (local HuggingFace, faster)
#   - vader       : rule-based VADER (no GPU required, always available)
#   - openai      : OpenAI GPT-4o / GPT-3.5 (API key required)
#   - claude      : Anthropic Claude (API key required)
#   - gemini      : Google Gemini (API key required)
#   - ollama      : Local Ollama server (e.g. llama3, mistral)
#
# Default assignments match current implementation state.
# Agents that previously hard-coded FinBERT now query this registry.
# ============================================================
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


# ── Model descriptor ──────────────────────────────────────────
@dataclass
class ModelConfig:
    """Configuration for a single AI model assignment."""
    provider: str        # "finbert" | "vader" | "openai" | "claude" | "gemini" | "ollama"
    model_name: str      # e.g. "ProsusAI/finbert", "gpt-4o", "claude-opus-4-6"
    temperature: float   = 0.1
    max_tokens: int      = 512
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "provider":    self.provider,
            "model_name":  self.model_name,
            "temperature": self.temperature,
            "max_tokens":  self.max_tokens,
            "extra":       self.extra,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ModelConfig":
        return cls(
            provider    = d.get("provider", "vader"),
            model_name  = d.get("model_name", "vader"),
            temperature = float(d.get("temperature", 0.1)),
            max_tokens  = int(d.get("max_tokens", 512)),
            extra       = d.get("extra", {}),
        )


# ── Default per-agent assignments ────────────────────────────
_DEFAULTS: Dict[str, ModelConfig] = {
    # Text-based sentiment agents — FinBERT when available, VADER fallback
    "news":                 ModelConfig("finbert",  "ProsusAI/finbert"),
    "social_sentiment":     ModelConfig("vader",    "vader"),
    "geopolitical":         ModelConfig("vader",    "vader"),
    "twitter":              ModelConfig("vader",    "vader"),
    "reddit":               ModelConfig("vader",    "vader"),
    "telegram":             ModelConfig("vader",    "vader"),
    "narrative_shift":      ModelConfig("finbert",  "ProsusAI/finbert"),
    # Quantitative agents — no LLM needed; use rule-based
    "onchain":              ModelConfig("rule",     "rule-based"),
    "whale":                ModelConfig("rule",     "rule-based"),
    "stablecoin":           ModelConfig("rule",     "rule-based"),
    "miner_flow":           ModelConfig("rule",     "rule-based"),
    "options_flow":         ModelConfig("rule",     "rule-based"),
    "liquidation_flow":     ModelConfig("rule",     "rule-based"),
    "squeeze_detection":    ModelConfig("rule",     "rule-based"),
    "leverage_crowding":    ModelConfig("rule",     "rule-based"),
    "liquidity_vacuum":     ModelConfig("rule",     "rule-based"),
    "position_monitor":     ModelConfig("rule",     "rule-based"),
    "scalp":                ModelConfig("rule",     "rule-based"),
    # Market-structure agents
    "funding_rate":         ModelConfig("rule",     "rule-based"),
    "order_book":           ModelConfig("rule",     "rule-based"),
    "volatility_surface":   ModelConfig("rule",     "rule-based"),
    "sector_rotation":      ModelConfig("vader",    "vader"),
    "macro":                ModelConfig("rule",     "rule-based"),
    "crash_detection":      ModelConfig("rule",     "rule-based"),
}


class ModelRegistry:
    """
    Thread-safe registry for per-agent AI model configuration.

    Usage:
        registry = get_model_registry()
        cfg = registry.get("news")          # ModelConfig for news agent
        registry.set("news", ModelConfig("openai", "gpt-4o"))
        scorer = registry.get_scorer("news")  # ready-to-use scorer
    """

    def __init__(self):
        self._lock    = threading.RLock()
        self._configs: Dict[str, ModelConfig] = dict(_DEFAULTS)
        self._scorers: Dict[str, Any]          = {}  # cached scorer instances
        self._load_from_settings()

    # ── Config API ────────────────────────────────────────────

    def get(self, agent_name: str) -> ModelConfig:
        """Return the ModelConfig for *agent_name* (falls back to vader)."""
        with self._lock:
            return self._configs.get(agent_name, ModelConfig("vader", "vader"))

    def set(self, agent_name: str, config: ModelConfig) -> None:
        """Update the config for *agent_name* and invalidate its cached scorer."""
        with self._lock:
            self._configs[agent_name] = config
            self._scorers.pop(agent_name, None)   # force re-build
        self._save_to_settings()
        logger.info(
            "ModelRegistry: %s → %s/%s",
            agent_name, config.provider, config.model_name,
        )
        try:
            from core.event_bus import bus, Topics
            bus.publish(
                Topics.MODEL_SELECTED,
                {"agent": agent_name, **config.to_dict()},
                source="model_registry",
            )
        except Exception:
            pass

    def get_all(self) -> Dict[str, ModelConfig]:
        """Return a copy of all current assignments."""
        with self._lock:
            return dict(self._configs)

    # ── Scorer API ────────────────────────────────────────────

    def get_scorer(self, agent_name: str):
        """
        Return a ready-to-use scorer object for *agent_name*.

        Scorers are cached — only the first call instantiates the model.
        Returns a TextScorer instance with a .score(texts: list[str]) method
        that returns list[tuple[float, float]] (signal, confidence).
        """
        with self._lock:
            if agent_name in self._scorers:
                return self._scorers[agent_name]
        cfg = self.get(agent_name)
        scorer = self._build_scorer(cfg)
        with self._lock:
            self._scorers[agent_name] = scorer
        return scorer

    # ── Internal ──────────────────────────────────────────────

    def _build_scorer(self, cfg: ModelConfig):
        """Instantiate a scorer based on provider."""
        if cfg.provider == "finbert":
            return _FinBERTScorer(cfg.model_name)
        elif cfg.provider == "distilbert":
            return _DistilBERTScorer(cfg.model_name)
        elif cfg.provider in ("openai", "claude", "gemini"):
            return _LLMScorer(cfg)
        elif cfg.provider == "ollama":
            return _OllamaScorer(cfg)
        else:
            return _VaderScorer()

    def _load_from_settings(self) -> None:
        """Load persisted per-agent model assignments from settings."""
        try:
            from config.settings import settings
            saved = settings.get("ai", {}).get("agent_models", {})
            for agent, raw in saved.items():
                if isinstance(raw, dict):
                    self._configs[agent] = ModelConfig.from_dict(raw)
        except Exception as exc:
            logger.debug("ModelRegistry: settings load failed: %s", exc)

    def _save_to_settings(self) -> None:
        """Persist current assignments to settings."""
        try:
            from config.settings import settings
            with self._lock:
                data = {k: v.to_dict() for k, v in self._configs.items()}
            ai_section = settings.get("ai", {})
            ai_section["agent_models"] = data
            settings["ai"] = ai_section
        except Exception as exc:
            logger.debug("ModelRegistry: settings save failed: %s", exc)


# ── Scorer implementations ────────────────────────────────────

class _VaderScorer:
    """VADER + crypto-boosted sentiment scorer (always available)."""

    def __init__(self):
        try:
            from nltk.sentiment.vader import SentimentIntensityAnalyzer
            import nltk
            try:
                nltk.data.find("sentiment/vader_lexicon.zip")
            except LookupError:
                nltk.download("vader_lexicon", quiet=True)
            self._sia = SentimentIntensityAnalyzer()
            _CRYPTO_BOOST = {
                "moon": 2.5, "mooning": 3.0, "bullish": 2.0, "bearish": -2.0,
                "dump": -2.5, "rekt": -3.0, "rug": -3.5, "pump": 1.5,
                "fud": -1.5, "fomo": 1.5, "hodl": 1.0, "ath": 2.0,
                "crash": -3.0, "capitulation": -2.5, "breakout": 2.0,
                "accumulation": 1.5, "distribution": -1.5, "liquidation": -2.5,
                "ban": -2.5, "hack": -3.5, "exploit": -3.5, "depeg": -3.0,
            }
            self._sia.lexicon.update(_CRYPTO_BOOST)
        except Exception:
            self._sia = None

    def score(self, texts: list[str]) -> list[tuple[float, float]]:
        if not self._sia or not texts:
            return [(0.0, 0.0)] * len(texts)
        results = []
        for text in texts:
            scores = self._sia.polarity_scores(str(text))
            compound = scores.get("compound", 0.0)
            signal = compound  # [-1, 1]
            confidence = min(abs(compound) * 1.2, 1.0)
            results.append((signal, confidence))
        return results


class _FinBERTScorer:
    """ProsusAI/finbert sentiment scorer (lazy HuggingFace load)."""

    def __init__(self, model_name: str = "ProsusAI/finbert"):
        self._model_name = model_name
        self._pipeline   = None
        self._fallback   = _VaderScorer()
        self._load_attempted = False

    def _ensure_loaded(self):
        if self._load_attempted:
            return
        self._load_attempted = True
        try:
            from transformers import pipeline
            self._pipeline = pipeline(
                "sentiment-analysis",
                model=self._model_name,
                truncation=True,
                max_length=512,
                device=-1,   # CPU
            )
            logger.info("ModelRegistry: FinBERT loaded (%s)", self._model_name)
        except Exception as exc:
            logger.warning("ModelRegistry: FinBERT load failed — falling back to VADER: %s", exc)

    def score(self, texts: list[str]) -> list[tuple[float, float]]:
        self._ensure_loaded()
        if not self._pipeline:
            return self._fallback.score(texts)
        results = []
        for text in texts:
            try:
                res = self._pipeline(str(text)[:512])
                label = res[0]["label"].lower()
                conf  = float(res[0]["score"])
                sig   = conf if label == "positive" else (-conf if label == "negative" else 0.0)
                results.append((sig, conf))
            except Exception:
                results.append((0.0, 0.0))
        return results


class _DistilBERTScorer:
    """distilbert-base-uncased-finetuned-sst-2-english (fast, general sentiment)."""

    def __init__(self, model_name: str = "distilbert-base-uncased-finetuned-sst-2-english"):
        self._model_name = model_name
        self._pipeline   = None
        self._fallback   = _VaderScorer()
        self._load_attempted = False

    def _ensure_loaded(self):
        if self._load_attempted:
            return
        self._load_attempted = True
        try:
            from transformers import pipeline
            self._pipeline = pipeline(
                "sentiment-analysis",
                model=self._model_name,
                truncation=True,
                max_length=512,
                device=-1,
            )
        except Exception as exc:
            logger.warning("ModelRegistry: DistilBERT load failed: %s", exc)

    def score(self, texts: list[str]) -> list[tuple[float, float]]:
        self._ensure_loaded()
        if not self._pipeline:
            return self._fallback.score(texts)
        results = []
        for text in texts:
            try:
                res = self._pipeline(str(text)[:512])
                label = res[0]["label"].lower()
                conf  = float(res[0]["score"])
                sig   = conf if "pos" in label else -conf
                results.append((sig, conf))
            except Exception:
                results.append((0.0, 0.0))
        return results


class _LLMScorer:
    """Uses OpenAI / Claude / Gemini API for sentiment scoring."""

    _SYSTEM = (
        "You are a crypto market sentiment analyst. "
        "For each text, output exactly one JSON object: "
        '{\"signal\": <float -1 to 1>, \"confidence\": <float 0 to 1>}. '
        "signal: 1=strongly bullish, -1=strongly bearish, 0=neutral."
    )

    def __init__(self, cfg: ModelConfig):
        self._cfg = cfg

    def score(self, texts: list[str]) -> list[tuple[float, float]]:
        import json as _json
        results = []
        for text in texts:
            try:
                resp = self._call_api(str(text)[:1000])
                parsed = _json.loads(resp)
                sig  = float(parsed.get("signal", 0.0))
                conf = float(parsed.get("confidence", 0.5))
                results.append((max(-1.0, min(1.0, sig)), max(0.0, min(1.0, conf))))
            except Exception:
                results.append((0.0, 0.0))
        return results

    def _call_api(self, text: str) -> str:
        cfg = self._cfg
        if cfg.provider == "openai":
            import openai
            from core.security.key_vault import key_vault
            key = key_vault.load("openai_api_key") or ""
            client = openai.OpenAI(api_key=key)
            resp = client.chat.completions.create(
                model=cfg.model_name,
                messages=[{"role": "user", "content": text}],
                response_format={"type": "json_object"},
                max_tokens=64,
                temperature=cfg.temperature,
            )
            return resp.choices[0].message.content
        elif cfg.provider == "claude":
            import anthropic
            from core.security.key_vault import key_vault
            key = key_vault.load("anthropic_api_key") or ""
            client = anthropic.Anthropic(api_key=key)
            msg = client.messages.create(
                model=cfg.model_name,
                max_tokens=64,
                system=self._SYSTEM,
                messages=[{"role": "user", "content": text}],
            )
            return msg.content[0].text
        else:
            raise NotImplementedError(f"LLMScorer: unsupported provider {cfg.provider}")


class _OllamaScorer:
    """Local Ollama server scorer."""

    def __init__(self, cfg: ModelConfig):
        self._cfg = cfg
        host = cfg.extra.get("host", "http://localhost:11434")
        self._url = f"{host}/api/generate"

    def score(self, texts: list[str]) -> list[tuple[float, float]]:
        import json as _json, urllib.request
        results = []
        for text in texts:
            try:
                prompt = (
                    f"Analyze sentiment of this crypto text. "
                    f"Reply only with JSON {{\"signal\": float, \"confidence\": float}}.\n\n{text[:500]}"
                )
                payload = _json.dumps({
                    "model": self._cfg.model_name,
                    "prompt": prompt,
                    "stream": False,
                    "options": {"temperature": self._cfg.temperature},
                }).encode()
                req = urllib.request.Request(
                    self._url,
                    data=payload,
                    headers={"Content-Type": "application/json"},
                )
                with urllib.request.urlopen(req, timeout=15) as resp:
                    body = _json.loads(resp.read().decode())
                parsed = _json.loads(body.get("response", "{}"))
                sig  = float(parsed.get("signal", 0.0))
                conf = float(parsed.get("confidence", 0.5))
                results.append((max(-1.0, min(1.0, sig)), max(0.0, min(1.0, conf))))
            except Exception:
                results.append((0.0, 0.0))
        return results


# ── Module-level singleton ────────────────────────────────────
_registry: Optional[ModelRegistry] = None
_registry_lock = threading.Lock()


def get_model_registry() -> ModelRegistry:
    """Return the global ModelRegistry singleton."""
    global _registry
    if _registry is None:
        with _registry_lock:
            if _registry is None:
                _registry = ModelRegistry()
    return _registry
