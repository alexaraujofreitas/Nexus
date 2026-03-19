# ============================================================
# NEXUS TRADER — Text-to-Speech Engine
#
# Backend priority order:
#   1. OpenAI TTS API        — best quality, requires openai key + package
#                              voice: nova (warm, professional female)
#   2. Windows SAPI          — zero-install on Windows 10/11
#                              uses .NET System.Speech via PowerShell
#                              female Zira / any installed female voice
#   3. pyttsx3               — offline, cross-platform, no key needed
#                              pip install pyttsx3  (+ espeak on Linux)
#
# Windows MP3 playback: WinMM MCI (built-in, blocking)
# macOS  MP3 playback:  afplay
# Linux  MP3 playback:  mpg123 / mpg321 / ffplay
# ============================================================
from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from typing import Optional

from PySide6.QtCore import QThread, Signal

logger = logging.getLogger(__name__)


# ── Text cleaner ─────────────────────────────────────────────
def clean_for_speech(text: str) -> str:
    """
    Strip markdown, code blocks, JSON, and strategy_config blocks
    so TTS reads clean prose instead of symbols and raw code.
    """
    text = re.sub(r"<strategy_config>.*?</strategy_config>", "", text, flags=re.DOTALL)
    text = re.sub(r"```[\s\S]*?```", "code block omitted.", text)
    text = re.sub(r"`[^`]+`", "", text)
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"\*(.+?)\*",     r"\1", text)
    text = re.sub(r"^#{1,3}\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"^[-•]\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"^\d+\.\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r" {2,}", " ", text)
    return text.strip()


# ── Helper: probe Windows SAPI availability ───────────────────
def _windows_sapi_available() -> bool:
    """
    Test whether PowerShell + System.Speech.Synthesis is available.
    This is built into every Windows 10 / 11 installation — no pip needed.
    """
    if sys.platform != "win32":
        return False
    try:
        result = subprocess.run(
            [
                "powershell", "-NoProfile", "-NonInteractive",
                "-Command",
                "Add-Type -AssemblyName System.Speech; Write-Output ok",
            ],
            capture_output=True, text=True, timeout=10,
        )
        return result.returncode == 0 and "ok" in result.stdout
    except Exception:
        return False


# ── Dependency / backend check ────────────────────────────────
def has_tts_support() -> tuple[bool, str]:
    """
    Returns (True, backend_name) when at least one TTS backend is ready,
    or (False, user-readable instructions) when nothing works.

    Priority: openai → windows_sapi → pyttsx3
    """
    # ── 1. OpenAI TTS ─────────────────────────────────────────
    try:
        import openai  # noqa: F401
        from config.settings import settings
        from core.security.key_vault import key_vault
        openai_key = (
            key_vault.load("ai.openai_api_key")
            or settings.get("ai.openai_api_key", "")
        )
        if openai_key and openai_key not in ("", "__vault__"):
            return True, "openai"
    except Exception:
        pass

    # ── 2. Windows SAPI (zero-install) ────────────────────────
    if _windows_sapi_available():
        return True, "windows_sapi"

    # ── 3. pyttsx3 (offline, cross-platform) ──────────────────
    try:
        import pyttsx3
        e = pyttsx3.init()
        e.stop()
        return True, "pyttsx3"
    except Exception:
        pass

    return False, (
        "Text-to-speech is not available.\n\n"
        "The easiest fix is to configure your OpenAI key in:\n"
        "  Settings → AI & ML → OpenAI API Key\n\n"
        "Alternatively, install the offline engine:\n"
        "  pip install pyttsx3\n"
        "  (Linux only: sudo apt-get install espeak espeak-ng)"
    )


# ── TTS Speaker QThread ───────────────────────────────────────
class TTSSpeaker(QThread):
    """
    Speaks text on a background thread using the best available backend.
    Always targets a polite, professional female voice.

    Signals
    -------
    started_speaking   playback has begun
    finished           playback complete or interrupted
    error(str)         human-readable error
    """

    started_speaking = Signal()
    finished         = Signal()
    error            = Signal(str)

    # ── Voice / quality settings ──────────────────────────────
    RATE      = 160      # WPM for pyttsx3 / SAPI (natural female pace)
    VOLUME    = 100      # 0–100 for SAPI; 0.0–1.0 mapped for pyttsx3
    OAI_VOICE = "nova"   # OpenAI voice: nova = warm, professional female
    OAI_MODEL = "tts-1"  # real-time latency optimised

    # Keywords used to detect female voices in pyttsx3 voice list
    _FEMALE_KW = (
        "zira",       # Windows 10/11 default female
        "samantha",   # macOS default female
        "victoria",   # macOS alternative
        "karen",      # macOS / some Windows
        "hazel",      # UK English female
        "susan", "eva", "female",
    )

    def __init__(self, text: str, parent=None):
        super().__init__(parent)
        self._text    = clean_for_speech(text)
        self._stop    = threading.Event()
        self._process: Optional[subprocess.Popen] = None   # active subprocess (SAPI / player)

    def stop(self):
        """
        Request immediate termination.
        - Sets the stop event (caught between pyttsx3 sentences)
        - Kills any live subprocess (Windows SAPI PowerShell or audio player)
        """
        self._stop.set()
        proc = self._process
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
            except Exception:
                pass

    # ── Main run loop ─────────────────────────────────────────
    def run(self):
        if not self._text.strip():
            self.finished.emit()
            return

        if self._try_openai_tts():
            return
        if self._try_windows_sapi():
            return
        self._try_pyttsx3()

    # ── Backend 1: OpenAI TTS ─────────────────────────────────
    def _try_openai_tts(self) -> bool:
        """High-quality cloud TTS. Returns True on success."""
        try:
            import openai
            from config.settings import settings
            from core.security.key_vault import key_vault

            openai_key = (
                key_vault.load("ai.openai_api_key")
                or settings.get("ai.openai_api_key", "")
            )
            if not openai_key or openai_key == "__vault__":
                return False

            client = openai.OpenAI(api_key=openai_key)

            with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
                tmp_path = f.name

            with client.audio.speech.with_streaming_response.create(
                model=self.OAI_MODEL,
                voice=self.OAI_VOICE,
                input=self._text[:4096],
            ) as resp:
                resp.stream_to_file(tmp_path)

            if self._stop.is_set():
                self.finished.emit()
                return True

            self.started_speaking.emit()
            self._play_file_blocking(tmp_path)

            try:
                os.unlink(tmp_path)
            except OSError:
                pass

            self.finished.emit()
            return True

        except Exception as exc:
            logger.debug("OpenAI TTS failed: %s", exc)
            return False

    # ── Backend 2: Windows SAPI via PowerShell ────────────────
    def _try_windows_sapi(self) -> bool:
        """
        Use .NET System.Speech.Synthesis via PowerShell — available on
        every Windows 10/11 machine with ZERO extra pip installs.

        Writes text to a temp file to avoid any quote-escaping issues.
        Selects Zira (or any installed female voice) at a natural pace.
        Uses Popen (not run) so stop() can terminate the process instantly.
        """
        if sys.platform != "win32":
            return False

        txt_path: Optional[str] = None
        try:
            # Write cleaned text to a temp UTF-8 file (avoids all escaping)
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".txt", delete=False, encoding="utf-8"
            ) as f:
                f.write(self._text)
                txt_path = f.name

            ps_script = (
                "Add-Type -AssemblyName System.Speech; "
                "$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
                "try { $synth.SelectVoiceByHints("
                "[System.Speech.Synthesis.VoiceGender]::Female) } catch {}; "
                "$synth.Rate = 1; "          # –10 slowest … +10 fastest; 1 ≈ 160 WPM
                f"$synth.Volume = {self.VOLUME}; "
                f"$text = [System.IO.File]::ReadAllText('{txt_path}'); "
                "$synth.Speak($text); "
                "$synth.Dispose()"
            )

            if self._stop.is_set():
                self.finished.emit()
                return True

            # Launch PowerShell as a separate process so we can kill it
            self._process = subprocess.Popen(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_script],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

            self.started_speaking.emit()

            # Poll every 100 ms — lets stop() terminate the process immediately
            while self._process.poll() is None:
                if self._stop.is_set():
                    try:
                        self._process.terminate()
                        self._process.wait(timeout=2)
                    except Exception:
                        pass
                    break
                time.sleep(0.1)

            self._process = None
            self.finished.emit()
            return True

        except Exception as exc:
            logger.debug("Windows SAPI TTS failed: %s", exc)
            return False
        finally:
            if txt_path:
                try:
                    os.unlink(txt_path)
                except OSError:
                    pass

    # ── Backend 3: pyttsx3 (offline cross-platform) ───────────
    def _try_pyttsx3(self):
        """
        Offline TTS using pyttsx3.  Sentence-by-sentence so stop()
        can interrupt between sentences.
        """
        try:
            import pyttsx3

            engine = pyttsx3.init()
            engine.setProperty("rate",   self.RATE)
            engine.setProperty("volume", self.VOLUME / 100.0)

            # ── Female voice selection ─────────────────────────
            voices = engine.getProperty("voices") or []
            female_id: Optional[str] = None

            for v in voices:
                name = (v.name or "").lower()
                vid  = (v.id  or "").lower()
                if any(kw in name or kw in vid for kw in self._FEMALE_KW):
                    female_id = v.id
                    break

            if female_id:
                engine.setProperty("voice", female_id)
            elif len(voices) > 1:
                # Windows fallback: index 0 = David (male), index 1 = Zira (female)
                engine.setProperty("voice", voices[1].id)

            if self._stop.is_set():
                self.finished.emit()
                return

            self.started_speaking.emit()

            sentences = re.split(r"(?<=[.!?])\s+", self._text)
            for sentence in sentences:
                if self._stop.is_set():
                    break
                if sentence.strip():
                    engine.say(sentence.strip())
                    engine.runAndWait()

            self.finished.emit()

        except Exception as exc:
            logger.warning("pyttsx3 TTS failed: %s", exc)
            self.error.emit(f"Text-to-speech error: {exc}")
            self.finished.emit()

    # ── MP3 file playback (blocking) ──────────────────────────
    def _play_file_blocking(self, path: str):
        """
        Play an MP3 file and block the thread until it finishes.
        Used by the OpenAI TTS backend after downloading the audio.
        """
        if self._stop.is_set():
            return

        if sys.platform == "win32":
            if self._play_winmm(path):
                return
            if self._play_ps_mediaplayer(path):
                return
            # Last resort: non-blocking + rough wait
            try:
                os.startfile(path)
                time.sleep(6)
            except Exception:
                pass
            return

        if sys.platform == "darwin":
            try:
                subprocess.run(["afplay", path], check=True, capture_output=True)
                return
            except Exception as exc:
                logger.debug("afplay failed: %s", exc)

        # Linux
        for player in ("mpg123", "mpg321", "ffplay", "aplay"):
            try:
                if subprocess.run(["which", player], capture_output=True).returncode != 0:
                    continue
                cmd = (
                    [player, "-nodisp", "-autoexit", path]
                    if player == "ffplay"
                    else [player, path]
                )
                subprocess.run(cmd, capture_output=True, check=True)
                return
            except Exception:
                continue
        logger.warning("No audio player found for TTS playback")

    def _play_winmm(self, path: str) -> bool:
        """
        WinMM MCI: blocking MP3 playback, zero extra packages.
        Uses a short poll loop (instead of 'play … wait') so that
        stop() can send an MCI 'stop' command mid-playback.
        """
        try:
            import ctypes
            wm    = ctypes.windll.winmm   # type: ignore[attr-defined]
            buf   = ctypes.create_unicode_buffer(256)
            safe  = path.replace("/", "\\")
            alias = "nexus_tts_mp3"

            wm.mciSendStringW(f'open "{safe}" type mpegvideo alias {alias}', None, 0, 0)
            wm.mciSendStringW(f"play {alias}", None, 0, 0)    # non-blocking start

            # Poll until the track finishes or stop() is called
            while True:
                if self._stop.is_set():
                    wm.mciSendStringW(f"stop {alias}", None, 0, 0)
                    break
                wm.mciSendStringW(f"status {alias} mode", buf, 255, 0)
                if buf.value.lower() in ("stopped", ""):
                    break
                time.sleep(0.1)

            wm.mciSendStringW(f"close {alias}", None, 0, 0)
            return True
        except Exception as exc:
            logger.debug("WinMM MCI failed: %s", exc)
            return False

    def _play_ps_mediaplayer(self, path: str) -> bool:
        """PowerShell WPF MediaPlayer — blocking fallback for Windows MP3."""
        try:
            # Convert Windows path to file:// URI format for PowerShell
            safe = path.replace("\\", "/")
            # Ensure proper file URI format (file:///C:/path/to/file.mp3)
            if not safe.startswith("file://"):
                if safe.startswith("/"):
                    safe = f"file://{safe}"
                else:
                    safe = f"file:///{safe}"
            ps = (
                "Add-Type -AssemblyName PresentationCore; "
                "$p = New-Object System.Windows.Media.MediaPlayer; "
                f"$p.Open([Uri]'{safe}'); $p.Play(); "
                "Start-Sleep -Milliseconds 800; "
                "while ($p.NaturalDuration.HasTimeSpan -and "
                "  $p.Position -lt $p.NaturalDuration.TimeSpan) "
                "{ Start-Sleep -Milliseconds 100 }; $p.Stop()"
            )
            subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps],
                timeout=120, capture_output=True,
            )
            return True
        except Exception as exc:
            logger.debug("PS MediaPlayer failed: %s", exc)
            return False
