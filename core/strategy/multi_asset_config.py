# ============================================================
# NEXUS TRADER — Multi-Asset Strategy Configuration (Phase 4c)
# ============================================================
# Per-asset strategy configuration for multi-asset trading.
#
# Allows each asset (BTC, ETH, SOL, etc.) to have independently
# tuned parameters: active strategies, position limits, risk multipliers,
# feature flags for experimental components (RL, FinBERT, HMM).
# ============================================================

from __future__ import annotations

import logging
from dataclasses import dataclass, field, asdict
from typing import Optional, Dict, List
from config.settings import settings

logger = logging.getLogger(__name__)


@dataclass
class AssetProfile:
    """
    Per-asset strategy configuration.

    Attributes:
        symbol: Trading pair symbol (e.g., 'BTC/USDT')
        active_strategies: List of strategy model names to use
        max_position_pct: Maximum position size as % of portfolio
        risk_multiplier: Scales position size (0.0 to 1.0+)
        min_confluence_score: Minimum signal confidence to trade
        enable_rl: Use Reinforcement Learning signals
        enable_finbert: Use sentiment analysis signals
        enable_hmm: Use Hidden Markov Model regime detection
        enable_websocket: Real-time candle feed via WebSocket
        notes: Human-readable notes about this asset's config
    """

    symbol: str
    active_strategies: List[str] = field(default_factory=list)
    max_position_pct: float = 0.05
    risk_multiplier: float = 1.0
    min_confluence_score: float = 0.55
    enable_rl: bool = False
    enable_finbert: bool = False
    enable_hmm: bool = False
    enable_websocket: bool = True
    notes: str = ""

    def validate(self) -> bool:
        """Validate profile parameters."""
        if not self.symbol:
            logger.warning("AssetProfile: symbol is empty")
            return False
        if self.max_position_pct <= 0 or self.max_position_pct > 1.0:
            logger.warning(f"AssetProfile {self.symbol}: max_position_pct out of range")
            return False
        if self.risk_multiplier <= 0:
            logger.warning(f"AssetProfile {self.symbol}: risk_multiplier must be positive")
            return False
        if self.min_confluence_score < 0 or self.min_confluence_score > 1.0:
            logger.warning(f"AssetProfile {self.symbol}: min_confluence_score out of range")
            return False
        return True

    def to_dict(self) -> dict:
        """Convert to dictionary."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> AssetProfile:
        """Create from dictionary."""
        return cls(**data)


class MultiAssetConfig:
    """
    Multi-asset strategy configuration manager.

    Manages per-asset profiles, loads/saves from settings, and provides
    convenience methods to query strategy parameters for any trading pair.
    """

    def __init__(self):
        """Initialize with default profiles or load from settings."""
        self._profiles: Dict[str, AssetProfile] = {}
        self._load_defaults()
        self._load_from_settings()

    def _load_defaults(self) -> None:
        """Load default profiles for major assets."""
        defaults = {
            "BTC/USDT": AssetProfile(
                symbol="BTC/USDT",
                active_strategies=[
                    "trend_following",
                    "mean_reversion",
                    "momentum",
                    "liquidity_sweep",
                    "statistical_arb",
                ],
                max_position_pct=0.10,
                risk_multiplier=1.0,
                min_confluence_score=0.55,
                enable_rl=True,
                enable_finbert=True,
                enable_hmm=True,
                enable_websocket=True,
                notes="Bitcoin: Full strategy suite with all features enabled",
            ),
            "ETH/USDT": AssetProfile(
                symbol="ETH/USDT",
                active_strategies=[
                    "trend_following",
                    "mean_reversion",
                    "momentum",
                    "liquidity_sweep",
                    "statistical_arb",
                ],
                max_position_pct=0.08,
                risk_multiplier=0.90,
                min_confluence_score=0.57,
                enable_rl=True,
                enable_finbert=True,
                enable_hmm=True,
                enable_websocket=True,
                notes="Ethereum: Slightly conservative than BTC",
            ),
            "SOL/USDT": AssetProfile(
                symbol="SOL/USDT",
                active_strategies=[
                    "trend_following",
                    "momentum",
                    "liquidity_sweep",
                ],
                max_position_pct=0.06,
                risk_multiplier=0.75,
                min_confluence_score=0.60,
                enable_rl=False,
                enable_finbert=False,
                enable_hmm=True,
                enable_websocket=True,
                notes="Solana: HMM regime detection only, select strategies",
            ),
            "BNB/USDT": AssetProfile(
                symbol="BNB/USDT",
                active_strategies=[
                    "trend_following",
                    "mean_reversion",
                ],
                max_position_pct=0.05,
                risk_multiplier=0.70,
                min_confluence_score=0.62,
                enable_rl=False,
                enable_finbert=False,
                enable_hmm=False,
                enable_websocket=True,
                notes="Binance Coin: Conservative, basic strategies only",
            ),
            "XRP/USDT": AssetProfile(
                symbol="XRP/USDT",
                active_strategies=[
                    "trend_following",
                    "mean_reversion",
                    "sentiment_analysis",
                ],
                max_position_pct=0.05,
                risk_multiplier=0.65,
                min_confluence_score=0.65,
                enable_rl=False,
                enable_finbert=True,
                enable_hmm=False,
                enable_websocket=True,
                notes="XRP: Sentiment-driven, FinBERT enabled",
            ),
        }

        # ── Mid-cap alt profiles (conservative defaults) ────────────────
        _midcap_alts = {
            "TRX/USDT":    ("Tron", 0.65),
            "DOGE/USDT":   ("Dogecoin", 0.60),
            "ADA/USDT":    ("Cardano", 0.65),
            "BCH/USDT":    ("Bitcoin Cash", 0.70),
            "HYPE/USDT":   ("Hyperliquid", 0.55),
            "LINK/USDT":   ("Chainlink", 0.70),
            "XLM/USDT":    ("Stellar", 0.60),
            "AVAX/USDT":   ("Avalanche", 0.70),
            "HBAR/USDT":   ("Hedera", 0.55),
            "SUI/USDT":    ("Sui", 0.65),
            "NEAR/USDT":   ("NEAR Protocol", 0.65),
            "ICP/USDT":    ("Internet Computer", 0.60),
            "ONDO/USDT":   ("Ondo Finance", 0.55),
            "ALGO/USDT":   ("Algorand", 0.60),
            "RENDER/USDT": ("Render", 0.65),
        }
        for sym, (name, risk_mult) in _midcap_alts.items():
            defaults[sym] = AssetProfile(
                symbol=sym,
                active_strategies=["trend_following", "momentum"],
                max_position_pct=0.05,
                risk_multiplier=risk_mult,
                min_confluence_score=0.60,
                enable_rl=False,
                enable_finbert=False,
                enable_hmm=True,
                enable_websocket=True,
                notes=f"{name}: Mid-cap alt, conservative risk settings",
            )

        for symbol, profile in defaults.items():
            if profile.validate():
                self._profiles[symbol] = profile
                logger.debug(f"Loaded default profile for {symbol}")
            else:
                logger.warning(f"Invalid default profile for {symbol}")

    def _load_from_settings(self) -> None:
        """Load or override profiles from settings."""
        try:
            profiles_data = settings.get("multi_asset.profiles", {})
            if isinstance(profiles_data, dict):
                for symbol, profile_data in profiles_data.items():
                    if isinstance(profile_data, dict):
                        try:
                            profile = AssetProfile.from_dict(profile_data)
                            if profile.validate():
                                self._profiles[symbol] = profile
                                logger.debug(f"Loaded profile for {symbol} from settings")
                            else:
                                logger.warning(f"Invalid profile in settings for {symbol}")
                        except Exception as e:
                            logger.warning(f"Could not load profile for {symbol}: {e}")
        except Exception as e:
            logger.debug(f"No multi_asset.profiles found in settings: {e}")

    def get_profile(self, symbol: str) -> AssetProfile:
        """
        Get profile for a symbol.

        Returns the specific profile if it exists, otherwise returns
        the default BTC/USDT profile for unknown symbols.

        Args:
            symbol: Trading pair symbol (e.g., 'BTC/USDT')

        Returns:
            AssetProfile for the symbol
        """
        if symbol in self._profiles:
            return self._profiles[symbol]

        # Fallback to BTC profile for unknown symbols
        logger.debug(f"No profile for {symbol}, using BTC/USDT defaults")
        default_btc = self._profiles.get("BTC/USDT")
        if default_btc:
            fallback = AssetProfile(
                symbol=symbol,
                active_strategies=default_btc.active_strategies.copy(),
                max_position_pct=default_btc.max_position_pct,
                risk_multiplier=default_btc.risk_multiplier,
                min_confluence_score=default_btc.min_confluence_score,
                enable_rl=default_btc.enable_rl,
                enable_finbert=default_btc.enable_finbert,
                enable_hmm=default_btc.enable_hmm,
                enable_websocket=default_btc.enable_websocket,
                notes=f"Fallback from BTC/USDT for {symbol}",
            )
            return fallback

        # Ultimate fallback
        logger.warning(f"No BTC/USDT profile found, creating minimal fallback for {symbol}")
        return AssetProfile(
            symbol=symbol,
            active_strategies=["trend_following"],
            max_position_pct=0.02,
            risk_multiplier=0.5,
            min_confluence_score=0.65,
            enable_rl=False,
            enable_finbert=False,
            enable_hmm=False,
            enable_websocket=True,
            notes=f"Minimal fallback for {symbol}",
        )

    def update_profile(self, symbol: str, **kwargs) -> None:
        """
        Update specific fields of a profile.

        Args:
            symbol: Trading pair symbol
            **kwargs: Fields to update (e.g., max_position_pct=0.15)
        """
        profile = self.get_profile(symbol)

        # Update allowed fields
        for key, value in kwargs.items():
            if hasattr(profile, key):
                setattr(profile, key, value)
            else:
                logger.warning(f"Unknown profile field: {key}")

        if profile.validate():
            self._profiles[symbol] = profile
            logger.info(f"Updated profile for {symbol}")
        else:
            logger.error(f"Profile validation failed for {symbol}")

    def get_active_strategies(self, symbol: str) -> List[str]:
        """Get list of active strategy names for a symbol."""
        profile = self.get_profile(symbol)
        return profile.active_strategies.copy()

    def should_enable_rl(self, symbol: str) -> bool:
        """Check if RL signals should be enabled for a symbol."""
        profile = self.get_profile(symbol)
        return profile.enable_rl

    def should_enable_finbert(self, symbol: str) -> bool:
        """Check if FinBERT sentiment signals should be enabled for a symbol."""
        profile = self.get_profile(symbol)
        return profile.enable_finbert

    def should_enable_hmm(self, symbol: str) -> bool:
        """Check if HMM regime detection should be enabled for a symbol."""
        profile = self.get_profile(symbol)
        return profile.enable_hmm

    def should_enable_websocket(self, symbol: str) -> bool:
        """Check if WebSocket real-time feed should be enabled for a symbol."""
        profile = self.get_profile(symbol)
        return profile.enable_websocket

    def get_risk_multiplier(self, symbol: str) -> float:
        """Get risk multiplier for a symbol (scales position size)."""
        profile = self.get_profile(symbol)
        return profile.risk_multiplier

    def get_max_position_pct(self, symbol: str) -> float:
        """Get maximum position size as % of portfolio for a symbol."""
        profile = self.get_profile(symbol)
        return profile.max_position_pct

    def get_min_confluence_score(self, symbol: str) -> float:
        """Get minimum confluence score threshold for a symbol.

        If the user has explicitly configured idss.min_confluence_score lower
        than the per-asset default, the user setting wins — it acts as a
        ceiling so that lowering the global threshold in Settings actually
        takes effect across all assets.
        """
        profile = self.get_profile(symbol)
        asset_min = profile.min_confluence_score
        try:
            from config.settings import settings as _s
            from core.meta_decision.confluence_scorer import SCORE_THRESHOLD
            user_min = float(_s.get("idss.min_confluence_score", SCORE_THRESHOLD))
            # Only override if user explicitly set a value below the default
            if user_min < SCORE_THRESHOLD:
                # Scale the per-asset threshold proportionally to preserve relative
                # ordering while respecting the user's global reduction
                scale = user_min / SCORE_THRESHOLD
                asset_min = asset_min * scale
        except Exception:
            pass
        return asset_min

    def get_all_symbols(self) -> List[str]:
        """Get list of all configured symbols."""
        return sorted(list(self._profiles.keys()))

    def to_dict(self) -> dict:
        """Serialize all profiles to a dictionary."""
        return {
            symbol: profile.to_dict()
            for symbol, profile in self._profiles.items()
        }

    @classmethod
    def from_dict(cls, data: dict) -> MultiAssetConfig:
        """
        Create MultiAssetConfig from a dictionary.

        Args:
            data: Dictionary with symbol keys and profile dicts as values

        Returns:
            New MultiAssetConfig instance
        """
        config = cls()
        if isinstance(data, dict):
            for symbol, profile_data in data.items():
                if isinstance(profile_data, dict):
                    try:
                        profile = AssetProfile.from_dict(profile_data)
                        if profile.validate():
                            config._profiles[symbol] = profile
                    except Exception as e:
                        logger.warning(f"Could not load profile for {symbol}: {e}")
        return config

    def save_to_settings(self) -> None:
        """Save current profiles to application settings."""
        try:
            settings.set("multi_asset.profiles", self.to_dict())
            logger.info("Multi-asset profiles saved to settings")
        except Exception as e:
            logger.error(f"Failed to save profiles to settings: {e}")


# Module-level singleton
multi_asset_config = MultiAssetConfig()

logger.info(f"MultiAssetConfig initialized with {len(multi_asset_config.get_all_symbols())} profiles")
