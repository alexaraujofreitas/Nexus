# ============================================================
# NEXUS TRADER — FinBERT News NLP Pipeline
#
# Local FinBERT sentiment analysis with graceful fallbacks:
# 1. ProsusAI/finbert (transformer-based)
# 2. VADER (rule-based, lightweight)
# 3. Keyword scoring (last resort)
#
# Returns sentiment scores in [-1, +1] range.
# ============================================================
from __future__ import annotations

import logging
import threading
from typing import Optional

logger = logging.getLogger(__name__)

# Keyword-based scoring fallback
_BULLISH_KEYWORDS = [
    "bull", "surge", "rally", "breakout", "ath",
    "adoption", "etf", "institutional", "buy", "long",
]
_BEARISH_KEYWORDS = [
    "bear", "crash", "dump", "hack", "ban",
    "regulation", "sell", "short", "fear", "liquidation",
]


class FinBERTPipeline:
    """
    Local FinBERT sentiment analysis pipeline for crypto news.

    Lazy-loads the transformer model on first use.
    Falls back to VADER if transformers unavailable.
    Falls back to keyword-based scoring if VADER unavailable.

    Returns sentiment score in [-1, +1] range:
    - positive = bullish
    - negative = bearish
    """

    # Class-level singleton state (thread-safe lazy loading)
    _model = None
    _vader_analyzer = None
    _model_lock = threading.Lock()
    _model_loaded = False
    _vader_loaded = False

    def __init__(self, model_name: str = "ProsusAI/finbert", device: str = "cpu"):
        """
        Initialize FinBERT pipeline.

        Parameters
        ----------
        model_name : str
            Hugging Face model identifier.
        device : str
            "cpu" or "cuda"
        """
        self.model_name = model_name
        self.device = device
        self._backend: str | None = None  # "finbert" | "vader" | "keyword"

    def analyze(self, texts: list[str]) -> list[dict]:
        """
        Analyze sentiment of multiple texts.

        Parameters
        ----------
        texts : list[str]
            Texts to analyze.

        Returns
        -------
        list[dict]
            List of dicts: {
                "text": str,
                "sentiment": "positive" | "negative" | "neutral",
                "score": float (in [-1, +1]),
                "confidence": float (in [0, 1])
            }
        """
        if not texts:
            return []

        # Ensure we have a backend available
        if not self.is_available():
            logger.warning("FinBERTPipeline: no backends available")
            return []

        results = []

        if self._backend == "finbert":
            results = self._analyze_with_finbert(texts)
        elif self._backend == "vader":
            results = self._analyze_with_vader(texts)
        else:  # keyword
            results = self._analyze_with_keywords(texts)

        return results

    def aggregate_sentiment(
        self, texts: list[str], max_texts: int = 20
    ) -> dict:
        """
        Aggregate multiple headlines into a single market sentiment signal.

        Parameters
        ----------
        texts : list[str]
            Headline texts to aggregate.
        max_texts : int
            Maximum number of texts to consider.

        Returns
        -------
        dict
            {
                "net_score": float (in [-1, +1]),
                "bullish_pct": float (0-100),
                "bearish_pct": float (0-100),
                "neutral_pct": float (0-100),
                "headline_count": int,
                "confidence": float (0-1)
            }
        """
        if not texts:
            return {
                "net_score": 0.0,
                "bullish_pct": 0.0,
                "bearish_pct": 0.0,
                "neutral_pct": 0.0,
                "headline_count": 0,
                "confidence": 0.0,
            }

        # Limit to max_texts
        texts_to_analyze = texts[:max_texts]

        # Analyze
        sentiments = self.analyze(texts_to_analyze)
        if not sentiments:
            return {
                "net_score": 0.0,
                "bullish_pct": 0.0,
                "bearish_pct": 0.0,
                "neutral_pct": 0.0,
                "headline_count": len(texts_to_analyze),
                "confidence": 0.0,
            }

        # Categorize
        bullish_count = 0
        bearish_count = 0
        neutral_count = 0
        confidence_sum = 0.0

        for sent in sentiments:
            sent_type = sent["sentiment"]
            if sent_type == "positive":
                bullish_count += 1
            elif sent_type == "negative":
                bearish_count += 1
            else:
                neutral_count += 1
            confidence_sum += sent["confidence"]

        total = len(sentiments)
        bullish_pct = (bullish_count / total * 100.0) if total > 0 else 0.0
        bearish_pct = (bearish_count / total * 100.0) if total > 0 else 0.0
        neutral_pct = (neutral_count / total * 100.0) if total > 0 else 0.0

        # Net score: average of scores weighted by confidence
        net_score = (
            sum(s["score"] * s["confidence"] for s in sentiments)
            / (confidence_sum + 1e-9)
        )
        net_score = max(-1.0, min(1.0, net_score))

        avg_confidence = confidence_sum / total if total > 0 else 0.0

        return {
            "net_score": round(net_score, 4),
            "bullish_pct": round(bullish_pct, 2),
            "bearish_pct": round(bearish_pct, 2),
            "neutral_pct": round(neutral_pct, 2),
            "headline_count": total,
            "confidence": round(avg_confidence, 4),
        }

    def is_available(self) -> bool:
        """
        Check if any sentiment backend is loaded and ready.
        Lazy-loads FinBERT, VADER, or enables keyword fallback.
        """
        with self._model_lock:
            # Try FinBERT if not yet attempted
            if not self._model_loaded:
                try:
                    self._load_finbert()
                    self._backend = "finbert"
                    self._model_loaded = True
                    logger.info("FinBERTPipeline: loaded FinBERT backend")
                    return True
                except Exception as e:
                    logger.debug(f"FinBERTPipeline: FinBERT load failed: {e}")
                    self._model_loaded = True

            # Try VADER if not yet attempted
            if not self._vader_loaded:
                try:
                    self._load_vader()
                    self._backend = "vader"
                    self._vader_loaded = True
                    logger.info("FinBERTPipeline: loaded VADER backend")
                    return True
                except Exception as e:
                    logger.debug(f"FinBERTPipeline: VADER load failed: {e}")
                    self._vader_loaded = True

            # Fall back to keyword-based
            if not self._backend:
                self._backend = "keyword"
                logger.info("FinBERTPipeline: using keyword-based fallback")
                return True

        return self._backend is not None

    # ── Private: FinBERT loader and analyzer ─────────────────────

    @classmethod
    def _load_finbert(cls) -> None:
        """Load FinBERT model from local cache (no network check).

        Sets HF_HUB_OFFLINE=1 and local_files_only=True to prevent
        huggingface_hub from making blocking HTTP requests to check
        for model updates. The model is already cached locally (~438 MB).
        This eliminates the 10-30s TLS hang on startup.
        """
        import os
        os.environ["HF_HUB_OFFLINE"] = "1"

        try:
            from transformers import pipeline as hf_pipeline
        except ImportError:
            raise ImportError("transformers library not installed")

        try:
            cls._model = hf_pipeline(
                "text-classification",
                model="ProsusAI/finbert",
                device=("cuda" if "cuda" in cls().device else -1),
            )
        except OSError:
            # Model not cached yet — fall back to network download
            logger.warning("FinBERT not in local cache, downloading (one-time)...")
            os.environ.pop("HF_HUB_OFFLINE", None)
            cls._model = hf_pipeline(
                "text-classification",
                model="ProsusAI/finbert",
                device=("cuda" if "cuda" in cls().device else -1),
            )

    def _analyze_with_finbert(self, texts: list[str]) -> list[dict]:
        """Analyze using FinBERT."""
        if self._model is None:
            return []

        results = []
        for text in texts:
            try:
                preds = self._model(text[:512])  # Limit to 512 tokens
                if preds:
                    pred = preds[0]
                    label = pred["label"].lower()
                    score_raw = float(pred["score"])

                    # Map FinBERT labels to sentiment
                    if label in ["positive", "1"]:
                        sentiment = "positive"
                        score = score_raw  # 0–1 → map to 0–1
                    elif label in ["negative", "0"]:
                        sentiment = "negative"
                        score = -score_raw  # 0–1 → map to -1–0
                    else:
                        sentiment = "neutral"
                        score = 0.0

                    results.append({
                        "text": text,
                        "sentiment": sentiment,
                        "score": round(score, 4),
                        "confidence": round(score_raw, 4),
                    })
            except Exception as e:
                logger.debug(f"FinBERTPipeline: FinBERT analysis failed: {e}")
                # Fall back to VADER for this text
                if self._vader_analyzer:
                    results.extend(self._analyze_with_vader([text]))

        return results

    # ── Private: VADER loader and analyzer ───────────────────────

    @classmethod
    def _load_vader(cls) -> None:
        """Load VADER sentiment analyzer."""
        try:
            from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
        except ImportError:
            raise ImportError("vaderSentiment library not installed")

        cls._vader_analyzer = SentimentIntensityAnalyzer()

    def _analyze_with_vader(self, texts: list[str]) -> list[dict]:
        """Analyze using VADER."""
        if self._vader_analyzer is None:
            return []

        results = []
        for text in texts:
            try:
                scores = self._vader_analyzer.polarity_scores(text)
                compound = float(scores.get("compound", 0.0))

                # Classify by compound score
                if compound > 0.05:
                    sentiment = "positive"
                elif compound < -0.05:
                    sentiment = "negative"
                else:
                    sentiment = "neutral"

                # Confidence: magnitude of compound score
                confidence = min(1.0, abs(compound))

                results.append({
                    "text": text,
                    "sentiment": sentiment,
                    "score": round(compound, 4),
                    "confidence": round(confidence, 4),
                })
            except Exception as e:
                logger.debug(f"FinBERTPipeline: VADER analysis failed: {e}")

        return results

    # ── Private: Keyword fallback ────────────────────────────────

    @staticmethod
    def _analyze_with_keywords(texts: list[str]) -> list[dict]:
        """Analyze using keyword-based scoring (last resort)."""
        results = []

        for text in texts:
            text_lower = text.lower()
            words = text_lower.split()

            bullish = sum(1 for w in words if w in _BULLISH_KEYWORDS)
            bearish = sum(1 for w in words if w in _BEARISH_KEYWORDS)
            total = max(len(words), 1)

            # Score = (bullish - bearish) / total * 2, clamped to [-1, 1]
            score = (bullish - bearish) / total * 2.0
            score = max(-1.0, min(1.0, score))

            if score > 0.1:
                sentiment = "positive"
            elif score < -0.1:
                sentiment = "negative"
            else:
                sentiment = "neutral"

            # Confidence: based on keyword density
            keyword_density = (bullish + bearish) / total
            confidence = min(1.0, keyword_density * 1.5)

            results.append({
                "text": text,
                "sentiment": sentiment,
                "score": round(score, 4),
                "confidence": round(confidence, 4),
            })

        return results
