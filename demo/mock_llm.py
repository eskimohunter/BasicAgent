#!/usr/bin/env python3
"""Scripted OpenAI-compatible server used to record the demo tapes.

agent.py only needs a handful of endpoints to run a full turn, so this server
serves exactly those: the context-window probe (``/props``, ``/v1/models``) and
``/v1/chat/completions``. Replies are canned per scenario so the VHS recordings
are reproducible without a live model.

Usage:
    python demo/mock_llm.py --scenario plan --port 8899
"""

from __future__ import annotations

import argparse
import json
import re
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

MODEL_NAME = "demo-model"
CONTEXT_WINDOW = 32768
# The listing the mock advertises; demo-model stays first so the startup
# context-window probe picks it up.
MODELS = [
    {"id": MODEL_NAME, "object": "model", "n_ctx": CONTEXT_WINDOW},
    {"id": "demo-coder", "object": "model", "n_ctx": CONTEXT_WINDOW},
    {"id": "demo-embed", "object": "model"},
]
# Seconds between streamed fragments: long enough for the recorder to show the
# reply building up, short enough to keep the GIFs moving.
CHUNK_DELAY = 0.03
# The argument JSON is streamed in fragments like real servers do.
ARGUMENT_FRAGMENTS = 4

PLAN_ANSWER = """## Project overview

This is a small Python project with two files:

- `greeter.py` — one `greet(name)` function that returns a greeting
- `test_greeter.py` — a `unittest` suite covering it

The function is deliberately slightly wrong: `greet()` builds
`Hello world!` without a comma, while the tests expect `Hello, Ada!`.
Run `python -m unittest -q` to see the failure.
"""

BUILD_ANSWER = """Fixed. `greet()` adds the missing comma and the suite passes:

- `greeter.py` — now returns `f"Hello, {name}!"`
- `python -m unittest -q` — 2 tests, OK
"""

BOUNDARY_ANSWER = """I can't read `/etc/hostname`: file tools are confined to the workspace
(`/tmp/demo-project`). The path resolves outside it, so the agent refuses
before anything is opened.

Ask me for a file inside the workspace instead; reading anything else would
need a shell command, which you would approve first.
"""

SCENARIOS: dict[str, dict[str, Any]] = {
    "plan": {
        "tool_calls": [{"name": "read_file", "arguments": {"path": "greeter.py"}}],
        "answer": PLAN_ANSWER,
    },
    "build": {
        "tool_calls": [
            {
                "name": "edit_file",
                "arguments": {
                    "path": "greeter.py",
                    "old_string": 'return f"Hello {name}!"',
                    "new_string": 'return f"Hello, {name}!"',
                },
            },
            {
                "name": "run_command",
                "arguments": {"command": "python -m unittest -q"},
            },
        ],
        "answer": BUILD_ANSWER,
    },
    "boundary": {
        "tool_calls": [{"name": "read_file", "arguments": {"path": "/etc/hostname"}}],
        "answer": BOUNDARY_ANSWER,
    },
}

TEST_SUMMARY = (
    "Runs the unit tests with Python's unittest module; it does not change "
    "any files."
)
GENERIC_SUMMARY = (
    "Runs the given command in the project directory; inspect it before "
    "approving, since it can change files."
)


def is_summary_request(messages: list[dict[str, Any]]) -> bool:
    """True when the request is the command-summary call (see agent.py)."""
    if not messages or messages[0].get("role") != "system":
        return False
    content = messages[0].get("content") or ""
    return isinstance(content, str) and "junior developer" in content


def summarise(messages: list[dict[str, Any]]) -> str:
    """Canned one-liner for the command awaiting approval."""
    blob = " ".join(str(message.get("content") or "") for message in messages)
    return TEST_SUMMARY if "unittest" in blob else GENERIC_SUMMARY


def split_fragments(text: str, count: int) -> list[str]:
    """Split *text* into roughly *count* contiguous fragments."""
    size = max(1, -(-len(text) // count))
    return [text[index : index + size] for index in range(0, len(text), size)]


class MockHandler(BaseHTTPRequestHandler):
    """Serves the probe endpoints and scripted chat completions."""

    server_version = "ba-demo-mock"

    def log_message(self, format: str, *args: Any) -> None:
        """Keep the console quiet; failures are visible in the log file."""

    def do_GET(self) -> None:
        path = self.path.rstrip("/")
        if path == "/props":
            self.send_json(
                {"default_generation_settings": {"n_ctx": CONTEXT_WINDOW}}
            )
        elif path in ("/v1/models", "/models"):
            self.send_json({"object": "list", "data": MODELS})
        else:
            self.send_error(404)

    def do_POST(self) -> None:
        if self.path.rstrip("/") != "/v1/chat/completions":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        messages = body.get("messages") or []
        if is_summary_request(messages):
            content = summarise(messages)
            stream = False
        else:
            scenario = SCENARIOS[self.server.scenario]
            has_tool_result = any(m.get("role") == "tool" for m in messages)
            content = scenario["answer"] if has_tool_result else ""
            tool_calls = [] if has_tool_result else scenario["tool_calls"]
            stream = bool(body.get("stream"))
        if not stream:
            self.send_json(
                {
                    "id": "chatcmpl-demo",
                    "object": "chat.completion",
                    "model": MODEL_NAME,
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": content},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 128,
                        "completion_tokens": 32,
                    },
                }
            )
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.stream_response(content, tool_calls)

    def stream_response(
        self, content: str, tool_calls: list[dict[str, Any]]
    ) -> None:
        """Write an SSE reply: content fragments, tool calls, usage, [DONE]."""
        self.sse(
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {"role": "assistant", "content": ""},
                        "finish_reason": None,
                    }
                ]
            }
        )
        for fragment in re.findall(r"\S+\s*", content):
            self.sse(
                {
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"content": fragment},
                            "finish_reason": None,
                        }
                    ]
                }
            )
            time.sleep(CHUNK_DELAY)
        for index, call in enumerate(tool_calls):
            arguments = json.dumps(call["arguments"], ensure_ascii=False)
            self.sse(
                {
                    "choices": [
                        {
                            "index": 0,
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": index,
                                        "id": f"call_{index}",
                                        "type": "function",
                                        "function": {
                                            "name": call["name"],
                                            "arguments": "",
                                        },
                                    }
                                ]
                            },
                            "finish_reason": None,
                        }
                    ]
                }
            )
            for fragment in split_fragments(arguments, ARGUMENT_FRAGMENTS):
                time.sleep(CHUNK_DELAY)
                self.sse(
                    {
                        "choices": [
                            {
                                "index": 0,
                                "delta": {
                                    "tool_calls": [
                                        {
                                            "index": index,
                                            "function": {"arguments": fragment},
                                        }
                                    ]
                                },
                                "finish_reason": None,
                            }
                        ]
                    }
                )
        finish_reason = "tool_calls" if tool_calls else "stop"
        self.sse(
            {"choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason}]}
        )
        self.sse(
            {
                "choices": [],
                "usage": {"prompt_tokens": 1284, "completion_tokens": 156},
            }
        )
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    def sse(self, chunk: dict[str, Any]) -> None:
        payload = json.dumps(chunk, ensure_ascii=False)
        self.wfile.write(f"data: {payload}\n\n".encode())
        self.wfile.flush()

    def send_json(self, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class MockServer(ThreadingHTTPServer):
    """HTTP server that carries the selected scenario."""

    daemon_threads = True

    def __init__(self, address: tuple[str, int], scenario: str):
        super().__init__(address, MockHandler)
        self.scenario = scenario


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", choices=sorted(SCENARIOS), default="plan")
    parser.add_argument("--port", type=int, default=8899)
    args = parser.parse_args()
    server = MockServer(("127.0.0.1", args.port), args.scenario)
    server.serve_forever()


if __name__ == "__main__":
    main()
