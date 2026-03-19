# ============================================================
# NEXUS TRADER — News & Sentiment Page  (Full Implementation)
# Bloomberg dark theme
# Layout:
#   PageHeader
#   ┌─ Controls bar ──────────────────────────────────────────┐
#   ├─ KPI strip (3 cards) ───────────────────────────────────┤
#   └─ Tab panel: News Feed | Reddit | History ───────────────┘
# ============================================================
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QFrame,
    QPushButton, QComboBox, QCheckBox, QTabWidget,
    QTableWidget, QTableWidgetItem, QHeaderView,
    QAbstractItemView, QSizePolicy, QProgressBar,
    QSplitter, QTextEdit, QScrollArea,
)
from PySide6.QtCore import Qt, QThread, Signal, Slot, QTimer, QUrl
from PySide6.QtGui import QColor, QDesktopServices, QFont

from gui.main_window import PageHeader
from core.event_bus import bus, Topics

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────
# Theme constants (Bloomberg dark)
# ─────────────────────────────────────────────────────────────
_BG         = "#080C16"
_PANEL      = "#0D1320"
_CARD       = "#0F1825"
_BORDER     = "#1A2332"
_TEXT       = "#E8EBF0"
_MUTED      = "#8899AA"
_ACCENT     = "#1E90FF"
_GREEN      = "#00CC77"
_RED        = "#FF3355"
_ORANGE     = "#FF8C00"
_YELLOW     = "#FFB300"

_CARD_STYLE = (
    "QFrame#kpi_card { background:#0F1825; border:1px solid #1A2332; "
    "border-radius:8px; }"
)

_TABLE_STYLE = (
    "QTableWidget { background:#0A0E1A; color:#E8EBF0; "
    "gridline-color:#141E2E; font-size:13px; border:none; }"
    "QTableWidget::item:selected { background:#1A2D4A; }"
    "QTableWidget::item:alternate { background:#0C1018; }"
    "QHeaderView::section { background:#0D1320; color:#8899AA; "
    "padding:6px 8px; border:none; border-bottom:1px solid #1A2332; "
    "font-size:13px; font-weight:600; }"
)

_BTN_PRIMARY = (
    "QPushButton { background:#1E90FF; color:#FFF; border:none; "
    "border-radius:5px; padding:6px 16px; font-weight:600; font-size:13px; }"
    "QPushButton:hover { background:#3AA0FF; }"
    "QPushButton:disabled { background:#1A2D4A; color:#4A6A8A; }"
)

_BTN_GHOST = (
    "QPushButton { background:transparent; color:#8899AA; border:1px solid #2A3A52; "
    "border-radius:5px; padding:5px 12px; font-size:13px; }"
    "QPushButton:hover { color:#E8EBF0; border-color:#1E90FF; }"
)

_COMBO_STYLE = (
    "QComboBox { background:#0F1623; color:#E8EBF0; border:1px solid #2A3A52; "
    "border-radius:4px; padding:4px 8px; font-size:13px; }"
    "QComboBox:focus { border-color:#1E90FF; }"
    "QComboBox QAbstractItemView { background:#0F1623; color:#E8EBF0; "
    "selection-background-color:#1A2D4A; border:1px solid #2A3A52; }"
)

_TAB_STYLE = (
    "QTabWidget::pane { border:none; background:#080C16; }"
    "QTabBar::tab { background:#0D1320; color:#6A7E99; padding:8px 20px; "
    "border:none; border-bottom:2px solid transparent; font-size:13px; }"
    "QTabBar::tab:selected { color:#E8EBF0; border-bottom:2px solid #1E90FF; }"
    "QTabBar::tab:hover { color:#B0C0D0; }"
)

_CHECK_STYLE = (
    "QCheckBox { color:#8899AA; font-size:13px; spacing:6px; }"
    "QCheckBox::indicator { width:14px; height:14px; border:1px solid #2A3A52; "
    "border-radius:3px; background:#0F1623; }"
    "QCheckBox::indicator:checked { background:#1E90FF; border-color:#1E90FF; }"
)

# ─────────────────────────────────────────────────────────────
# Column definitions
# ─────────────────────────────────────────────────────────────
_NEWS_COLS   = ["Time", "Source", "Headline", "Symbol", "Sentiment", "Score"]
_REDDIT_COLS = ["Time", "Subreddit", "Title", "↑ Score", "Comments", "Sentiment"]


# ─────────────────────────────────────────────────────────────
# Worker thread
# ─────────────────────────────────────────────────────────────
class SentimentWorker(QThread):
    progress = Signal(str)
    finished = Signal(dict)
    error    = Signal(str)

    def __init__(self, symbols: Optional[list[str]] = None, parent=None):
        super().__init__(parent)
        self._symbols = symbols

    def run(self):
        try:
            from core.sentiment.sentiment_engine import sentiment_engine
            result = sentiment_engine.fetch_and_score(
                symbols=self._symbols,
                progress_cb=lambda msg: self.progress.emit(msg),
            )
            self.finished.emit(result)
        except Exception as exc:
            logger.exception("SentimentWorker error")
            self.error.emit(str(exc))


# ─────────────────────────────────────────────────────────────
# KPI Card
# ─────────────────────────────────────────────────────────────
class KpiCard(QFrame):
    def __init__(self, title: str, parent=None):
        super().__init__(parent)
        self.setObjectName("kpi_card")
        self.setStyleSheet(_CARD_STYLE)
        self.setMinimumWidth(180)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setFixedHeight(110)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(4)

        self._title_lbl = QLabel(title.upper())
        self._title_lbl.setStyleSheet(f"color:{_MUTED}; font-size:13px; font-weight:600;")

        self._value_lbl = QLabel("—")
        self._value_lbl.setStyleSheet(f"color:{_TEXT}; font-size:26px; font-weight:700;")

        self._sub_lbl = QLabel("")
        self._sub_lbl.setStyleSheet(f"color:{_MUTED}; font-size:13px;")

        layout.addWidget(self._title_lbl)
        layout.addWidget(self._value_lbl)
        layout.addWidget(self._sub_lbl)
        layout.addStretch()

    def set_value(self, value: str, color: str = _TEXT, sub: str = ""):
        self._value_lbl.setText(value)
        self._value_lbl.setStyleSheet(f"color:{color}; font-size:26px; font-weight:700;")
        self._sub_lbl.setText(sub)


# ─────────────────────────────────────────────────────────────
# News Table
# ─────────────────────────────────────────────────────────────
class NewsTable(QTableWidget):
    article_selected = Signal(dict)

    def __init__(self, parent=None):
        super().__init__(0, len(_NEWS_COLS), parent)
        self._rows: list[dict] = []
        self._setup()

    def _setup(self):
        self.setHorizontalHeaderLabels(_NEWS_COLS)
        hdr = self.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeToContents)   # Time
        hdr.setSectionResizeMode(1, QHeaderView.ResizeToContents)   # Source
        hdr.setSectionResizeMode(2, QHeaderView.Stretch)            # Headline
        hdr.setSectionResizeMode(3, QHeaderView.ResizeToContents)   # Symbol
        hdr.setSectionResizeMode(4, QHeaderView.ResizeToContents)   # Sentiment
        hdr.setSectionResizeMode(5, QHeaderView.ResizeToContents)   # Score
        self.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.setAlternatingRowColors(True)
        self.setSortingEnabled(True)
        self.verticalHeader().setVisible(False)
        self.setShowGrid(True)
        self.setStyleSheet(_TABLE_STYLE)
        self.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.setWordWrap(False)
        self.verticalHeader().setDefaultSectionSize(28)
        self.currentCellChanged.connect(
            lambda row, _c, _pr, _pc: self._on_row_changed(row)
        )
        self.doubleClicked.connect(self._on_double_click)

    def load_articles(self, articles: list[dict]) -> None:
        self._rows = list(articles)
        self.setSortingEnabled(False)
        self.setRowCount(0)
        self.setRowCount(len(articles))

        for ri, a in enumerate(articles):
            pub: datetime = a.get("published_at", datetime.now(timezone.utc))
            time_str = pub.strftime("%m/%d %H:%M") if pub else "—"

            score: float = a.get("score", 0.0)
            label: str   = a.get("label", "Neutral")
            color: str   = a.get("color", _MUTED)

            self._set_cell(ri, 0, time_str,                     align=Qt.AlignCenter)
            self._set_cell(ri, 1, a.get("source", ""),          align=Qt.AlignCenter)
            self._set_cell(ri, 2, a.get("title", ""),           align=Qt.AlignLeft)
            self._set_cell(ri, 3, a.get("symbol", "").upper(),  align=Qt.AlignCenter)
            self._set_colored(ri, 4, label, color)
            self._set_colored(ri, 5, f"{score:+.2f}", color)

        self.setSortingEnabled(True)
        self.sortByColumn(0, Qt.DescendingOrder)

    def _set_cell(self, row, col, text, align=Qt.AlignLeft):
        item = QTableWidgetItem(str(text))
        item.setTextAlignment(int(align) | Qt.AlignVCenter)
        item.setForeground(QColor(_TEXT))
        self.setItem(row, col, item)

    def _set_colored(self, row, col, text, color):
        item = QTableWidgetItem(str(text))
        item.setTextAlignment(Qt.AlignCenter | Qt.AlignVCenter)
        item.setForeground(QColor(color))
        self.setItem(row, col, item)

    def _on_row_changed(self, row: int):
        if 0 <= row < len(self._rows):
            self.article_selected.emit(self._rows[row])

    def _on_double_click(self, index):
        row = index.row()
        if 0 <= row < len(self._rows):
            url = self._rows[row].get("url", "")
            if url:
                QDesktopServices.openUrl(QUrl(url))


# ─────────────────────────────────────────────────────────────
# Reddit Table
# ─────────────────────────────────────────────────────────────
class RedditTable(QTableWidget):
    post_selected = Signal(dict)

    def __init__(self, parent=None):
        super().__init__(0, len(_REDDIT_COLS), parent)
        self._rows: list[dict] = []
        self._setup()

    def _setup(self):
        self.setHorizontalHeaderLabels(_REDDIT_COLS)
        hdr = self.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(2, QHeaderView.Stretch)
        hdr.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(4, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(5, QHeaderView.ResizeToContents)
        self.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.setAlternatingRowColors(True)
        self.setSortingEnabled(True)
        self.verticalHeader().setVisible(False)
        self.setShowGrid(True)
        self.setStyleSheet(_TABLE_STYLE)
        self.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.verticalHeader().setDefaultSectionSize(28)
        self.currentCellChanged.connect(
            lambda row, _c, _pr, _pc: self._on_row_changed(row)
        )
        self.doubleClicked.connect(self._on_double_click)

    def load_posts(self, posts: list[dict]) -> None:
        self._rows = list(posts)
        self.setSortingEnabled(False)
        self.setRowCount(0)
        self.setRowCount(len(posts))

        for ri, p in enumerate(posts):
            pub: datetime = p.get("published_at", datetime.now(timezone.utc))
            time_str = pub.strftime("%m/%d %H:%M") if pub else "—"
            score    = p.get("score", 0.0)
            label    = p.get("label", "Neutral")
            color    = p.get("color", _MUTED)

            self._set_cell(ri, 0, time_str,                    align=Qt.AlignCenter)
            self._set_cell(ri, 1, f"r/{p.get('subreddit','')}",align=Qt.AlignCenter)
            self._set_cell(ri, 2, p.get("title", ""),          align=Qt.AlignLeft)
            self._set_cell(ri, 3, f"{p.get('score', 0):,}",    align=Qt.AlignRight)
            self._set_cell(ri, 4, f"{p.get('num_comments', 0):,}", align=Qt.AlignRight)
            self._set_colored(ri, 5, label, color)

        self.setSortingEnabled(True)
        self.sortByColumn(0, Qt.DescendingOrder)

    def _set_cell(self, row, col, text, align=Qt.AlignLeft):
        item = QTableWidgetItem(str(text))
        item.setTextAlignment(int(align) | Qt.AlignVCenter)
        item.setForeground(QColor(_TEXT))
        self.setItem(row, col, item)

    def _set_colored(self, row, col, text, color):
        item = QTableWidgetItem(str(text))
        item.setTextAlignment(Qt.AlignCenter | Qt.AlignVCenter)
        item.setForeground(QColor(color))
        self.setItem(row, col, item)

    def _on_row_changed(self, row: int):
        if 0 <= row < len(self._rows):
            self.post_selected.emit(self._rows[row])

    def _on_double_click(self, index):
        row = index.row()
        if 0 <= row < len(self._rows):
            url = self._rows[row].get("url", "")
            if url:
                QDesktopServices.openUrl(QUrl(url))


# ─────────────────────────────────────────────────────────────
# Detail Panel (right side — shows selected article/post body)
# ─────────────────────────────────────────────────────────────
class DetailPanel(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("card")
        self.setStyleSheet(
            "QFrame#card { background:#0D1320; border:1px solid #1A2332; border-radius:6px; }"
        )
        self.setMinimumWidth(260)
        self.setMaximumWidth(340)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(8)

        self._title = QLabel("Select an article or post")
        self._title.setStyleSheet(f"color:{_TEXT}; font-size:13px; font-weight:600;")
        self._title.setWordWrap(True)

        self._source = QLabel("")
        self._source.setStyleSheet(f"color:{_MUTED}; font-size:13px;")

        self._sentiment_badge = QLabel("")
        self._sentiment_badge.setStyleSheet(
            f"color:{_ACCENT}; font-size:13px; font-weight:600;"
        )

        self._body = QTextEdit()
        self._body.setReadOnly(True)
        self._body.setStyleSheet(
            f"QTextEdit {{ background:#080C16; color:{_MUTED}; border:none; "
            f"font-size:13px; border-radius:4px; padding:6px; }}"
        )

        self._open_btn = QPushButton("Open in Browser ↗")
        self._open_btn.setStyleSheet(_BTN_GHOST)
        self._open_btn.clicked.connect(self._open_url)
        self._open_btn.setVisible(False)

        layout.addWidget(self._title)
        layout.addWidget(self._source)
        layout.addWidget(self._sentiment_badge)
        layout.addWidget(self._body, 1)
        layout.addWidget(self._open_btn)

        self._current_url = ""

    def show_article(self, article: dict):
        self._current_url = article.get("url", "")
        pub = article.get("published_at")
        time_str = pub.strftime("%b %d %Y %H:%M UTC") if pub else ""

        label = article.get("label", "Neutral")
        color = article.get("color", _MUTED)
        score = article.get("score", 0.0)

        self._title.setText(article.get("title", ""))
        self._source.setText(
            f"{article.get('source', '')}  ·  {time_str}"
        )
        self._sentiment_badge.setText(f"● {label}  ({score:+.3f})")
        self._sentiment_badge.setStyleSheet(
            f"color:{color}; font-size:13px; font-weight:600;"
        )
        self._body.setPlainText(
            article.get("description", "") or "(no description)"
        )
        self._open_btn.setVisible(bool(self._current_url))

    def show_post(self, post: dict):
        self._current_url = post.get("url", "")
        pub = post.get("published_at")
        time_str = pub.strftime("%b %d %Y %H:%M UTC") if pub else ""

        label = post.get("label", "Neutral")
        color = post.get("color", _MUTED)
        score = post.get("score", 0.0)
        reddit_score = post.get("score", 0)

        self._title.setText(post.get("title", ""))
        self._source.setText(
            f"r/{post.get('subreddit','')}  ·  {time_str}  ·  "
            f"↑{post.get('score',0):,}  💬{post.get('num_comments',0):,}"
        )
        self._sentiment_badge.setText(f"● {label}  ({score:+.3f})")
        self._sentiment_badge.setStyleSheet(
            f"color:{color}; font-size:13px; font-weight:600;"
        )
        self._body.setPlainText(post.get("text", "") or "(link post / no body)")
        self._open_btn.setVisible(bool(self._current_url))

    def _open_url(self):
        if self._current_url:
            QDesktopServices.openUrl(QUrl(self._current_url))


# ─────────────────────────────────────────────────────────────
# Main Page
# ─────────────────────────────────────────────────────────────
class NewsSentimentPage(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._worker:  Optional[SentimentWorker] = None
        self._articles: list[dict] = []
        self._posts:    list[dict] = []
        self._aggregate: dict = {}
        self._first_show = True   # triggers auto-fetch the first time the page is shown

        # Auto-refresh timer (default off)
        self._auto_timer = QTimer(self)
        self._auto_timer.timeout.connect(self._run_fetch)

        self._build()
        self._subscribe()

    # ─────────────────────────────────────────────────────
    # Build UI
    # ─────────────────────────────────────────────────────
    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        root.addWidget(PageHeader(
            "News & Sentiment",
            "Live news feed, sentiment scores and social signals",
        ))

        # ── Controls bar ──────────────────────────────────
        ctrl_bar = QWidget()
        ctrl_bar.setStyleSheet(f"background:{_PANEL}; border-bottom:1px solid {_BORDER};")
        ctrl_bar.setFixedHeight(52)
        ch = QHBoxLayout(ctrl_bar)
        ch.setContentsMargins(20, 0, 20, 0)
        ch.setSpacing(12)

        lbl = QLabel("Symbol:")
        lbl.setStyleSheet(f"color:{_MUTED}; font-size:13px;")

        self._symbol_combo = QComboBox()
        self._symbol_combo.setStyleSheet(_COMBO_STYLE)
        self._symbol_combo.setFixedWidth(130)
        self._symbol_combo.addItem("All Crypto")
        self._symbol_combo.addItems(["BTC", "ETH", "SOL", "BNB", "XRP", "ADA"])

        self._news_check = QCheckBox("News")
        self._news_check.setStyleSheet(_CHECK_STYLE)
        self._news_check.setChecked(True)

        self._reddit_check = QCheckBox("Reddit")
        self._reddit_check.setStyleSheet(_CHECK_STYLE)

        self._auto_check = QCheckBox("Auto (5 min)")
        self._auto_check.setStyleSheet(_CHECK_STYLE)
        self._auto_check.toggled.connect(self._on_auto_toggle)

        self._refresh_btn = QPushButton("⟳  Refresh")
        self._refresh_btn.setStyleSheet(_BTN_PRIMARY)
        self._refresh_btn.setFixedWidth(110)
        self._refresh_btn.clicked.connect(self._run_fetch)

        self._status_lbl = QLabel("Not loaded")
        self._status_lbl.setStyleSheet(f"color:{_MUTED}; font-size:13px;")

        self._progress = QProgressBar()
        self._progress.setRange(0, 0)   # indeterminate
        self._progress.setFixedWidth(80)
        self._progress.setFixedHeight(6)
        self._progress.setVisible(False)
        self._progress.setStyleSheet(
            "QProgressBar { background:#0F1623; border:none; border-radius:3px; }"
            "QProgressBar::chunk { background:#1E90FF; border-radius:3px; }"
        )

        ch.addWidget(lbl)
        ch.addWidget(self._symbol_combo)
        ch.addSpacing(8)
        ch.addWidget(self._news_check)
        ch.addWidget(self._reddit_check)
        ch.addSpacing(8)
        ch.addWidget(self._auto_check)
        ch.addStretch()
        ch.addWidget(self._progress)
        ch.addWidget(self._status_lbl)
        ch.addWidget(self._refresh_btn)
        root.addWidget(ctrl_bar)

        # ── KPI strip ──────────────────────────────────────
        kpi_strip = QWidget()
        kpi_strip.setStyleSheet(f"background:{_BG};")
        kh = QHBoxLayout(kpi_strip)
        kh.setContentsMargins(20, 14, 20, 14)
        kh.setSpacing(14)

        self._kpi_overall   = KpiCard("Overall Sentiment")
        self._kpi_articles  = KpiCard("Articles Fetched")
        self._kpi_posts     = KpiCard("Reddit Posts")
        self._kpi_mood      = KpiCard("Market Mood")

        for card in (self._kpi_overall, self._kpi_articles, self._kpi_posts, self._kpi_mood):
            kh.addWidget(card)

        root.addWidget(kpi_strip)

        # ── Main content: tabs + detail panel ─────────────
        body = QWidget()
        body.setStyleSheet(f"background:{_BG};")
        bh = QHBoxLayout(body)
        bh.setContentsMargins(20, 0, 20, 16)
        bh.setSpacing(14)

        # Tab panel
        self._tabs = QTabWidget()
        self._tabs.setStyleSheet(_TAB_STYLE)

        # News tab
        news_widget = QWidget()
        nv = QVBoxLayout(news_widget)
        nv.setContentsMargins(0, 8, 0, 0)
        nv.setSpacing(0)
        self._news_table = NewsTable()
        self._news_table.article_selected.connect(self._detail.show_article
            if hasattr(self, "_detail") else lambda a: None)
        nv.addWidget(self._news_table)
        self._tabs.addTab(news_widget, "📰  News Feed")

        # Reddit tab
        reddit_widget = QWidget()
        rv = QVBoxLayout(reddit_widget)
        rv.setContentsMargins(0, 8, 0, 0)
        rv.setSpacing(0)
        self._reddit_table = RedditTable()
        rv.addWidget(self._reddit_table)
        self._tabs.addTab(reddit_widget, "🟠  Reddit")

        # History tab (sentiment score history from DB)
        hist_widget = QWidget()
        hv = QVBoxLayout(hist_widget)
        hv.setContentsMargins(0, 8, 0, 0)
        self._hist_table = self._build_history_table()
        hv.addWidget(self._hist_table)
        self._tabs.addTab(hist_widget, "📊  History")

        # Detail panel (right side)
        self._detail = DetailPanel()
        # Now wire signal properly
        self._news_table.article_selected.connect(self._detail.show_article)
        self._reddit_table.post_selected.connect(self._detail.show_post)

        bh.addWidget(self._tabs, 1)
        bh.addWidget(self._detail)

        root.addWidget(body, 1)

    def _build_history_table(self) -> QTableWidget:
        cols = ["Timestamp", "Articles", "Posts", "Overall Score", "Mood"]
        tbl = QTableWidget(0, len(cols))
        tbl.setHorizontalHeaderLabels(cols)
        hdr = tbl.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(3, QHeaderView.Stretch)
        hdr.setSectionResizeMode(4, QHeaderView.ResizeToContents)
        tbl.setSelectionBehavior(QAbstractItemView.SelectRows)
        tbl.setAlternatingRowColors(True)
        tbl.setSortingEnabled(True)
        tbl.verticalHeader().setVisible(False)
        tbl.setShowGrid(True)
        tbl.setStyleSheet(_TABLE_STYLE)
        tbl.setEditTriggers(QAbstractItemView.NoEditTriggers)
        tbl.verticalHeader().setDefaultSectionSize(28)
        return tbl

    # ─────────────────────────────────────────────────────
    # Event bus
    # ─────────────────────────────────────────────────────
    def _subscribe(self):
        bus.subscribe(Topics.SENTIMENT_UPDATED, self._on_sentiment_event)

    @Slot()
    def _on_sentiment_event(self, event):
        # Called from event bus (may be non-Qt thread) — schedule UI update
        QTimer.singleShot(0, self._load_history)

    # ─────────────────────────────────────────────────────
    # Show / hide
    # ─────────────────────────────────────────────────────
    def showEvent(self, event):
        super().showEvent(event)
        # Load settings-driven defaults
        try:
            from config.settings import settings
            self._reddit_check.setChecked(
                bool(settings.get("sentiment.reddit_enabled", False))
            )
            self._news_check.setChecked(
                bool(settings.get("sentiment.news_enabled", True))
            )
        except Exception:
            pass
        self._load_history()
        # Auto-fetch on first show so the page never starts blank
        if self._first_show:
            self._first_show = False
            self._status_lbl.setStyleSheet(f"color:{_MUTED}; font-size:13px;")
            self._status_lbl.setText("Loading latest news & sentiment…")
            QTimer.singleShot(300, self._run_fetch)

    # ─────────────────────────────────────────────────────
    # Fetch
    # ─────────────────────────────────────────────────────
    @Slot()
    def _run_fetch(self):
        if self._worker and self._worker.isRunning():
            return

        sym_text = self._symbol_combo.currentText()
        symbols  = None if sym_text == "All Crypto" else [sym_text]

        # Temporarily patch settings based on checkboxes
        try:
            from config.settings import settings
            settings.set("sentiment.news_enabled",   self._news_check.isChecked())
            settings.set("sentiment.reddit_enabled", self._reddit_check.isChecked())
        except Exception:
            pass

        self._refresh_btn.setEnabled(False)
        self._progress.setVisible(True)
        self._status_lbl.setText("Fetching…")

        self._worker = SentimentWorker(symbols=symbols, parent=self)
        self._worker.progress.connect(self._on_progress)
        self._worker.finished.connect(self._on_finished)
        self._worker.error.connect(self._on_error)
        self._worker.start()

    @Slot(str)
    def _on_progress(self, msg: str):
        self._status_lbl.setText(msg)

    @Slot(dict)
    def _on_finished(self, result: dict):
        self._articles  = result.get("articles", [])
        self._posts     = result.get("posts", [])
        self._aggregate = result.get("aggregate", {})
        warnings        = result.get("warnings", [])

        self._news_table.load_articles(self._articles)
        self._reddit_table.load_posts(self._posts)
        self._update_kpis()
        self._load_history()

        now = datetime.now().strftime("%H:%M:%S")
        n_articles = len(self._articles)
        n_posts    = len(self._posts)

        if n_articles == 0 and n_posts == 0 and warnings:
            # Show the first warning so the user knows what happened
            self._status_lbl.setText(f"⚠ {warnings[0]}")
            self._status_lbl.setStyleSheet(f"color:{_ORANGE}; font-size:13px;")
        else:
            self._status_lbl.setText(
                f"Updated {now}  ·  {n_articles} articles  ·  {n_posts} posts"
            )
            self._status_lbl.setStyleSheet(f"color:{_MUTED}; font-size:13px;")

        self._refresh_btn.setEnabled(True)
        self._progress.setVisible(False)

    @Slot(str)
    def _on_error(self, msg: str):
        self._status_lbl.setText(f"⚠ {msg}")
        self._status_lbl.setStyleSheet(f"color:{_RED}; font-size:13px;")
        self._refresh_btn.setEnabled(True)
        self._progress.setVisible(False)

    # ─────────────────────────────────────────────────────
    # KPI update
    # ─────────────────────────────────────────────────────
    def _update_kpis(self):
        from core.sentiment.sentiment_engine import sentiment_label

        overall = self._aggregate.get("__overall__", 0.0)
        label, color = sentiment_label(overall)

        self._kpi_overall.set_value(f"{overall:+.3f}", color, sub=label)
        self._kpi_articles.set_value(str(len(self._articles)), _ACCENT)
        self._kpi_posts.set_value(str(len(self._posts)), _ORANGE)
        self._kpi_mood.set_value(label, color)

    # ─────────────────────────────────────────────────────
    # History tab
    # ─────────────────────────────────────────────────────
    def _load_history(self):
        try:
            from core.database.engine import get_session
            from core.database.models import SentimentData
            from sqlalchemy import select, desc
            from core.sentiment.sentiment_engine import sentiment_label

            with get_session() as session:
                rows = session.execute(
                    select(SentimentData)
                    .where(SentimentData.source == "aggregated")
                    .order_by(desc(SentimentData.timestamp))
                    .limit(100)
                ).scalars().all()

            tbl = self._hist_table
            tbl.setSortingEnabled(False)
            tbl.setRowCount(0)
            tbl.setRowCount(len(rows))

            for ri, row in enumerate(rows):
                raw  = row.raw_data or {}
                score = row.sentiment_score or 0.0
                label, color = sentiment_label(score)
                ts = row.timestamp
                ts_str = ts.strftime("%Y-%m-%d %H:%M") if ts else "—"

                def _cell(text, align=Qt.AlignCenter):
                    item = QTableWidgetItem(str(text))
                    item.setTextAlignment(int(align) | Qt.AlignVCenter)
                    item.setForeground(QColor(_TEXT))
                    return item

                def _colored(text, clr):
                    item = QTableWidgetItem(str(text))
                    item.setTextAlignment(Qt.AlignCenter | Qt.AlignVCenter)
                    item.setForeground(QColor(clr))
                    return item

                tbl.setItem(ri, 0, _cell(ts_str))
                tbl.setItem(ri, 1, _cell(raw.get("article_count", "—")))
                tbl.setItem(ri, 2, _cell(raw.get("post_count", "—")))
                tbl.setItem(ri, 3, _colored(f"{score:+.4f}", color))
                tbl.setItem(ri, 4, _colored(label, color))

            tbl.setSortingEnabled(True)

        except Exception as exc:
            logger.debug("History load failed: %s", exc)

    # ─────────────────────────────────────────────────────
    # Auto-refresh
    # ─────────────────────────────────────────────────────
    @Slot(bool)
    def _on_auto_toggle(self, checked: bool):
        if checked:
            self._auto_timer.start(5 * 60 * 1000)   # 5 minutes
            self._run_fetch()
        else:
            self._auto_timer.stop()
