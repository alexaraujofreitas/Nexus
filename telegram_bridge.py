import os
import json
import logging
import asyncio
import subprocess
import time
from pathlib import Path
from typing import Optional

from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    MessageHandler,
    CommandHandler,
    filters,
)

# -----------------------------------------------------------------------------
# CONFIG
# -----------------------------------------------------------------------------

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()

# Comma-separated Telegram user IDs for auth guard
ALLOWED_USERS_RAW = os.getenv("TELEGRAM_ALLOWED_USERS", "").strip()

NEXUSTRADER_PATH = os.getenv(
    "NEXUSTRADER_PATH", r"C:\Users\alexa\NexusTrader"
).strip()

# Claude Code CLI binary (must be in PATH)
CLAUDE_CLI = os.getenv("CLAUDE_CLI", "claude").strip()

# Defaults
DEFAULT_MODEL = "sonnet"
CLI_TIMEOUT = 300  # seconds
TELEGRAM_CHUNK = 3900  # safe limit under Telegram's 4096
MAX_RESPONSE_LEN = 15_000

# Tool allow-lists for Claude CLI
READ_ONLY_TOOLS = (
    "Read,Glob,Grep,"
    "Bash(git:*),Bash(cat:*),Bash(ls:*),Bash(head:*),"
    "Bash(tail:*),Bash(wc:*),Bash(find:*)"
)
EDIT_TOOLS = "Read,Glob,Grep,Edit,Write,Bash"

# -----------------------------------------------------------------------------
# LOGGING
# -----------------------------------------------------------------------------

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("telegram_bridge")

# -----------------------------------------------------------------------------
# VALIDATION
# -----------------------------------------------------------------------------

if not BOT_TOKEN:
    raise ValueError(
        "TELEGRAM_BOT_TOKEN is not set. "
        "Set it with: setx TELEGRAM_BOT_TOKEN YOUR_TOKEN"
    )

AUTHORIZED_USERS: set[int] = set()
if ALLOWED_USERS_RAW:
    for part in ALLOWED_USERS_RAW.split(","):
        part = part.strip()
        if part.isdigit():
            AUTHORIZED_USERS.add(int(part))

# -----------------------------------------------------------------------------
# STATE
# -----------------------------------------------------------------------------

# Per-user Claude Code session IDs for conversation continuity
user_sessions: dict[int, str] = {}

# Lock to prevent concurrent /fix operations
_fix_lock = asyncio.Lock()

# -----------------------------------------------------------------------------
# AUTH
# -----------------------------------------------------------------------------


def is_authorized(user_id: int) -> bool:
    if not AUTHORIZED_USERS:
        return True
    return user_id in AUTHORIZED_USERS


async def auth_guard(update: Update) -> bool:
    if not update.message or not update.message.from_user:
        return False

    user_id = update.message.from_user.id
    username = update.message.from_user.username or "unknown"

    if is_authorized(user_id):
        return True

    logger.warning(
        "Unauthorized access attempt | user_id=%s | username=%s",
        user_id,
        username,
    )
    await update.message.reply_text(
        f"Unauthorized.\nYour Telegram user ID is: {user_id}"
    )
    return False


# -----------------------------------------------------------------------------
# CLAUDE CODE CLI
# -----------------------------------------------------------------------------


def run_claude_cli(
    prompt: str,
    user_id: int,
    mode: str = "readonly",
    model: Optional[str] = None,
    isolated: bool = False,
) -> str:
    """Invoke Claude Code CLI and return the text response.

    Args:
        prompt: The user's message/instruction.
        user_id: Telegram user ID (for session tracking).
        mode: "readonly" or "edit".
        model: Model override (default: sonnet).
        isolated: If True, skip session resume (for /fix).
    """
    cmd = [
        CLAUDE_CLI,
        "-p",
        prompt,
        "--output-format",
        "json",
        "--model",
        model or DEFAULT_MODEL,
    ]

    if mode == "edit":
        cmd.extend(["--dangerously-skip-permissions"])
        cmd.extend(["--allowed-tools", EDIT_TOOLS])
    else:
        cmd.extend(["--dangerously-skip-permissions"])
        cmd.extend(["--allowed-tools", READ_ONLY_TOOLS])

    # Resume existing session for conversation continuity
    if not isolated and user_id in user_sessions:
        cmd.extend(["--resume", user_sessions[user_id]])

    logger.info(
        "Claude CLI | user=%s mode=%s model=%s isolated=%s",
        user_id,
        mode,
        model or DEFAULT_MODEL,
        isolated,
    )

    try:
        result = subprocess.run(
            cmd,
            cwd=NEXUSTRADER_PATH,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            shell=False,
            timeout=CLI_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return f"Claude timed out after {CLI_TIMEOUT}s. Try a more specific question."
    except FileNotFoundError:
        return (
            "Claude Code CLI not found. Ensure 'claude' is in PATH.\n"
            "Install: npm install -g @anthropic-ai/claude-code"
        )
    except Exception as exc:
        logger.exception("Claude CLI failed")
        return f"Claude CLI error: {exc}"

    stdout = (result.stdout or "").strip()
    stderr = (result.stderr or "").strip()

    if result.returncode != 0 and not stdout:
        return (
            f"Claude exited with code {result.returncode}.\n"
            f"{stderr[:2000] if stderr else 'No error output.'}"
        )

    # Parse JSON output to extract result text and session_id
    try:
        data = json.loads(stdout)
        session_id = data.get("session_id", "")
        if session_id and not isolated:
            user_sessions[user_id] = session_id

        # Extract text from the result
        response_text = data.get("result", "")
        if not response_text:
            # Fallback: try content blocks
            content = data.get("content", [])
            if isinstance(content, list):
                parts = [
                    b.get("text", "")
                    for b in content
                    if isinstance(b, dict) and b.get("type") == "text"
                ]
                response_text = "\n".join(p for p in parts if p)

        return response_text.strip() or "Claude returned an empty response."
    except json.JSONDecodeError:
        # If not valid JSON, return raw stdout (text mode fallback)
        return stdout or "No output from Claude."


# -----------------------------------------------------------------------------
# FAST COMMANDS (no CLI overhead — read files directly)
# -----------------------------------------------------------------------------

_NT = Path(NEXUSTRADER_PATH)


def fast_status() -> str:
    """Quick health summary from data files."""
    lines = ["NexusTrader Status"]
    lines.append("-" * 30)

    # Capital & positions
    pos_file = _NT / "data" / "open_positions.json"
    try:
        data = json.loads(pos_file.read_text(encoding="utf-8"))
        capital = data.get("capital", "?")
        peak = data.get("peak_capital", "?")
        positions = data.get("positions", [])
        lines.append(f"Capital: ${capital:,.2f}" if isinstance(capital, (int, float)) else f"Capital: {capital}")
        lines.append(f"Peak: ${peak:,.2f}" if isinstance(peak, (int, float)) else f"Peak: {peak}")
        lines.append(f"Open positions: {len(positions)}")
    except Exception as exc:
        lines.append(f"Positions: error reading ({exc})")

    # Recent errors from log
    log_file = _NT / "logs" / "nexus_trader.log"
    if log_file.exists():
        try:
            raw = log_file.read_text(encoding="utf-8", errors="replace")
            all_lines = raw.splitlines()
            errors = [
                ln for ln in all_lines[-500:]
                if "| ERROR |" in ln or "| CRITICAL |" in ln
            ]
            if errors:
                lines.append(f"\nRecent errors ({len(errors[-5:])} shown):")
                for e in errors[-5:]:
                    lines.append(f"  {e[:200]}")
            else:
                lines.append("\nNo recent errors.")
        except Exception:
            lines.append("\nLogs: unreadable")
    else:
        lines.append("\nLog file not found.")

    return "\n".join(lines)


def fast_positions() -> str:
    """Formatted open positions."""
    pos_file = _NT / "data" / "open_positions.json"
    try:
        data = json.loads(pos_file.read_text(encoding="utf-8"))
    except Exception as exc:
        return f"Error reading positions: {exc}"

    positions = data.get("positions", [])
    if not positions:
        capital = data.get("capital", "?")
        return f"No open positions.\nCapital: ${capital:,.2f}" if isinstance(capital, (int, float)) else f"No open positions.\nCapital: {capital}"

    lines = [f"Open Positions ({len(positions)}):"]
    for pos in positions:
        sym = pos.get("symbol", "?")
        direction = pos.get("direction", pos.get("side", "?"))
        entry = pos.get("entry_price", "?")
        size = pos.get("size_usdt", "?")
        sl = pos.get("stop_loss", "?")
        tp = pos.get("take_profit", "?")
        lines.append(
            f"\n{sym} ({direction})"
            f"\n  Entry: ${entry} | Size: ${size}"
            f"\n  SL: ${sl} | TP: ${tp}"
        )
    return "\n".join(lines)


def fast_logs(n: int = 30) -> str:
    """Last N non-DEBUG log lines."""
    log_file = _NT / "logs" / "nexus_trader.log"
    if not log_file.exists():
        return "Log file not found."

    try:
        raw = log_file.read_text(encoding="utf-8", errors="replace")
        all_lines = raw.splitlines()
        filtered = [ln for ln in all_lines if "| DEBUG |" not in ln]
        tail = filtered[-n:]
        return f"Last {len(tail)} log lines:\n\n" + "\n".join(tail)
    except Exception as exc:
        return f"Error reading logs: {exc}"


def fast_perf() -> str:
    """Model performance summary."""
    tracker_file = _NT / "data" / "model_perf_tracker.json"
    monitor_file = _NT / "data" / "trade_monitor.json"

    lines = ["Model Performance Summary"]
    lines.append("-" * 30)

    # Model perf from trade_monitor.json
    try:
        data = json.loads(monitor_file.read_text(encoding="utf-8"))
        model_perf = data.get("model_perf", {})
        for model_name, stats in model_perf.items():
            wins = stats.get("wins", 0)
            losses = stats.get("losses", 0)
            total = wins + losses
            wr = (wins / total * 100) if total > 0 else 0
            r_mults = stats.get("r_multiples", [])
            avg_r = sum(r_mults) / len(r_mults) if r_mults else 0
            lines.append(
                f"\n{model_name}: {wins}W/{losses}L "
                f"(WR {wr:.1f}%, Avg R {avg_r:.3f}, n={total})"
            )

        buckets = data.get("score_buckets", {})
        if buckets:
            lines.append("\nScore Buckets:")
            for bucket, stats in sorted(buckets.items()):
                w = stats.get("wins", 0)
                l = stats.get("losses", 0)
                t = w + l
                wr = (w / t * 100) if t > 0 else 0
                lines.append(f"  {bucket}: {w}W/{l}L (WR {wr:.1f}%, n={t})")
    except FileNotFoundError:
        lines.append("trade_monitor.json not found.")
    except Exception as exc:
        lines.append(f"Error: {exc}")

    return "\n".join(lines)


def fast_diff() -> str:
    """Git diff stats and current branch."""
    try:
        branch = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=NEXUSTRADER_PATH,
            capture_output=True,
            text=True,
            timeout=10,
        )
        diff = subprocess.run(
            ["git", "diff", "--stat"],
            cwd=NEXUSTRADER_PATH,
            capture_output=True,
            text=True,
            timeout=10,
        )
        staged = subprocess.run(
            ["git", "diff", "--staged", "--stat"],
            cwd=NEXUSTRADER_PATH,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception as exc:
        return f"Git error: {exc}"

    lines = [f"Branch: {(branch.stdout or '').strip()}"]

    diff_out = (diff.stdout or "").strip()
    if diff_out:
        lines.append(f"\nUnstaged changes:\n{diff_out}")
    else:
        lines.append("\nNo unstaged changes.")

    staged_out = (staged.stdout or "").strip()
    if staged_out:
        lines.append(f"\nStaged changes:\n{staged_out}")

    return "\n".join(lines)


# -----------------------------------------------------------------------------
# RESPONSE HELPERS
# -----------------------------------------------------------------------------


def split_at_boundaries(text: str, max_len: int) -> list[str]:
    """Split text into chunks at paragraph/line boundaries."""
    if len(text) <= max_len:
        return [text]

    chunks = []
    remaining = text
    while remaining:
        if len(remaining) <= max_len:
            chunks.append(remaining)
            break

        # Try splitting at paragraph boundary
        cut = remaining[:max_len].rfind("\n\n")
        if cut > max_len // 2:
            chunks.append(remaining[: cut + 1])
            remaining = remaining[cut + 2 :]
            continue

        # Try splitting at line boundary
        cut = remaining[:max_len].rfind("\n")
        if cut > max_len // 3:
            chunks.append(remaining[: cut + 1])
            remaining = remaining[cut + 1 :]
            continue

        # Hard cut
        chunks.append(remaining[:max_len])
        remaining = remaining[max_len:]

    return chunks


async def send_long_response(update: Update, text: str) -> None:
    """Send a potentially long response, splitting as needed."""
    if not update.message:
        return

    if len(text) > MAX_RESPONSE_LEN:
        full_len = len(text)
        text = text[:MAX_RESPONSE_LEN] + f"\n\n... (truncated, full response was {full_len} chars)"

    chunks = split_at_boundaries(text, TELEGRAM_CHUNK)
    for chunk in chunks:
        if chunk.strip():
            await update.message.reply_text(chunk)


# -----------------------------------------------------------------------------
# TELEGRAM HANDLERS
# -----------------------------------------------------------------------------

HELP_TEXT = (
    "NexusTrader Claude Code Bridge\n"
    "=" * 30 + "\n\n"
    "Ask anything — your message goes to Claude Code with full project context.\n\n"
    "Commands:\n"
    "/help — show this message\n"
    "/status — capital, positions, recent errors\n"
    "/positions — open positions detail\n"
    "/logs [N] — last N log lines (default 30)\n"
    "/perf — model win/loss summary\n"
    "/diff — git diff + current branch\n"
    "/fix <instruction> — make code changes (creates branch + commit)\n"
    "/opus <prompt> — query with Opus model (deeper analysis)\n"
    "/reset — clear conversation, start fresh\n\n"
    "Any other text is sent to Claude (read-only, Sonnet model)."
)


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await auth_guard(update):
        return
    user = update.message.from_user
    await update.message.reply_text(
        f"Claude Code bridge is running.\n"
        f"Your Telegram user ID: {user.id}\n\n"
        + HELP_TEXT
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await auth_guard(update):
        return
    await update.message.reply_text(HELP_TEXT)


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await auth_guard(update):
        return
    await update.message.reply_text(fast_status())


async def positions_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if not await auth_guard(update):
        return
    await update.message.reply_text(fast_positions())


async def logs_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await auth_guard(update):
        return
    # Parse optional line count: /logs 50
    text = (update.message.text or "").strip()
    parts = text.split(maxsplit=1)
    n = 30
    if len(parts) > 1 and parts[1].isdigit():
        n = min(int(parts[1]), 200)
    result = fast_logs(n)
    await send_long_response(update, result)


async def perf_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await auth_guard(update):
        return
    await send_long_response(update, fast_perf())


async def diff_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await auth_guard(update):
        return
    await send_long_response(update, fast_diff())


async def reset_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await auth_guard(update):
        return
    user_id = update.message.from_user.id
    old = user_sessions.pop(user_id, None)
    msg = "Session cleared. Next message starts a fresh conversation."
    if old:
        msg += f"\n(Previous session: {old[:12]}...)"
    await update.message.reply_text(msg)


async def fix_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await auth_guard(update):
        return

    text = (update.message.text or "").strip()
    instruction = text.split(maxsplit=1)[1] if len(text.split(maxsplit=1)) > 1 else ""
    if not instruction:
        await update.message.reply_text("Usage: /fix <what to fix or change>")
        return

    if _fix_lock.locked():
        await update.message.reply_text(
            "Another /fix operation is in progress. Please wait."
        )
        return

    timestamp = int(time.time())
    branch_name = f"fix/telegram-{timestamp}"

    full_prompt = (
        f"Create a new git branch named '{branch_name}' from the current HEAD, "
        f"then switch to it. Then do the following:\n\n"
        f"{instruction}\n\n"
        f"After making changes, run any relevant tests if appropriate, "
        f"then stage and commit with a descriptive message. "
        f"Do NOT push to remote. Do NOT merge to main. "
        f"Report what you changed concisely."
    )

    working_msg = await update.message.reply_text(
        f"Working on fix (branch: {branch_name})..."
    )

    async with _fix_lock:
        try:
            result = await asyncio.to_thread(
                run_claude_cli,
                full_prompt,
                update.message.from_user.id,
                mode="edit",
                isolated=True,
            )
        except Exception as exc:
            logger.exception("Fix command failed")
            result = f"Fix failed: {exc}"

    try:
        await working_msg.edit_text(
            f"Fix complete (branch: {branch_name}). See results below."
        )
    except Exception:
        pass

    await send_long_response(update, result)


async def opus_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await auth_guard(update):
        return

    text = (update.message.text or "").strip()
    prompt = text.split(maxsplit=1)[1] if len(text.split(maxsplit=1)) > 1 else ""
    if not prompt:
        await update.message.reply_text("Usage: /opus <your question>")
        return

    working_msg = await update.message.reply_text("Thinking (Opus)...")

    try:
        result = await asyncio.to_thread(
            run_claude_cli,
            prompt,
            update.message.from_user.id,
            mode="readonly",
            model="opus",
        )
    except Exception as exc:
        logger.exception("Opus query failed")
        result = f"Error: {exc}"

    try:
        await working_msg.edit_text("Done.")
    except Exception:
        pass

    await send_long_response(update, result)


async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle free-text messages — send to Claude Code in read-only mode."""
    if not await auth_guard(update):
        return
    if not update.message:
        return

    text = (update.message.text or "").strip()
    if not text:
        return

    user_id = update.message.from_user.id
    logger.info("Query | user_id=%s | text=%s", user_id, text[:100])

    working_msg = await update.message.reply_text("Working...")

    try:
        result = await asyncio.to_thread(
            run_claude_cli, text, user_id, mode="readonly"
        )
    except Exception as exc:
        logger.exception("Query failed")
        result = f"Error: {exc}"

    try:
        await working_msg.edit_text("Done.")
    except Exception:
        pass

    await send_long_response(update, result)


# -----------------------------------------------------------------------------
# MAIN
# -----------------------------------------------------------------------------


def main() -> None:
    logger.info("Starting Claude Code Telegram bridge")
    logger.info("NEXUSTRADER_PATH=%s", NEXUSTRADER_PATH)
    logger.info("Claude CLI=%s", CLAUDE_CLI)
    logger.info("Default model=%s", DEFAULT_MODEL)
    logger.info("Authorized users configured=%s", bool(AUTHORIZED_USERS))

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # Command handlers (registered before the catch-all text handler)
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("status", status_command))
    app.add_handler(CommandHandler("positions", positions_command))
    app.add_handler(CommandHandler("logs", logs_command))
    app.add_handler(CommandHandler("perf", perf_command))
    app.add_handler(CommandHandler("diff", diff_command))
    app.add_handler(CommandHandler("fix", fix_command))
    app.add_handler(CommandHandler("opus", opus_command))
    app.add_handler(CommandHandler("reset", reset_command))

    # Catch-all: free text goes to Claude Code (read-only)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

    app.run_polling()


if __name__ == "__main__":
    main()
