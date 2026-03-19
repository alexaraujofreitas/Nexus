# ============================================================
# NEXUS TRADER — Theme Manager
# ============================================================

from pathlib import Path
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QFile, QTextStream

THEME_PATH = Path(__file__).parent / "dark_theme.qss"


class ThemeManager:
    @staticmethod
    def apply_dark_theme(app: QApplication):
        """Load and apply the Bloomberg dark QSS theme."""
        try:
            qss = THEME_PATH.read_text(encoding="utf-8")
            app.setStyleSheet(qss)
        except FileNotFoundError:
            print(f"[ThemeManager] Theme file not found: {THEME_PATH}")

    @staticmethod
    def reload(app: QApplication):
        """Reload theme at runtime (useful for development)."""
        ThemeManager.apply_dark_theme(app)
