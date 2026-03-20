# ============================================================
# NEXUS TRADER — Voice Input Engine
# Records mic audio and transcribes to text.
#
# Transcription order:
#   1. Google Web Speech API  (free, no key, requires internet)
#   2. OpenAI Whisper API     (requires openai key in vault)
#   3. sphinx                 (offline fallback, lower accuracy)
#
# Requirements (user's machine):
#   pip install SpeechRecognition pyaudio
#   On Windows PyAudio usually installs fine with pip.
#   On macOS: brew install portaudio && pip install pyaudio
#   On Linux: sudo apt-get install portaudio19-dev && pip install pyaudio
# ============================================================
from __future__ import annotations

import logging
import threading
from typing import Optional

from PySide6.QtCore import QThread, Signal

logger = logging.getLogger(__name__)

# ── Dependency check ──────────────────────────────────────────
def has_audio_support() -> tuple[bool, str]:
    """
    Returns (True, "") if audio capture is available,
    or (False, install_instructions) if not.
    """
    try:
        import speech_recognition  # noqa: F401
    except ImportError:
        return False, (
            "SpeechRecognition is not installed.\n\n"
            "Run:  pip install SpeechRecognition pyaudio\n\n"
            "On Windows: pip install SpeechRecognition pyaudio\n"
            "On macOS:   brew install portaudio && pip install SpeechRecognition pyaudio\n"
            "On Linux:   sudo apt-get install portaudio19-dev && pip install SpeechRecognition pyaudio"
        )
    try:
        import pyaudio  # noqa: F401
    except (ImportError, OSError):
        return False, (
            "PyAudio (microphone driver) is not installed.\n\n"
            "On Windows: pip install pyaudio\n"
            "On macOS:   brew install portaudio && pip install pyaudio\n"
            "On Linux:   sudo apt-get install portaudio19-dev && pip install pyaudio"
        )
    return True, ""


# ── Voice Recorder QThread ────────────────────────────────────
class VoiceRecorder(QThread):
    """
    Records a single audio phrase from the default microphone,
    then transcribes it.

    Signals:
        recording_started  — emitted once mic opens and listens
        recording_stopped  — emitted once speech ends / phrase captured
        transcription_ready(str) — final transcribed text
        error(str)         — human-readable error message
    """

    recording_started   = Signal()
    recording_stopped   = Signal()
    transcription_ready = Signal(str)
    error               = Signal(str)

    # Config
    AMBIENT_ADJUST_SECS  = 0.4   # time to calibrate for ambient noise
    LISTEN_TIMEOUT_SECS  = 8     # max silence before "no speech" error
    PHRASE_MAX_SECS      = 30    # hard cap on recording length
    PAUSE_THRESHOLD_SECS = 1.2   # silence duration to end phrase

    def __init__(self, parent=None):
        super().__init__(parent)
        self._abort = threading.Event()

    def stop(self):
        """Request abort — the thread may not stop instantly."""
        self._abort.set()

    def run(self):
        self._abort.clear()
        try:
            import speech_recognition as sr
        except ImportError:
            self.error.emit(
                "SpeechRecognition not installed.\n"
                "Run: pip install SpeechRecognition pyaudio"
            )
            return

        recogniser = sr.Recognizer()
        recogniser.pause_threshold    = self.PAUSE_THRESHOLD_SECS
        recogniser.non_speaking_duration = 0.3
        recogniser.energy_threshold   = 300

        try:
            mic = sr.Microphone()
        except OSError as exc:
            self.error.emit(
                f"Microphone not available: {exc}\n"
                "Check that your microphone is connected and not in use by another app."
            )
            return

        try:
            with mic as source:
                if self._abort.is_set():
                    return

                # Calibrate for ambient noise
                recogniser.adjust_for_ambient_noise(source, duration=self.AMBIENT_ADJUST_SECS)

                if self._abort.is_set():
                    return

                self.recording_started.emit()

                try:
                    audio = recogniser.listen(
                        source,
                        timeout=self.LISTEN_TIMEOUT_SECS,
                        phrase_time_limit=self.PHRASE_MAX_SECS,
                    )
                except sr.WaitTimeoutError:
                    self.error.emit("No speech detected. Try speaking closer to the microphone.")
                    return

        except OSError as exc:
            self.error.emit(f"Audio device error: {exc}")
            return

        if self._abort.is_set():
            return

        self.recording_stopped.emit()

        # ── Transcription ────────────────────────────────────
        text = self._transcribe(recogniser, audio)
        if text:
            self.transcription_ready.emit(text.strip())
        else:
            self.error.emit(
                "Could not transcribe the audio.\n"
                "Check your internet connection (Google Speech) or configure an OpenAI key."
            )

    # ── Transcription backends ────────────────────────────────
    def _transcribe(self, recogniser, audio) -> Optional[str]:
        import speech_recognition as sr

        # 1. Google Web Speech (free, requires internet)
        try:
            text = recogniser.recognize_google(audio)
            logger.info("Voice transcribed via Google Speech")
            return text
        except sr.UnknownValueError:
            logger.debug("Google Speech: could not understand audio")
        except sr.RequestError as exc:
            logger.debug("Google Speech request failed: %s", exc)

        # 2. OpenAI Whisper API (requires openai key)
        try:
            from config.settings import settings
            from core.security.key_vault import key_vault
            openai_key = key_vault.load("ai.openai_api_key") or settings.get("ai.openai_api_key", "")
            if openai_key and openai_key != "__vault__":
                text = recogniser.recognize_whisper_api(audio, api_key=openai_key)
                logger.info("Voice transcribed via Whisper API")
                return text
        except Exception as exc:
            logger.debug("Whisper API transcription failed: %s", exc)

        # 3. Sphinx offline fallback (installed separately)
        try:
            text = recogniser.recognize_sphinx(audio)
            logger.info("Voice transcribed via Sphinx (offline)")
            return text
        except Exception:
            pass

        return None
