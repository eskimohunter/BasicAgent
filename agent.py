#!/usr/bin/env python3
"""BasicAgent: a small, auditable, single-file LLM coding agent with a cross-platform TUI.

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
import subprocess
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
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.styles import Style

APP_NAME = "BasicAgent"
VERSION = "0.1.0"
DEFAULT_BASE_URL = "http://localhost:8080/v1"
TOOL_RESULT_LIMIT = 64_000
DISPLAY_PREVIEW_LIMIT = 2_000
DISPLAY_PREVIEW_LINES = 40
READ_MAX_LINES = 2_000
GREP_MAX_FILE_BYTES = 2_000_000
FETCH_MAX_BYTES = 100_000
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
    temperature: float | None = None
    max_tokens: int | None = None
    request_timeout: float = 120.0
    command_timeout: int = 60
    max_steps: int = 25
    workspace: Path = field(default_factory=Path.cwd)
    log_dir: Path = field(default_factory=lambda: Path("logs"))
    log_enabled: bool = True
    allow_outside: bool = False
    system_prompt: str | None = None


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
    parser.add_argument("--temperature", type=float, default=None, help="Sampling temperature")
    parser.add_argument("--max-tokens", type=int, default=None, help="Response token limit")
    parser.add_argument(
        "--timeout", type=float, default=120.0, dest="request_timeout", help="HTTP timeout (s)"
    )
    parser.add_argument(
        "--command-timeout", type=int, default=60, help="Default shell command timeout (s)"
    )
    parser.add_argument(
        "--max-steps", type=int, default=25, help="Maximum tool rounds per user turn"
    )
    parser.add_argument("--workspace", default=".", help="Workspace root directory")
    parser.add_argument("--log-dir", default="logs", help="Directory for JSONL audit logs")
    parser.add_argument("--no-log", action="store_true", help="Disable audit logging")
    parser.add_argument(
        "--allow-outside",
        action="store_true",
        help="Allow file tools to access paths outside the workspace",
    )
    parser.add_argument("--system-prompt", default=None, help="Override the built-in system prompt")
    parser.add_argument("--version", action="version", version=f"{APP_NAME} {VERSION}")
    ns = parser.parse_args(argv)
    return Config(
        base_url=ns.base_url.rstrip("/"),
        api_key=ns.api_key,
        model=ns.model,
        temperature=ns.temperature,
        max_tokens=ns.max_tokens,
        request_timeout=ns.request_timeout,
        command_timeout=ns.command_timeout,
        max_steps=max(1, ns.max_steps),
        workspace=Path(ns.workspace).expanduser().resolve(),
        log_dir=Path(ns.log_dir).expanduser(),
        log_enabled=not ns.no_log,
        allow_outside=ns.allow_outside,
        system_prompt=ns.system_prompt,
    )


class AuditLog:
    """Append-only JSONL log of everything the agent says, calls and executes."""

    def __init__(self, config: Config):
        self.path: Path | None = None
        self._fh: Any = None
        if config.log_enabled:
            config.log_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
            self.path = config.log_dir / f"{stamp}-{os.getpid()}.jsonl"
            self._fh = self.path.open("a", encoding="utf-8")

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
    if not config.allow_outside:
        try:
            path.relative_to(config.workspace)
        except ValueError as exc:
            raise ToolError(
                f"path is outside the workspace ({config.workspace}); "
                "use --allow-outside to permit this"
            ) from exc
    return path


def is_excluded(path: Path, root: Path) -> bool:
    try:
        rel = path.relative_to(root)
    except ValueError:
        return False
    return any(part in DEFAULT_EXCLUDES for part in rel.parts[:-1])


def tool_read_file(
    config: Config, path: str, start_line: int = 1, end_line: int = 0
) -> str:
    target = resolve_path(config, path)
    if not target.exists():
        raise ToolError(f"file not found: {target}")
    if not target.is_file():
        raise ToolError(f"not a file: {target}")
    raw = target.read_bytes()
    if b"\x00" in raw[:8192]:
        raise ToolError(f"refusing to read binary file: {target}")
    text = raw.decode("utf-8", errors="replace")
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
        if len(raw) > GREP_MAX_FILE_BYTES or b"\x00" in raw[:4096]:
            continue
        text = raw.decode("utf-8", errors="replace")
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
        with urllib.request.urlopen(request, timeout=config.request_timeout) as response:
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
    timeout = int(timeout_seconds) if int(timeout_seconds) > 0 else config.command_timeout
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
            "Read a UTF-8 text file and return numbered lines. "
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


def build_system_prompt(config: Config, mode: Mode) -> str:
    if config.system_prompt:
        base = config.system_prompt
    else:
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
    """Interactive shell-command approval with an in-memory exact-match allowlist."""

    def __init__(self, session: PromptSession, config: Config):
        self.session = session
        self.config = config
        self.allowed: set[str] = set()

    def ask(self, command: str) -> str:
        lines = [
            "",
            "+-- run_command " + "-" * 47,
            f"| cwd: {self.config.workspace}",
        ]
        for command_line in command.splitlines() or [""]:
            lines.append(f"| $ {command_line}")
        lines.append("+" + "-" * 62)
        say("\n".join(lines), "class:warn")
        try:
            answer = self.session.prompt(
                FormattedText(
                    [("class:warn", "Run? [y] once  [a] always (session)  [n] deny: ")]
                ),
                key_bindings=KeyBindings(),
            ).strip().lower()
        except (KeyboardInterrupt, EOFError):
            return "deny"
        if answer in ("y", "yes"):
            return "allow"
        if answer in ("a", "always"):
            self.allowed.add(command)
            return "always"
        return "deny"


# ---------------------------------------------------------------------------
# LLM client (stdlib HTTP + SSE)
# ---------------------------------------------------------------------------


@dataclass
class StreamResult:
    message: dict[str, Any]
    interrupted: bool = False


def parse_non_stream_response(body: str) -> dict[str, Any]:
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
    return message


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
    if config.temperature is not None:
        payload["temperature"] = config.temperature
    if config.max_tokens is not None:
        payload["max_tokens"] = config.max_tokens
    request = urllib.request.Request(
        f"{config.base_url}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {config.api_key}",
        },
        method="POST",
    )
    try:
        response = urllib.request.urlopen(request, timeout=config.request_timeout)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:2000]
        raise AgentError(f"HTTP {exc.code} from {config.base_url}: {body}") from exc
    except urllib.error.URLError as exc:
        raise AgentError(f"cannot reach {config.base_url}: {exc.reason}") from exc
    except OSError as exc:
        raise AgentError(f"request failed: {exc}") from exc

    content_parts: list[str] = []
    calls: dict[int, dict[str, str]] = {}
    interrupted = False
    try:
        with response:
            content_type = (response.headers.get("Content-Type") or "").lower()
            if "text/event-stream" not in content_type:
                body = response.read().decode("utf-8", errors="replace")
                message = parse_non_stream_response(body)
                if message.get("content"):
                    on_delta(message["content"])
                return StreamResult(message=message)
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
        )
    tool_calls = finalize_tool_calls(calls)
    message = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return StreamResult(message=message)


# ---------------------------------------------------------------------------
# Agent loop
# ---------------------------------------------------------------------------


def summarize_args(call: dict[str, Any]) -> str:
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
            decision = app.approvals.ask(command)
            app.log.log("approval", command=command, decision=decision)
            if decision == "deny":
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
    for _ in range(app.config.max_steps):
        say(f"\n{APP_NAME} [{app.mode.value}]> ", "class:agent")
        try:
            result = stream_chat(app, stream_write)
        except AgentError as exc:
            say(f"\nERROR: {exc}", "class:error")
            app.log.log("error", message=str(exc))
            return
        stream_write("\n")
        assistant = result.message
        app.messages.append(assistant)
        app.log.log(
            "assistant_message",
            content=assistant.get("content", ""),
            tool_calls=assistant.get("tool_calls", []),
        )
        if result.interrupted or not assistant.get("tool_calls"):
            return
        for call in assistant["tool_calls"]:
            say(f"  -> {call['function']['name']} {summarize_args(call)}", "class:tool")
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
    say(f"step limit ({app.config.max_steps}) reached; stopping this turn", "class:warn")
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

    def tool_allowed(self, tool: Tool) -> bool:
        return not (tool.writes and self.mode is Mode.PLAN)

    def allowed_tool_schemas(self) -> list[dict[str, Any]]:
        return [tool_schema(tool) for tool in self.tools.values() if self.tool_allowed(tool)]

    def toggle_mode(self) -> None:
        self.mode = Mode.BUILD if self.mode is Mode.PLAN else Mode.PLAN
        self.messages[0] = {
            "role": "system",
            "content": build_system_prompt(self.config, self.mode),
        }
        self.log.log("mode_change", mode=self.mode.value)
        say(f"mode: {self.mode.value.upper()}", "class:info")


def build_toolbar(app: App) -> FormattedText:
    return FormattedText(
        [
            (f"class:mode.{app.mode.value}", f" {app.mode.value.upper()} "),
            (
                "class:toolbar",
                f" {app.config.model}  {app.config.workspace}  Tab:mode  /help ",
            ),
        ]
    )


def build_key_bindings(app: App) -> KeyBindings:
    bindings = KeyBindings()

    @bindings.add("tab")
    def _toggle_mode(event: Any) -> None:
        app.toggle_mode()

    return bindings


def print_banner(app: App) -> None:
    say(f"{APP_NAME} {VERSION}", "class:info")
    say(f"  model:     {app.config.model}")
    say(f"  endpoint:  {app.config.base_url}")
    say(f"  workspace: {app.config.workspace}")
    say(f"  log:       {app.log.path if app.log.path else 'disabled'}")
    say("  mode:      PLAN (read-only) - press Tab to switch to BUILD")
    say("  /help for commands")


def main(argv: list[str] | None = None) -> int:
    config = parse_args(argv)
    log = AuditLog(config)
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
    )
    print_banner(app)
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
    say("bye", "class:info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
