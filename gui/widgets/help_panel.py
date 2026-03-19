# ============================================================
# NEXUS TRADER — Help Panel Widget
#
# A floating side panel that shows contextual help text.
# Opens as a non-modal dialog anchored to the right side
# of the main window.
# ============================================================
from __future__ import annotations

from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QTextBrowser, QFrame, QScrollArea,
)
from PySide6.QtCore import Qt, QSize
from PySide6.QtGui import QFont


_PANEL_WIDTH  = 380
_PANEL_HEIGHT = 500


class HelpPanel(QDialog):
    """
    Floating help panel displaying formatted help text.
    """

    def __init__(self, title: str, content: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Help — {title}")
        self.setWindowFlags(
            Qt.Dialog |
            Qt.CustomizeWindowHint |
            Qt.WindowTitleHint |
            Qt.WindowCloseButtonHint
        )
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.resize(_PANEL_WIDTH, _PANEL_HEIGHT)
        self.setModal(False)

        self.setStyleSheet(
            "QDialog { background:#0A0E1A; color:#C8D0E0; }"
            "QLabel { color:#C8D0E0; }"
        )

        v = QVBoxLayout(self)
        v.setContentsMargins(16, 12, 16, 16)
        v.setSpacing(12)

        # Header
        header = QFrame()
        header.setStyleSheet(
            "background:#0D1B2A; border-radius:6px; padding:4px;"
        )
        hh = QHBoxLayout(header)
        hh.setContentsMargins(12, 8, 12, 8)

        title_icon = QLabel("?")
        title_icon.setStyleSheet(
            "background:#1E3A5F; color:#4299E1; font-size:13px; font-weight:700;"
            " border-radius:11px; padding:2px 7px;"
        )
        hh.addWidget(title_icon)

        title_lbl = QLabel(title)
        font = QFont()
        font.setPointSize(12)
        font.setBold(True)
        title_lbl.setFont(font)
        title_lbl.setStyleSheet("color:#C8D0E0;")
        hh.addWidget(title_lbl)
        hh.addStretch()

        v.addWidget(header)

        # Content browser
        self._browser = QTextBrowser()
        self._browser.setStyleSheet(
            "QTextBrowser {"
            "  background:#0D1B2A; color:#C8D0E0;"
            "  border:1px solid #1E3A5F; border-radius:6px;"
            "  padding:12px; font-size:13px; line-height:160%;"
            "}"
            "QScrollBar:vertical { background:#0D1B2A; width:6px; border-radius:3px; }"
            "QScrollBar::handle:vertical { background:#1E3A5F; border-radius:3px; }"
        )
        # Convert plain text to basic HTML with line breaks preserved
        html_content = (
            "<html><body style='font-family:sans-serif; color:#C8D0E0; line-height:160%;'>"
            + content.replace("\n\n", "<br><br>").replace("\n  •", "<br>&nbsp;&nbsp;•")
                     .replace("\n  1.", "<br>&nbsp;&nbsp;1.").replace("\n  2.", "<br>&nbsp;&nbsp;2.")
                     .replace("\n  3.", "<br>&nbsp;&nbsp;3.").replace("\n  4.", "<br>&nbsp;&nbsp;4.")
                     .replace("\n  5.", "<br>&nbsp;&nbsp;5.").replace("\n  6.", "<br>&nbsp;&nbsp;6.")
                     .replace("\n", "<br>")
            + "</body></html>"
        )
        self._browser.setHtml(html_content)
        v.addWidget(self._browser, 1)

        # Footer: link to help center
        footer = QHBoxLayout()
        footer_lbl = QLabel(
            "<a href='help_center' style='color:#4299E1;'>Open full Help Center →</a>"
        )
        footer_lbl.setOpenExternalLinks(False)
        footer_lbl.linkActivated.connect(self._open_help_center)
        footer.addWidget(footer_lbl)
        footer.addStretch()

        close_btn = QPushButton("Close")
        close_btn.setFixedWidth(80)
        close_btn.clicked.connect(self.close)
        footer.addWidget(close_btn)
        v.addLayout(footer)

        # Position panel on right side of parent
        if parent:
            parent_rect = parent.geometry()
            x = parent_rect.right() - _PANEL_WIDTH - 20
            y = parent_rect.top() + 80
            self.move(x, y)

    def _open_help_center(self, _: str) -> None:
        """Navigate to the help center page in the main window."""
        try:
            # Walk up the hierarchy to find the main window
            w = self.parent()
            while w and not hasattr(w, "_navigate_to"):
                w = w.parent() if hasattr(w, "parent") else None
            if w and hasattr(w, "_navigate_to"):
                w._navigate_to("help_center")
                self.close()
        except Exception:
            pass
