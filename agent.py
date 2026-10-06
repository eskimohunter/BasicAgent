#!/usr/bin/env python3
"""BA: a small, auditable, single-file LLM coding agent with a cross-platform TUI.

The agent talks to any OpenAI-compatible chat completions endpoint (streaming),
exposes a fixed set of tools, and requires explicit user approval before running
shell commands. Tab toggles between PLAN (read-only) and BUILD modes. Every
message, tool call, approval and result is written to a JSONL audit log.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from prompt_toolkit import PromptSession
from prompt_toolkit import print_formatted_text as pt_print
from prompt_toolkit.application import Application
from prompt_toolkit.application.current import get_app_session
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Layout, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.output.vt100 import Vt100_Output
from prompt_toolkit.shortcuts import clear
from prompt_toolkit.styles import Style

PYGMENTS_AVAILABLE = False
try:
    from pygments import lex
    from pygments.lexers import get_lexer_by_name
    from pygments.token import Token
    from pygments.util import ClassNotFound

    PYGMENTS_AVAILABLE = True
except ImportError:
    pass

APP_NAME = "BA"
VERSION = "0.5.0"
APP_DIR = Path(__file__).resolve().parent
DEFAULT_LOG_DIR = APP_DIR / "logs"
INSTRUCTIONS_FILE = APP_DIR / "Instructions.md"
DEFAULT_BASE_URL = "http://localhost:8080/v1"
TOOL_RESULT_LIMIT = 64_000
DISPLAY_PREVIEW_LIMIT = 2_000
DISPLAY_PREVIEW_LINES = 40
READ_MAX_LINES = 2_000
GREP_MAX_FILE_BYTES = 2_000_000
FETCH_MAX_BYTES = 100_000
REQUEST_TIMEOUT = 120.0
COMMAND_TIMEOUT = 60
MAX_STEPS = 25
SUMMARY_TIMEOUT = 5.0
SUMMARY_PROMPT = (
    "You explain shell commands to a junior developer. Reply with exactly one "
    "sentence describing what the command does and any important "
    "side effects. No markdown, no code formatting, no preamble. "
    "don't just repeat the command; explain it in plain English. "
    "If the command is dangerous make that clear"
)
INSTRUCTIONS_MAX_BYTES = 32_000
DEFAULT_EXCLUDES = {
    ".git",
    "__pycache__",
    "node_modules",
    ".venv",
    ".mypy_cache",
    ".ruff_cache",
    ".pytest_cache",
}

STYLE = Style.from_dict(
    {
        "mode.plan": "bg:#005f87 #ffffff bold",
        "mode.build": "bg:#00875f #ffffff bold",
        "toolbar": "bg:#303030 #b0b0b0",
        "user": "bold #5fafff",
        "agent": "bold #87d7ff",
        "tool": "bold #ffaf5f",
        "tool.result": "#9e9e9e",
        "error": "bold #ff5f5f",
        "warn": "#ffd75f",
        "info": "bold #5fd7ff",
        "approval": "#ffd75f",
        "approval.selected": "bg:#ffd75f #1c1c1c bold",
        "md.heading1": "bold #87d7ff",
        "md.heading2": "bold #5fd7ff",
        "md.heading3": "bold #5fafaf",
        "md.heading4": "bold #5faf87",
        "md.heading5": "#5fafaf",
        "md.heading6": "#6c6c6c",
        "md.bold": "bold",
        "md.italic": "italic",
        "md.bolditalic": "bold italic",
        "md.strike": "strike",
        "md.code": "bg:#303030 #e4e4e4",
        "md.codeblock": "#c8c8c8",
        "md.code.border": "#585858",
        "md.code.keyword": "bold #ff87d7",
        "md.code.string": "#87d787",
        "md.code.comment": "#6c6c6c italic",
        "md.code.number": "#87afff",
        "md.code.function": "#5fd7ff",
        "md.code.class": "bold #ffd75f",
        "md.code.operator": "#d7d7af",
        "md.code.punctuation": "#b0b0b0",
        "md.quote": "#9e9e9e italic",
        "md.quote.border": "#585858",
        "md.list": "#ffaf5f",
        "md.task": "#ffd75f",
        "md.link": "underline #5fafff",
        "md.link.url": "#6c6c6c",
        "md.hr": "#585858",
        "md.table": "#d0d0d0",
        "md.table.header": "bold #ffd75f",
        "md.table.border": "#585858",
        "ctx": "#b0b0b0",
        "ctx.warn": "#ffd75f",
        "ctx.hot": "bold #ff5f5f",
    }
)


class AgentError(Exception):
    """Raised for failures that should be shown to the user, not crash the agent."""


class ToolError(Exception):
    """Raised inside tool handlers; converted to an ERROR: result for the model."""


class Mode(Enum):
    PLAN = "plan"
    BUILD = "build"


@dataclass
class Config:
    base_url: str = DEFAULT_BASE_URL
    api_key: str = "none"
    model: str = "local-model"
    summary_base_url: str = DEFAULT_BASE_URL
    summary_api_key: str = "none"
    summary_model: str = "local-model"
    summary_model_defined: bool = False
    workspace: Path = field(default_factory=Path.cwd)
    project_instructions: str | None = None
    project_instructions_path: Path | None = None
    context_window: int = 0
    context_window_source: str = "unknown"


def parse_args(argv: list[str] | None = None) -> Config:
    parser = argparse.ArgumentParser(
        prog=APP_NAME.lower(),
        description="A basic, auditable LLM coding agent (OpenAI-compatible endpoint).",
    )
    parser.add_argument(
        "--base-url",
        default=os.environ.get("AGENT_BASE_URL")
        or os.environ.get("OPENAI_BASE_URL")
        or DEFAULT_BASE_URL,
        help=f"OpenAI-compatible API root (default: {DEFAULT_BASE_URL})",
    )
    parser.add_argument(
        "--api-key",
        default=os.environ.get("AGENT_API_KEY")
        or os.environ.get("OPENAI_API_KEY")
        or "none",
        help="API key sent as Bearer token (default: none)",
    )
    parser.add_argument(
        "--model",
        default=os.environ.get("AGENT_MODEL")
        or os.environ.get("OPENAI_MODEL")
        or "local-model",
        help="Model name to request",
    )
    parser.add_argument(
        "--summary-base-url",
        default=os.environ.get("AGENT_SUMMARY_BASE_URL"),
        help="API root for command summaries (default: --base-url)",
    )
    parser.add_argument(
        "--summary-api-key",
        default=os.environ.get("AGENT_SUMMARY_API_KEY"),
        help="Bearer token for command summaries (default: --api-key)",
    )
    parser.add_argument(
        "--summary-model",
        default=os.environ.get("AGENT_SUMMARY_MODEL"),
        help="Model for command summaries (default: --model)",
    )
    parser.add_argument("--workspace", default=".", help="Workspace root directory")
    parser.add_argument("--version", action="version", version=f"{APP_NAME} {VERSION}")
    ns = parser.parse_args(argv)
    env_context = os.environ.get("AGENT_CONTEXT_WINDOW", "").strip()
    context_window = 0
    if env_context:
        try:
            context_window = max(0, int(env_context))
        except ValueError:
            parser.error(f"AGENT_CONTEXT_WINDOW must be an integer, got {env_context!r}")
    return Config(
        base_url=ns.base_url.rstrip("/"),
        api_key=ns.api_key,
        model=ns.model,
        summary_base_url=(ns.summary_base_url or ns.base_url).rstrip("/"),
        summary_api_key=ns.summary_api_key or ns.api_key,
        summary_model=ns.summary_model or ns.model,
        summary_model_defined=bool(ns.summary_model),
        workspace=Path(ns.workspace).expanduser().resolve(),
        context_window=context_window,
        context_window_source="env" if context_window > 0 else "unknown",
    )


def probe_context_window(config: Config) -> tuple[int, str]:
    """Best-effort context window probe. Returns (tokens, source)."""
    timeout = min(REQUEST_TIMEOUT, 5.0)
    root = config.base_url.removesuffix("/v1")

    def get_json(url: str) -> Any:
        request = urllib.request.Request(url, headers={"Authorization": f"Bearer {config.api_key}"})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8", errors="replace"))

    try:
        props = get_json(f"{root}/props")
        if isinstance(props, dict):
            settings = props.get("default_generation_settings")
            n_ctx = settings.get("n_ctx") if isinstance(settings, dict) else None
            if isinstance(n_ctx, int) and n_ctx > 0:
                return n_ctx, "props"
    except (OSError, ValueError):
        pass

    try:
        models = get_json(f"{config.base_url}/models")
        entries = models.get("data") if isinstance(models, dict) else None
        if isinstance(entries, list) and entries:
            entry = next(
                (e for e in entries if isinstance(e, dict) and e.get("id") == config.model),
                None,
            )
            if entry is None:
                entry = entries[0] if isinstance(entries[0], dict) else {}
            for key in (
                "max_model_len",
                "context_length",
                "context_window",
                "max_context_length",
                "n_ctx",
            ):
                value = entry.get(key)
                if isinstance(value, int) and value > 0:
                    return value, "models"
            meta = entry.get("meta")
            n_ctx = meta.get("n_ctx") if isinstance(meta, dict) else None
            if isinstance(n_ctx, int) and n_ctx > 0:
                return n_ctx, "models"
    except (OSError, ValueError):
        pass

    return 0, "unknown"


class AuditLog:
    """Append-only JSONL log of everything the agent says, calls and executes."""

    def __init__(self):
        self.path: Path | None = None
        self._fh: Any = None
        try:
            DEFAULT_LOG_DIR.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
            self.path = DEFAULT_LOG_DIR / f"{stamp}-{os.getpid()}.jsonl"
            self._fh = self.path.open("a", encoding="utf-8")
        except OSError as exc:
            say(
                f"warning: cannot write audit log to {DEFAULT_LOG_DIR}: {exc}",
                "class:warn",
            )
            self.path = None
            self._fh = None

    def log(self, event: str, **fields: Any) -> None:
        if self._fh is None:
            return
        record = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "event": event,
        }
        record.update(fields)
        self._fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        self._fh.flush()

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None


def say(text: str = "", style: str = "") -> None:
    pt_print(FormattedText([(style, text)]), style=STYLE)


def stream_write(text: str) -> None:
    pt_print(text, end="", flush=True, style=STYLE)


SPINNER_FRAMES = "|/-\\"
SPINNER_INTERVAL = 0.08
SPINNER_GRACE = 0.15


class Spinner:
    """Animates a glyph on the current line until stopped.

    Assumes the cursor sits at column 0 of a fresh line (the turn banner is
    printed with a trailing newline). Writes straight to stdout; callers must
    stop the spinner before printing anything else.
    """

    def __init__(self) -> None:
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._spin, daemon=True)
        self._thread.start()

    def _spin(self) -> None:
        if self._stop.wait(SPINNER_GRACE):
            return
        index = 0
        while not self._stop.wait(SPINNER_INTERVAL):
            frame = SPINNER_FRAMES[index % len(SPINNER_FRAMES)]
            sys.stdout.write(f"\r{frame} ")
            sys.stdout.flush()
            index += 1

    def stop(self) -> None:
        thread = self._thread
        if thread is None:
            return
        self._stop.set()
        thread.join()
        self._thread = None
        sys.stdout.write("\r  \r")
        sys.stdout.flush()


RE_HEADING = re.compile(r"^(#{1,6})[ \t]+")
RE_FENCE = re.compile(r"^(`{3,}|~{3,})(.*)")
RE_HR = re.compile(r"^(-{3,}|\*{3,}|_{3,})[ \t]*$")
RE_QUOTE = re.compile(r"^>[ \t]?")
RE_TASK = re.compile(r"^( *)[-*+][ \t]+\[([ xX])\][ \t]+")
RE_ULIST = re.compile(r"^( *)[-*+][ \t]+")
RE_OLIST = re.compile(r"^( *)(\d{1,9})[.)][ \t]+")
RE_TABLE = re.compile(r"^ {0,3}\|")
RE_INLINE = re.compile(r"[*_`~\[!\\\n]")
RE_POTENTIAL_PREFIX = re.compile(
    r"^(?:"
    r"#{0,6}"
    r"|[-*+>|]"
    r"|-{2}"
    r"|\*{2}"
    r"|_{1,2}"
    r"|`{1,2}"
    r"|~{1,2}"
    r"|\d{1,9}[.)]?"
    r"|[-*+][ \t]+"
    r"|[-*+][ \t]+\[[ xX]?\]?"
    r")$"
)

_TOKEN_STYLE_CACHE: list[tuple[Any, str]] | None = None


def _token_style(token: Any) -> str:
    global _TOKEN_STYLE_CACHE
    if _TOKEN_STYLE_CACHE is None:
        _TOKEN_STYLE_CACHE = [
            (Token.Comment, "class:md.code.comment"),
            (Token.Keyword, "class:md.code.keyword"),
            (Token.Name.Function, "class:md.code.function"),
            (Token.Name.Class, "class:md.code.class"),
            (Token.String, "class:md.code.string"),
            (Token.Number, "class:md.code.number"),
            (Token.Operator, "class:md.code.operator"),
            (Token.Punctuation, "class:md.code.punctuation"),
        ]
    for prefix, style in _TOKEN_STYLE_CACHE:
        if token in prefix:
            return style
    return "class:md.codeblock"


def _make_lexer(lang: str) -> Any:
    if not PYGMENTS_AVAILABLE or not lang:
        return None
    try:
        return get_lexer_by_name(lang, stripnl=False, ensurenl=False)
    except ClassNotFound:
        return None


OSC8_CLOSE = "\x1b]8;;\x1b\\"


def osc8_supported() -> bool:
    """True when the active prompt_toolkit output understands OSC 8 sequences."""
    output = get_app_session().output
    vt = getattr(output, "vt100_output", output)
    return isinstance(vt, Vt100_Output) and getattr(vt, "term", None) != "dumb"


def osc8_open(url: str) -> str | None:
    """OSC 8 opener for an http(s) URL, with control characters stripped."""
    if not re.match(r"^https?://", url, re.IGNORECASE):
        return None
    cleaned = re.sub(r"[\x00-\x1f\x7f]", "", url)
    return f"\x1b]8;;{cleaned}\x1b\\"


class MarkdownStream:
    """Incremental markdown renderer for streamed assistant text.

    ``emit`` receives FormattedText-compatible fragments. Plain text is emitted
    as soon as it is unambiguous; only partial markers and unresolved spans are
    held back, so paragraphs keep streaming live. ``feed`` is called for every
    SSE delta and ``finalize`` once the stream ends.
    """

    def __init__(self, emit: Callable[[list[tuple[str, str]]], None], width: int | None = None):
        self.emit = emit
        self.width = width or shutil.get_terminal_size((80, 24)).columns
        self.buffer = ""
        self.at_line_start = True
        self.line_style = ""
        self.fence: str | None = None
        self.fence_lang = ""
        self.table_lines: list[str] = []
        self.in_table = False
        self._lexer: Any = None
        self.wrote = False
        self.ended_with_newline = True

    def feed(self, text: str) -> None:
        if text:
            self.buffer += text
            self._process(final=False)

    def finalize(self) -> None:
        self._process(final=True)
        if self.buffer:
            self._emit_text(self.buffer)
            self.buffer = ""
        if self.in_table:
            self._render_table()
            self.in_table = False
        self.fence = None
        if self.wrote and not self.ended_with_newline:
            self._write([("", "\n")])

    # -- internals ----------------------------------------------------------

    def _write(self, fragments: list[tuple[str, str]]) -> None:
        if not fragments:
            return
        self.wrote = True
        self.ended_with_newline = fragments[-1][1].endswith("\n")
        self.emit(fragments)

    def _emit_text(self, text: str, style: str | None = None) -> None:
        if text:
            self._write([(self.line_style if style is None else style, text)])

    def _process(self, final: bool) -> None:
        while True:
            if self.fence is not None:
                if not self._process_fence(final):
                    return
                continue
            if self.in_table:
                if not self._process_table(final):
                    return
                continue
            if self.at_line_start:
                if not self._consume_line_prefix(final):
                    return
                continue
            if not self._process_inline(final):
                return

    def _consume_line_prefix(self, final: bool) -> bool:
        if not self.buffer:
            if not final:
                return False
            self.at_line_start = False
            return True
        stripped = self.buffer.lstrip(" ")
        fence_match = RE_FENCE.match(stripped)
        if fence_match:
            newline = self.buffer.find("\n")
            if newline == -1 and not final:
                return False
            self.buffer = self.buffer[newline + 1 :] if newline != -1 else ""
            self.fence = fence_match.group(1)
            info = fence_match.group(2).strip()
            self.fence_lang = info.split()[0] if info else ""
            self._lexer = _make_lexer(self.fence_lang)
            label = f" {self.fence_lang}" if self.fence_lang else ""
            self._write([("class:md.code.border", f"┌─{label}\n")])
            self.at_line_start = True
            return True
        if self._at_potential_prefix(final):
            return False
        if RE_TABLE.match(stripped):
            self.in_table = True
            self.table_lines = []
            return True
        heading = RE_HEADING.match(stripped)
        if heading:
            self.buffer = stripped[heading.end() :]
            self.line_style = f"class:md.heading{len(heading.group(1))}"
            self.at_line_start = False
            return True
        newline = self.buffer.find("\n")
        first_line = self.buffer[:newline] if newline != -1 else self.buffer
        if RE_HR.match(first_line) and (newline != -1 or final):
            self.buffer = self.buffer[newline + 1 :] if newline != -1 else ""
            self._write([("class:md.hr", "─" * min(self.width, 40) + "\n")])
            self.at_line_start = True
            return True
        if newline == -1 and not final and re.fullmatch(r" {0,3}[-*_]+[ \t]*", self.buffer):
            return False
        quote = RE_QUOTE.match(stripped)
        if quote:
            self.buffer = stripped[quote.end() :]
            self.line_style = "class:md.quote"
            self._write([("class:md.quote.border", "│ ")])
            self.at_line_start = False
            return True
        task = RE_TASK.match(stripped)
        if task:
            indent, mark = task.group(1), task.group(2)
            self.buffer = stripped[task.end() :]
            box = "☑ " if mark in "xX" else "☐ "
            self._write([("class:md.list", indent), ("class:md.task", box)])
            self.line_style = ""
            self.at_line_start = False
            return True
        ordered = RE_OLIST.match(stripped)
        if ordered:
            self.buffer = stripped[ordered.end() :]
            self._write([("class:md.list", f"{ordered.group(1)}{ordered.group(2)}. ")])
            self.line_style = ""
            self.at_line_start = False
            return True
        unordered = RE_ULIST.match(stripped)
        if unordered:
            self.buffer = stripped[unordered.end() :]
            self._write([("class:md.list", f"{unordered.group(1)}• ")])
            self.line_style = ""
            self.at_line_start = False
            return True
        self.at_line_start = False
        return True

    def _at_potential_prefix(self, final: bool) -> bool:
        if final:
            return False
        stripped = self.buffer.lstrip(" ")
        if not stripped:
            return True
        return bool(RE_POTENTIAL_PREFIX.match(stripped))

    def _process_inline(self, final: bool) -> bool:
        if not self.buffer:
            return False
        match = RE_INLINE.search(self.buffer)
        if match is None:
            self._emit_text(self.buffer)
            self.buffer = ""
            return False
        if match.start() > 0:
            self._emit_text(self.buffer[: match.start()])
            self.buffer = self.buffer[match.start() :]
        char = self.buffer[0]
        if char == "\n":
            self._write([("", "\n")])
            self.buffer = self.buffer[1:]
            self.at_line_start = True
            self.line_style = ""
            return True
        if char == "\\":
            if len(self.buffer) < 2:
                if not final:
                    return False
                self._emit_text("\\")
                self.buffer = ""
                return True
            self._emit_text(self.buffer[1])
            self.buffer = self.buffer[2:]
            return True
        if char == "`":
            return self._inline_code(final)
        if char in "*_~":
            return self._inline_emphasis(final, char)
        if char == "!":
            if len(self.buffer) >= 2 and self.buffer[1] == "[":
                return self._inline_link(final)
            if len(self.buffer) < 2 and not final:
                return False
            self._emit_text("!")
            self.buffer = self.buffer[1:]
            return True
        if char == "[":
            return self._inline_link(final)
        return False

    def _inline_code(self, final: bool) -> bool:
        run = len(self.buffer) - len(self.buffer.lstrip("`"))
        marker = "`" * run
        line_end = self.buffer.find("\n")
        limit = line_end if line_end != -1 else len(self.buffer)
        closer = self.buffer.find(marker, run, limit)
        if closer == -1:
            if line_end == -1 and not final:
                return False
            self._emit_text(marker)
            self.buffer = self.buffer[run:]
            return True
        self._write([("class:md.code", self.buffer[run:closer])])
        self.buffer = self.buffer[closer + run :]
        return True

    def _inline_emphasis(self, final: bool, char: str) -> bool:
        run = 0
        while run < len(self.buffer) and self.buffer[run] == char:
            run += 1
        if char == "~":
            if run < 2:
                if run == len(self.buffer) and not final:
                    return False
                self._emit_text("~")
                self.buffer = self.buffer[1:]
                return True
            marker = "~~"
            style = "class:md.strike"
        else:
            length = min(run, 3)
            marker = char * length
            style = {
                1: "class:md.italic",
                2: "class:md.bold",
                3: "class:md.bolditalic",
            }[length]
        line_end = self.buffer.find("\n")
        limit = line_end if line_end != -1 else len(self.buffer)
        closer = self.buffer.find(marker, run, limit)
        if closer == -1:
            if line_end == -1 and not final:
                return False
            self._emit_text(marker)
            self.buffer = self.buffer[len(marker) :]
            return True
        if char == "_":
            before = self.buffer[run - 1] if run > 0 else ""
            after_at = closer + len(marker)
            after = self.buffer[after_at] if after_at < len(self.buffer) else ""
            if before.isalnum() or after.isalnum():
                self._emit_text(marker)
                self.buffer = self.buffer[len(marker) :]
                return True
        self._write([(style, self.buffer[run:closer])])
        self.buffer = self.buffer[closer + len(marker) :]
        return True

    def _inline_link(self, final: bool) -> bool:
        image = self.buffer.startswith("![")
        start = 2 if image else 1
        line_end = self.buffer.find("\n")
        limit = line_end if line_end != -1 else len(self.buffer)
        bracket = self.buffer.find("](", start)
        if bracket != -1 and bracket < limit:
            paren = self.buffer.find(")", bracket + 2)
            if paren != -1 and paren < limit:
                text = self.buffer[start:bracket]
                url = self.buffer[bracket + 2 : paren]
                self._write(self._link_fragments(image, text, url))
                self.buffer = self.buffer[paren + 1 :]
                return True
        if line_end == -1 and not final:
            return False
        self._emit_text("!" if image else "[")
        self.buffer = self.buffer[1:]
        return True

    def _link_fragments(self, image: bool, text: str, url: str) -> list[tuple[str, str]]:
        if not text:
            return [("class:md.link", url)]
        suffix = [("class:md.link.url", f" ({url})")]
        opener = None if image else osc8_open(url)
        if opener and osc8_supported():
            return [
                ("[ZeroWidthEscape]", opener),
                ("class:md.link", text),
                ("[ZeroWidthEscape]", OSC8_CLOSE),
                *suffix,
            ]
        return [("class:md.link", text), *suffix]

    def _process_fence(self, final: bool) -> bool:
        if "\n" not in self.buffer:
            if not final:
                return False
            if self.buffer:
                self._write_code_line(self.buffer)
                self.buffer = ""
            self._write([("class:md.code.border", "└─\n")])
            self.fence = None
            self._lexer = None
            self.at_line_start = True
            return True
        line, self.buffer = self.buffer.split("\n", 1)
        if line.strip().startswith(self.fence):
            self._write([("class:md.code.border", "└─\n")])
            self.fence = None
            self._lexer = None
            self.at_line_start = True
            return True
        self._write_code_line(line)
        return True

    def _write_code_line(self, line: str) -> None:
        fragments: list[tuple[str, str]] = [("class:md.code.border", "│ ")]
        if self._lexer is not None:
            fragments.extend((_token_style(token), value) for token, value in lex(line, self._lexer))
        else:
            fragments.append(("class:md.codeblock", line))
        fragments.append(("", "\n"))
        self._write(fragments)

    def _process_table(self, final: bool) -> bool:
        while "\n" in self.buffer:
            line, self.buffer = self.buffer.split("\n", 1)
            if line.lstrip().startswith("|"):
                self.table_lines.append(line)
                continue
            self._render_table()
            self.in_table = False
            self.buffer = line + "\n" + self.buffer
            return True
        if final:
            if self.buffer and self.buffer.lstrip().startswith("|"):
                self.table_lines.append(self.buffer)
                self.buffer = ""
            self._render_table()
            self.in_table = False
            return True
        return False

    def _render_table(self) -> None:
        rows: list[list[str]] = []
        for raw in self.table_lines:
            cells = [cell.strip() for cell in raw.strip().strip("|").split("|")]
            if cells and all(re.fullmatch(r":?-{3,}:?", cell) for cell in cells if cell):
                continue
            rows.append(cells)
        self.table_lines = []
        if not rows:
            return
        columns = max(len(row) for row in rows)
        for row in rows:
            row.extend([""] * (columns - len(row)))
        cap = max(8, self.width // max(1, columns) - 3)
        widths = [max(3, min(max(len(row[i]) for row in rows), cap)) for i in range(columns)]

        def rule(left: str, mid: str, right: str) -> str:
            return left + mid.join("─" * (width + 2) for width in widths) + right

        self._write([("class:md.table.border", rule("┌", "┬", "┐") + "\n")])
        for index, row in enumerate(rows):
            fragments: list[tuple[str, str]] = [("class:md.table.border", "│")]
            for cell, width in zip(row, widths):
                style = "class:md.table.header" if index == 0 else "class:md.table"
                fragments.append((style, f" {cell[:width].ljust(width)} "))
                fragments.append(("class:md.table.border", "│"))
            fragments.append(("", "\n"))
            self._write(fragments)
            if index == 0 and len(rows) > 1:
                self._write([("class:md.table.border", rule("├", "┼", "┤") + "\n")])
        self._write([("class:md.table.border", rule("└", "┴", "┘") + "\n")])


def cap(text: str, limit: int = TOOL_RESULT_LIMIT) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n...[truncated {len(text) - limit} chars]"


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: Callable[..., str]
    writes: bool = False
    requires_approval: bool = False


def tool_schema(tool: Tool) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.parameters,
        },
    }


def object_schema(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": required}


def resolve_path(config: Config, raw: str) -> Path:
    if not raw:
        raise ToolError("path is required")
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = config.workspace / path
    path = path.resolve()
    try:
        path.relative_to(config.workspace)
    except ValueError as exc:
        raise ToolError(f"path is outside the workspace ({config.workspace})") from exc
    return path


def is_excluded(path: Path, root: Path) -> bool:
    try:
        rel = path.relative_to(root)
    except ValueError:
        return False
    return any(part in DEFAULT_EXCLUDES for part in rel.parts[:-1])


TEXT_BOMS = (
    (b"\xff\xfe\x00\x00", "utf-32"),
    (b"\x00\x00\xfe\xff", "utf-32"),
    (b"\xef\xbb\xbf", "utf-8-sig"),
    (b"\xff\xfe", "utf-16"),
    (b"\xfe\xff", "utf-16"),
)


def looks_like_utf16(raw: bytes) -> str | None:
    """Guess a BOM-less UTF-16 byte order from NUL-byte positions."""
    sample = raw[:8192]
    if len(sample) < 4:
        return None
    even_nul = sample[0::2].count(0) / len(sample[0::2])
    odd_nul = sample[1::2].count(0) / len(sample[1::2])
    if odd_nul > 0.3 and even_nul < 0.05:
        return "utf-16-le"
    if even_nul > 0.3 and odd_nul < 0.05:
        return "utf-16-be"
    return None


def decode_text(raw: bytes) -> str | None:
    """Decode file bytes as text, or return None if they look binary."""
    for bom, encoding in TEXT_BOMS:
        if raw.startswith(bom):
            return raw.decode(encoding, errors="replace")
    if b"\x00" not in raw[:8192]:
        return raw.decode("utf-8", errors="replace")
    encoding = looks_like_utf16(raw)
    if encoding:
        return raw.decode(encoding, errors="replace")
    return None


def tool_read_file(
    config: Config, path: str, start_line: int = 1, end_line: int = 0
) -> str:
    target = resolve_path(config, path)
    if not target.exists():
        raise ToolError(f"file not found: {target}")
    if not target.is_file():
        raise ToolError(f"not a file: {target}")
    raw = target.read_bytes()
    text = decode_text(raw)
    if text is None:
        raise ToolError(f"refusing to read binary file: {target}")
    lines = text.splitlines()
    total = len(lines)
    start = max(1, int(start_line))
    end = int(end_line) if int(end_line) > 0 else total
    end = min(end, total)
    if start > end:
        raise ToolError(f"invalid line range {start}-{end} for {total}-line file")
    selected = lines[start - 1 : end]
    truncated = False
    if len(selected) > READ_MAX_LINES:
        selected = selected[:READ_MAX_LINES]
        truncated = True
    body = "\n".join(f"{n:>6}\t{line}" for n, line in enumerate(selected, start=start))
    header = f"{target} (lines {start}-{start + len(selected) - 1} of {total})"
    if truncated:
        header += f" [showing first {READ_MAX_LINES} lines]"
    return f"{header}\n{body}"


def tool_list_dir(config: Config, path: str = ".") -> str:
    target = resolve_path(config, path)
    if not target.exists():
        raise ToolError(f"directory not found: {target}")
    if not target.is_dir():
        raise ToolError(f"not a directory: {target}")
    entries = sorted(target.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower()))
    if not entries:
        return f"{target} is empty"
    lines = []
    for entry in entries:
        if entry.is_dir():
            lines.append(f"{entry.name}/")
        else:
            try:
                size = entry.stat().st_size
            except OSError:
                size = 0
            lines.append(f"{entry.name}\t{size} B")
    return f"{target} ({len(entries)} entries)\n" + "\n".join(lines)


def tool_grep(
    config: Config,
    pattern: str,
    path: str = ".",
    include: str = "*",
    ignore_case: bool = False,
    max_results: int = 100,
) -> str:
    base = resolve_path(config, path)
    if not base.exists():
        raise ToolError(f"path not found: {base}")
    flags = re.IGNORECASE if ignore_case else 0
    try:
        rx = re.compile(pattern, flags)
    except re.error as exc:
        raise ToolError(f"invalid regular expression: {exc}") from exc
    limit = max(1, min(int(max_results), 1000))
    if base.is_file():
        candidates = [base]
    else:
        candidates = [
            p for p in sorted(base.rglob(include)) if p.is_file() and not is_excluded(p, base)
        ]
    hits: list[str] = []
    for candidate in candidates:
        if len(hits) >= limit:
            break
        try:
            raw = candidate.read_bytes()
        except OSError:
            continue
        if len(raw) > GREP_MAX_FILE_BYTES:
            continue
        text = decode_text(raw)
        if text is None:
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            if rx.search(line):
                rel = candidate.relative_to(base) if base.is_dir() else candidate.name
                hits.append(f"{rel}:{lineno}: {line.strip()[:300]}")
                if len(hits) >= limit:
                    break
    if not hits:
        return f"no matches for {pattern!r} under {base}"
    suffix = " (limit reached)" if len(hits) >= limit else ""
    return f"{len(hits)} match(es){suffix}\n" + "\n".join(hits)


def tool_fetch_url(config: Config, url: str, max_bytes: int = FETCH_MAX_BYTES) -> str:
    if not re.match(r"^https?://", url, re.IGNORECASE):
        raise ToolError("only http(s) URLs are supported")
    limit = max(1000, min(int(max_bytes), 1_000_000))
    request = urllib.request.Request(url, headers={"User-Agent": f"{APP_NAME}/{VERSION}"})
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
            raw = response.read(limit + 1)
            content_type = response.headers.get_content_type()
            charset = response.headers.get_content_charset() or "utf-8"
            status = response.status
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:1000]
        return f"ERROR: HTTP {exc.code} for {url}\n{body}"
    except urllib.error.URLError as exc:
        raise ToolError(f"cannot fetch {url}: {exc.reason}") from exc
    truncated = len(raw) > limit
    text = raw[:limit].decode(charset, errors="replace")
    suffix = f"\n...[truncated at {limit} bytes]" if truncated else ""
    return f"HTTP {status} {content_type}\n{text}{suffix}"


def tool_write_file(config: Config, path: str, content: str) -> str:
    target = resolve_path(config, path)
    if target.exists() and target.is_dir():
        raise ToolError(f"path is a directory: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return f"wrote {len(content.encode('utf-8'))} bytes to {target}"


def tool_edit_file(
    config: Config,
    path: str,
    old_string: str,
    new_string: str,
    replace_all: bool = False,
) -> str:
    if not old_string:
        raise ToolError("old_string must not be empty")
    target = resolve_path(config, path)
    if not target.exists() or not target.is_file():
        raise ToolError(f"file not found: {target}")
    text = target.read_text(encoding="utf-8", errors="replace")
    count = text.count(old_string)
    if count == 0:
        raise ToolError(f"old_string not found in {target}")
    if count > 1 and not replace_all:
        raise ToolError(
            f"old_string appears {count} times in {target}; "
            "add more context or set replace_all=true"
        )
    replaced = count if replace_all else 1
    updated = (
        text.replace(old_string, new_string)
        if replace_all
        else text.replace(old_string, new_string, 1)
    )
    target.write_text(updated, encoding="utf-8")
    return f"edited {target}: replaced {replaced} occurrence(s)"


def tool_run_command(config: Config, command: str, timeout_seconds: int = 0) -> str:
    if not command.strip():
        raise ToolError("command must not be empty")
    timeout = int(timeout_seconds) if int(timeout_seconds) > 0 else COMMAND_TIMEOUT
    timeout = max(1, min(timeout, 600))
    try:
        proc = subprocess.run(
            command,
            shell=True,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            cwd=str(config.workspace),
        )
    except subprocess.TimeoutExpired as exc:
        partial = ""
        if exc.stdout:
            partial += exc.stdout if isinstance(exc.stdout, str) else exc.stdout.decode("utf-8", "replace")
        if exc.stderr:
            partial += "\n" + (
                exc.stderr if isinstance(exc.stderr, str) else exc.stderr.decode("utf-8", "replace")
            )
        return f"ERROR: command timed out after {timeout}s\n{partial}".rstrip()
    body = proc.stdout or ""
    if (proc.stderr or "").strip():
        body += ("\n" if body else "") + "--- stderr ---\n" + proc.stderr
    return f"exit code: {proc.returncode}\n{body}".rstrip()


def build_tools(config: Config) -> dict[str, Tool]:
    tools = [
        Tool(
            "read_file",
            "Read a text file (UTF-8/16/32) and return numbered lines. "
            "Optionally limit to a line range (end_line=0 means end of file).",
            object_schema(
                {
                    "path": {"type": "string", "description": "File path, relative to the workspace"},
                    "start_line": {"type": "integer", "description": "First line (1-based)"},
                    "end_line": {"type": "integer", "description": "Last line (0 = end of file)"},
                },
                ["path"],
            ),
            lambda **kw: tool_read_file(config, **kw),
        ),
        Tool(
            "list_dir",
            "List the entries of a directory. Directories are marked with a trailing slash.",
            object_schema(
                {"path": {"type": "string", "description": "Directory path (default: workspace root)"}},
                [],
            ),
            lambda **kw: tool_list_dir(config, **kw),
        ),
        Tool(
            "grep",
            "Search files for a regular expression and return matching lines with line numbers.",
            object_schema(
                {
                    "pattern": {"type": "string", "description": "Python regular expression"},
                    "path": {"type": "string", "description": "File or directory to search"},
                    "include": {"type": "string", "description": "Glob for file names, e.g. *.py"},
                    "ignore_case": {"type": "boolean", "description": "Case-insensitive search"},
                    "max_results": {"type": "integer", "description": "Maximum matching lines"},
                },
                ["pattern"],
            ),
            lambda **kw: tool_grep(config, **kw),
        ),
        Tool(
            "fetch_url",
            "Fetch an http(s) URL and return the response body as text (read-only).",
            object_schema(
                {
                    "url": {"type": "string", "description": "Absolute http(s) URL"},
                    "max_bytes": {"type": "integer", "description": "Maximum bytes to return"},
                },
                ["url"],
            ),
            lambda **kw: tool_fetch_url(config, **kw),
        ),
        Tool(
            "write_file",
            "Create or overwrite a file with the given content. Creates parent directories.",
            object_schema(
                {
                    "path": {"type": "string", "description": "File path, relative to the workspace"},
                    "content": {"type": "string", "description": "Full file content"},
                },
                ["path", "content"],
            ),
            lambda **kw: tool_write_file(config, **kw),
            writes=True,
        ),
        Tool(
            "edit_file",
            "Replace an exact string in a file. Fails if the string is missing or ambiguous.",
            object_schema(
                {
                    "path": {"type": "string", "description": "File path, relative to the workspace"},
                    "old_string": {"type": "string", "description": "Exact text to replace"},
                    "new_string": {"type": "string", "description": "Replacement text"},
                    "replace_all": {"type": "boolean", "description": "Replace every occurrence"},
                },
                ["path", "old_string", "new_string"],
            ),
            lambda **kw: tool_edit_file(config, **kw),
            writes=True,
        ),
        Tool(
            "run_command",
            "Run a shell command in the workspace. The user must approve every command before it runs.",
            object_schema(
                {
                    "command": {"type": "string", "description": "Shell command to execute"},
                    "timeout_seconds": {"type": "integer", "description": "Timeout in seconds"},
                },
                ["command"],
            ),
            lambda **kw: tool_run_command(config, **kw),
            writes=True,
            requires_approval=True,
        ),
    ]
    return {tool.name: tool for tool in tools}


def find_agents_file(workspace: Path) -> Path | None:
    """Return the first Agents.md in the workspace root, case-insensitively."""
    try:
        entries = sorted(workspace.iterdir(), key=lambda entry: entry.name)
    except OSError:
        return None
    for entry in entries:
        if entry.name.lower() == "agents.md" and entry.is_file():
            return entry
    return None


def load_project_instructions(config: Config) -> None:
    """Load workspace Agents.md into the config, warning on unreadable files."""
    path = find_agents_file(config.workspace)
    if path is None:
        return
    try:
        raw = path.read_bytes()
    except OSError as exc:
        say(f"warning: cannot read {path}: {exc}", "class:warn")
        return
    text = decode_text(raw)
    if text is None:
        say(f"warning: ignoring binary file {path}", "class:warn")
        return
    config.project_instructions = cap(text, INSTRUCTIONS_MAX_BYTES)
    config.project_instructions_path = path


def build_system_prompt(config: Config, mode: Mode) -> str:
    base = (
        f"You are {APP_NAME}, a coding agent running in the user's terminal.\n"
        f"Workspace root: {config.workspace}\n\n"
        "Rules:\n"
        "- Inspect files with read_file, list_dir and grep before making changes.\n"
        "- Use write_file and edit_file to modify files.\n"
        "- To run a shell command, call run_command. The user must approve every command; "
        "never claim a command ran until you see the tool result.\n"
        "- Tool results beginning with 'ERROR:' indicate failure; read them and adjust.\n"
        "- Keep answers concise and grounded in tool output. Do not invent file contents.\n"
    )
    if config.project_instructions:
        base += (
            f"\n\nProject instructions ({config.project_instructions_path}):\n"
            f"{config.project_instructions}\n"
        )
    if mode is Mode.PLAN:
        base += (
            "\nCurrent mode: PLAN (read-only). Available tools: read_file, list_dir, grep, "
            "fetch_url. You cannot write files or run commands. Investigate the codebase and "
            "produce a concrete plan; ask the user to switch to build mode (Tab) before implementing."
        )
    else:
        base += (
            "\nCurrent mode: BUILD. You may read, write and edit files, and propose shell "
            "commands. Every shell command requires explicit user approval."
        )
    return base


# ---------------------------------------------------------------------------
# Approvals
# ---------------------------------------------------------------------------


class Approvals:
    """Interactive shell-command approval via a Yes/No button prompt.

    ``allowed`` holds pre-approved exact command strings. It is empty for now and
    will be populated from a config file in a future change.
    """

    def __init__(self, session: PromptSession, config: Config):
        self.session = session
        self.config = config
        self.allowed: set[str] = set()

    def ask(self, command: str) -> bool:
        lines = [
            "",
            "+-- run_command " + "-" * 47,
            f"| cwd: {self.config.workspace}",
        ]
        for command_line in command.splitlines() or [""]:
            lines.append(f"| $ {command_line}")
        lines.append("+" + "-" * 62)
        say("\n".join(lines), "class:warn")

        options = [("Yes", True), ("No", False)]
        selected = {"index": 0}

        def render() -> FormattedText:
            fragments: list[tuple[str, str]] = [("class:approval", " Run?  ")]
            for index, (label, _) in enumerate(options):
                if index:
                    fragments.append(("", "  "))
                style = (
                    "class:approval.selected"
                    if index == selected["index"]
                    else "class:approval"
                )
                fragments.append((style, f"[ {label} ]"))
            return FormattedText(fragments)

        bindings = KeyBindings()

        @bindings.add("left")
        @bindings.add("right")
        def _move(event: Any) -> None:
            selected["index"] = 1 - selected["index"]
            event.app.invalidate()

        @bindings.add("enter")
        def _confirm(event: Any) -> None:
            event.app.exit(result=options[selected["index"]][1])

        @bindings.add("c-c")
        @bindings.add("c-d")
        def _cancel(event: Any) -> None:
            event.app.exit(result=False)

        confirmation: Application[bool] = Application(
            layout=Layout(
                Window(FormattedTextControl(render, show_cursor=False, focusable=True))
            ),
            key_bindings=bindings,
            style=STYLE,
            full_screen=False,
            input=self.session.app.input,
            output=self.session.app.output,
        )
        try:
            return confirmation.run()
        except (KeyboardInterrupt, EOFError):
            return False


# ---------------------------------------------------------------------------
# LLM client (stdlib HTTP + SSE)
# ---------------------------------------------------------------------------


@dataclass
class StreamResult:
    message: dict[str, Any]
    interrupted: bool = False
    usage: dict[str, Any] | None = None


def parse_non_stream_response(body: str) -> tuple[dict[str, Any], dict[str, Any] | None]:
    try:
        obj = json.loads(body)
    except json.JSONDecodeError as exc:
        raise AgentError(f"unexpected non-JSON response: {body[:500]}") from exc
    if isinstance(obj, dict) and "error" in obj:
        raise AgentError(f"server error: {json.dumps(obj['error'])[:500]}")
    choices = obj.get("choices") or []
    if not choices:
        raise AgentError(f"no choices in response: {body[:500]}")
    msg = choices[0].get("message") or {}
    tool_calls = []
    for index, call in enumerate(msg.get("tool_calls") or []):
        fn = call.get("function") or {}
        arguments = fn.get("arguments")
        if not isinstance(arguments, str):
            arguments = json.dumps(arguments or {})
        tool_calls.append(
            {
                "id": call.get("id") or f"call_{index}",
                "type": "function",
                "function": {"name": fn.get("name") or "", "arguments": arguments},
            }
        )
    message: dict[str, Any] = {"role": "assistant", "content": msg.get("content") or ""}
    if tool_calls:
        message["tool_calls"] = tool_calls
    usage = obj.get("usage")
    return message, usage if isinstance(usage, dict) else None


def finalize_tool_calls(calls: dict[int, dict[str, str]]) -> list[dict[str, Any]]:
    result = []
    for index in sorted(calls):
        acc = calls[index]
        if not acc["name"]:
            continue
        arguments = acc["arguments"].strip() or "{}"
        result.append(
            {
                "id": acc["id"] or f"call_{index}",
                "type": "function",
                "function": {"name": acc["name"], "arguments": arguments},
            }
        )
    return result


def stream_chat(app: App, on_delta: Callable[[str], None]) -> StreamResult:
    config = app.config
    payload: dict[str, Any] = {
        "model": config.model,
        "messages": app.messages,
        "stream": True,
    }
    schemas = app.allowed_tool_schemas()
    if schemas:
        payload["tools"] = schemas
        payload["tool_choice"] = "auto"

    def open_chat(with_stream_options: bool) -> Any:
        if with_stream_options:
            payload["stream_options"] = {"include_usage": True}
        else:
            payload.pop("stream_options", None)
        request = urllib.request.Request(
            f"{config.base_url}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {config.api_key}",
            },
            method="POST",
        )
        return urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT)

    def failure(exc: Exception) -> AgentError:
        if isinstance(exc, urllib.error.HTTPError):
            body = exc.read().decode("utf-8", errors="replace")[:2000]
            return AgentError(f"HTTP {exc.code} from {config.base_url}: {body}")
        if isinstance(exc, urllib.error.URLError):
            return AgentError(f"cannot reach {config.base_url}: {exc.reason}")
        return AgentError(f"request failed: {exc}")

    try:
        response = open_chat(app.stream_usage)
    except urllib.error.HTTPError as exc:
        if not (app.stream_usage and exc.code == 400):
            raise failure(exc) from exc
        app.stream_usage = False
        exc.close()
        try:
            response = open_chat(False)
        except (urllib.error.HTTPError, urllib.error.URLError, OSError) as retry_exc:
            raise failure(retry_exc) from retry_exc
    except (urllib.error.URLError, OSError) as exc:
        raise failure(exc) from exc

    content_parts: list[str] = []
    calls: dict[int, dict[str, str]] = {}
    usage: dict[str, Any] | None = None
    interrupted = False
    try:
        with response:
            content_type = (response.headers.get("Content-Type") or "").lower()
            if "text/event-stream" not in content_type:
                body = response.read().decode("utf-8", errors="replace")
                message, usage = parse_non_stream_response(body)
                if message.get("content"):
                    on_delta(message["content"])
                return StreamResult(message=message, usage=usage)
            for raw_line in response:
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line.startswith("data:"):
                    continue
                chunk_text = line[5:].strip()
                if chunk_text == "[DONE]":
                    break
                try:
                    chunk = json.loads(chunk_text)
                except json.JSONDecodeError:
                    continue
                chunk_usage = chunk.get("usage")
                if isinstance(chunk_usage, dict):
                    usage = chunk_usage
                choices = chunk.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta") or choices[0].get("message") or {}
                text = delta.get("content")
                if text:
                    content_parts.append(text)
                    on_delta(text)
                for tool_call in delta.get("tool_calls") or []:
                    index = int(tool_call.get("index", 0))
                    acc = calls.setdefault(index, {"id": "", "name": "", "arguments": ""})
                    if tool_call.get("id"):
                        acc["id"] = tool_call["id"]
                    fn = tool_call.get("function") or {}
                    if fn.get("name"):
                        if not acc["name"]:
                            acc["name"] = fn["name"]
                        elif not acc["name"].endswith(fn["name"]):
                            acc["name"] += fn["name"]
                    if fn.get("arguments"):
                        acc["arguments"] += fn["arguments"]
    except KeyboardInterrupt:
        interrupted = True

    content = "".join(content_parts)
    if interrupted:
        return StreamResult(
            message={"role": "assistant", "content": content + "\n[interrupted by user]"},
            interrupted=True,
            usage=usage,
        )
    tool_calls = finalize_tool_calls(calls)
    message = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return StreamResult(message=message, usage=usage)


# ---------------------------------------------------------------------------
# Agent loop
# ---------------------------------------------------------------------------


def summarise_args(call: dict[str, Any]) -> str:
    raw = call["function"].get("arguments") or ""
    try:
        args = json.loads(raw)
    except json.JSONDecodeError:
        return str(raw)[:200]
    if not isinstance(args, dict):
        return str(args)[:200]
    if "command" in args:
        return str(args["command"])
    if "path" in args:
        return str(args["path"])
    return json.dumps(args, ensure_ascii=False)[:200]


def summarise_command(config: Config, command: str) -> str | None:
    """Best-effort one-line explanation of a shell command; None on failure."""
    payload = {
        "model": config.summary_model,
        "messages": [
            {"role": "system", "content": SUMMARY_PROMPT},
            {"role": "user", "content": f"Command (cwd: {config.workspace}):\n{command}"},
        ],
        "temperature": 0,
        "stream": False,
    }
    request = urllib.request.Request(
        f"{config.summary_base_url}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {config.summary_api_key}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=SUMMARY_TIMEOUT) as response:
            data = json.loads(response.read().decode("utf-8", errors="replace"))
        content = data["choices"][0]["message"]["content"]
    except (OSError, ValueError, KeyError, IndexError, TypeError):
        return None
    if not isinstance(content, str):
        return None
    summary = " ".join(content.split())
    return summary or None


def command_from_call(call: dict[str, Any]) -> str:
    raw = call["function"].get("arguments") or ""
    try:
        args = json.loads(raw) if isinstance(raw, str) else raw
    except json.JSONDecodeError:
        return ""
    if not isinstance(args, dict):
        return ""
    return str(args.get("command", ""))


def describe_command(app: App, command: str) -> str | None:
    """Cached, best-effort command summary; disables summarisation for the session on failure."""
    if command in app.summaries:
        summary = app.summaries[command]
        app.log.log("command_summary", command=command, summary=summary, source="cache")
        return summary
    if app.summary_disabled:
        app.log.log("command_summary", command=command, summary=None, source="fallback")
        return None
    summary = summarise_command(app.config, command)
    if summary:
        app.summaries[command] = summary
        source = "model"
    else:
        app.summary_disabled = True
        source = "fallback"
    app.log.log("command_summary", command=command, summary=summary, source=source)
    return summary


def execute_tool(app: App, call: dict[str, Any]) -> str:
    name = call["function"]["name"]
    call_id = call.get("id")
    raw_args = call["function"].get("arguments") or "{}"
    try:
        args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
    except json.JSONDecodeError as exc:
        result = f"ERROR: invalid JSON arguments for {name}: {exc}"
        app.log.log("tool_result", name=name, call_id=call_id, ok=False, content=result)
        return result
    if not isinstance(args, dict):
        result = f"ERROR: arguments for {name} must be a JSON object"
        app.log.log("tool_result", name=name, call_id=call_id, ok=False, content=result)
        return result

    tool = app.tools.get(name)
    if tool is None:
        result = f"ERROR: unknown tool: {name}"
        app.log.log("tool_result", name=name, call_id=call_id, ok=False, content=result)
        return result
    if tool.writes and app.mode is Mode.PLAN:
        result = (
            f"ERROR: {name} is unavailable in plan mode; "
            "ask the user to switch to build mode (Tab) and try again"
        )
        app.log.log("tool_result", name=name, call_id=call_id, ok=False, content=result)
        return result
    if tool.requires_approval:
        command = str(args.get("command", ""))
        if not command.strip():
            result = "ERROR: command must not be empty"
            app.log.log("tool_result", name=name, call_id=call_id, ok=False, content=result)
            return result
        if command not in app.approvals.allowed:
            allowed = app.approvals.ask(command)
            app.log.log("approval", command=command, decision="allow" if allowed else "deny")
            if not allowed:
                result = "User denied execution of this command."
                app.log.log("tool_result", name=name, call_id=call_id, ok=False, content=result)
                return result

    app.log.log("tool_call", name=name, arguments=args, call_id=call_id)
    try:
        result = tool.handler(**args)
    except KeyboardInterrupt:
        result = "ERROR: command interrupted by user"
    except ToolError as exc:
        result = f"ERROR: {exc}"
    except TypeError as exc:
        result = f"ERROR: invalid arguments for {name}: {exc}"
    except Exception as exc:  # noqa: BLE001
        result = f"ERROR: {name} failed: {exc.__class__.__name__}: {exc}"
    ok = not result.startswith("ERROR:")
    app.log.log("tool_result", name=name, call_id=call_id, ok=ok, content=cap(result))
    return cap(result)


def run_turn(app: App, user_text: str) -> None:
    app.messages.append({"role": "user", "content": user_text})
    app.log.log("user_message", content=user_text)
    for _ in range(MAX_STEPS):
        say(f"\n{APP_NAME} [{app.mode.value}]> ", "class:agent")
        spinner = Spinner()

        def emit(fragments: list[tuple[str, str]], spinner: Spinner = spinner) -> None:
            spinner.stop()
            pt_print(FormattedText(fragments), end="", flush=True, style=STYLE)

        renderer = MarkdownStream(emit)

        def on_delta(text: str, renderer: MarkdownStream = renderer) -> None:
            renderer.feed(text)

        spinner.start()
        try:
            result = stream_chat(app, on_delta)
        except AgentError as exc:
            spinner.stop()
            say(f"\nERROR: {exc}", "class:error")
            app.log.log("error", message=str(exc))
            return
        assistant = result.message
        app.messages.append(assistant)
        app.record_usage(result.usage)
        app.log.log(
            "assistant_message",
            content=assistant.get("content", ""),
            tool_calls=assistant.get("tool_calls", []),
        )
        if result.interrupted or not assistant.get("tool_calls"):
            spinner.stop()
            renderer.finalize()
            if not renderer.wrote:
                stream_write("\n")
            return

        streamed = renderer.wrote
        if streamed:
            renderer.finalize()
        spinner.start()
        try:
            prepared: list[tuple[dict[str, Any], str, str | None]] = []
            for call in assistant["tool_calls"]:
                name = call["function"]["name"]
                if name == "run_command":
                    command = command_from_call(call)
                    summary = describe_command(app, command) if command else None
                    prepared.append((call, f"  -> run_command {command}", summary))
                else:
                    prepared.append((call, f"  -> {name} {summarise_args(call)}", None))
        finally:
            spinner.stop()
        if not streamed:
            renderer.finalize()
            stream_write("\n")
        for call, label, summary in prepared:
            say(label, "class:tool")
            if summary:
                say(f"     {summary}", "class:tool.result")
            content = execute_tool(app, call)
            app.messages.append(
                {"role": "tool", "tool_call_id": call["id"], "content": content}
            )
            preview = content
            if len(preview) > DISPLAY_PREVIEW_LIMIT:
                preview = (
                    preview[:DISPLAY_PREVIEW_LIMIT]
                    + f"\n... [{len(content) - DISPLAY_PREVIEW_LIMIT} more chars]"
                )
            preview_lines = preview.splitlines()
            for line in preview_lines[:DISPLAY_PREVIEW_LINES]:
                say(f"     {line}", "class:tool.result")
            if len(preview_lines) > DISPLAY_PREVIEW_LINES:
                say("     ...", "class:tool.result")
    say(f"step limit ({MAX_STEPS}) reached; stopping this turn", "class:warn")
    app.log.log("error", message="step limit reached")


def handle_command(app: App, text: str) -> bool:
    command = text.split(maxsplit=1)[0].lower()
    if command in ("/exit", "/quit"):
        return True
    if command == "/help":
        say("commands:", "class:info")
        for line in (
            "/help          show this help",
            "/clear         clear the conversation (keeps the system prompt)",
            "/mode          toggle PLAN/BUILD (same as Tab)",
            "/tools         list tools available in the current mode",
            "/log           show the audit log path",
            "/exit          quit",
        ):
            say(f"  {line}")
    elif command == "/clear":
        app.messages = [
            {"role": "system", "content": build_system_prompt(app.config, app.mode)}
        ]
        app.exact_context = 0
        app.exact_context_at = -1
        say("conversation cleared", "class:info")
    elif command == "/mode":
        app.toggle_mode()
    elif command == "/tools":
        names = [tool.name for tool in app.tools.values() if app.tool_allowed(tool)]
        say(f"tools available in {app.mode.value} mode: {', '.join(names)}", "class:info")
    elif command == "/log":
        say(
            f"audit log: {app.log.path}" if app.log.path else "audit logging disabled",
            "class:info",
        )
    else:
        say(f"unknown command: {command} (try /help)", "class:error")
    return False


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------


class App:
    def __init__(self, config: Config, log: AuditLog, session: PromptSession):
        self.config = config
        self.log = log
        self.session = session
        self.mode = Mode.PLAN
        self.messages: list[dict[str, Any]] = [
            {"role": "system", "content": build_system_prompt(config, self.mode)}
        ]
        self.tools = build_tools(config)
        self.approvals = Approvals(session, config)
        self.stream_usage = True
        self.exact_context = 0
        self.exact_context_at = -1
        self.summaries: dict[str, str] = {}
        self.summary_disabled = False

    def tool_allowed(self, tool: Tool) -> bool:
        return not (tool.writes and self.mode is Mode.PLAN)

    def allowed_tool_schemas(self) -> list[dict[str, Any]]:
        return [tool_schema(tool) for tool in self.tools.values() if self.tool_allowed(tool)]

    def record_usage(self, usage: dict[str, Any] | None) -> None:
        if not isinstance(usage, dict):
            return
        prompt = usage.get("prompt_tokens")
        completion = usage.get("completion_tokens")
        if isinstance(prompt, int) and isinstance(completion, int):
            self.exact_context = prompt + completion
            self.exact_context_at = len(self.messages)

    def toggle_mode(self) -> None:
        self.mode = Mode.BUILD if self.mode is Mode.PLAN else Mode.PLAN
        self.messages[0] = {
            "role": "system",
            "content": build_system_prompt(self.config, self.mode),
        }
        self.exact_context = 0
        self.exact_context_at = -1
        self.log.log("mode_change", mode=self.mode.value)
        say(f"mode: {self.mode.value.upper()}", "class:info")


CHARS_PER_TOKEN = 4
MESSAGE_TOKEN_OVERHEAD = 4


def estimate_tokens(
    messages: list[dict[str, Any]],
    schemas: list[dict[str, Any]] | None = None,
) -> int:
    payload: Any = messages if not schemas else [messages, schemas]
    size = len(json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8"))
    return size // CHARS_PER_TOKEN + MESSAGE_TOKEN_OVERHEAD * len(messages)


def format_tokens(count: int) -> str:
    if count < 1000:
        return str(count)
    return f"{count / 1000:.1f}k"


def context_tokens(app: App) -> tuple[int, bool]:
    """Return (tokens, exact) for the context of the next request."""
    if app.exact_context and 0 <= app.exact_context_at <= len(app.messages):
        appended = app.messages[app.exact_context_at :]
        if not appended:
            return app.exact_context, True
        return app.exact_context + estimate_tokens(appended), False
    return estimate_tokens(app.messages, app.allowed_tool_schemas()), False


def build_toolbar(app: App) -> FormattedText:
    tokens, exact = context_tokens(app)
    label = f"ctx {'~' if not exact else ''}{format_tokens(tokens)}"
    style = "class:ctx"
    if app.config.context_window:
        label += f"/{format_tokens(app.config.context_window)}"
        ratio = tokens / app.config.context_window
        if ratio >= 0.95:
            style = "class:ctx.hot"
        elif ratio >= 0.8:
            style = "class:ctx.warn"
    return FormattedText(
        [
            (f"class:mode.{app.mode.value}", f" {app.mode.value.upper()} "),
            ("class:toolbar", f" {app.config.model}  {app.config.workspace} "),
            (style, f" {label} "),
            ("class:toolbar", " Tab:mode  /help "),
        ]
    )


def build_key_bindings(app: App) -> KeyBindings:
    bindings = KeyBindings()

    @bindings.add("tab")
    def _toggle_mode(event: Any) -> None:
        app.toggle_mode()

    return bindings


BANNER = (
    " _____           _",
    "|  __ \\         / \\",
    "| |__) |       / _ \\",
    "|  __ <       / ___ \\",
    "| |__) |     / /   \\ \\",
    "|_____/ asic \\/     \\/ gent",
)


def print_banner(app: App) -> None:
    for line in BANNER:
        say(line, "class:info")
    say(f"Version: {VERSION}", "class:info")
    say("")
    model = app.config.model
    if app.config.summary_model_defined:
        model += f" ({app.config.summary_model})"
    say(f"  model:     {model}")
    say(f"  endpoint:  {app.config.base_url}")
    say(f"  workspace: {app.config.workspace}")
    say("  /help for commands")


def print_startup_instructions() -> None:
    try:
        text = INSTRUCTIONS_FILE.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return
    except OSError as exc:
        say(f"warning: cannot read {INSTRUCTIONS_FILE.name}: {exc}", "class:warn")
        return
    say("")
    renderer = MarkdownStream(
        lambda fragments: pt_print(FormattedText(fragments), end="", flush=True, style=STYLE)
    )
    renderer.feed(text)
    renderer.finalize()
    if not renderer.wrote:
        stream_write("\n")


def main(argv: list[str] | None = None) -> int:
    config = parse_args(argv)
    clear()
    load_project_instructions(config)
    if config.context_window <= 0:
        config.context_window, config.context_window_source = probe_context_window(config)
    log = AuditLog()
    session: PromptSession = PromptSession(style=STYLE)
    app = App(config, log, session)
    bindings = build_key_bindings(app)
    log.log(
        "session_start",
        version=VERSION,
        base_url=config.base_url,
        model=config.model,
        workspace=str(config.workspace),
        mode=app.mode.value,
        api_key="[redacted]" if config.api_key not in ("", "none") else "[none]",
        instructions=str(config.project_instructions_path)
        if config.project_instructions_path
        else None,
        instructions_bytes=len(config.project_instructions)
        if config.project_instructions
        else 0,
        context_window=config.context_window,
        context_window_source=config.context_window_source,
    )
    print_banner(app)
    print_startup_instructions()
    try:
        while True:
            try:
                text = session.prompt(
                    FormattedText([("class:user", "you> ")]),
                    key_bindings=bindings,
                    bottom_toolbar=lambda: build_toolbar(app),
                )
            except KeyboardInterrupt:
                continue
            except EOFError:
                break
            text = text.strip()
            if not text:
                continue
            if text.startswith("/"):
                if handle_command(app, text):
                    break
                continue
            try:
                run_turn(app, text)
            except KeyboardInterrupt:
                say("interrupted", "class:warn")
    finally:
        log.log("session_end")
        log.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
