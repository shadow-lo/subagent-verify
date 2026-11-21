"""Read a subagent transcript (JSONL) for *actions*: which files it wrote through tools,
which shell commands it ran and whether they failed, and its final message.
Only structure is extracted; contents are never echoed."""
from __future__ import annotations

import json
import os
import re
import stat
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional

MAX_TRANSCRIPT_BYTES = 32 * 1024 * 1024
MAX_ENTRIES = 50_000
WRITE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}
SHELL_TOOLS = {"Bash", "PowerShell"}

TEST_CMD = re.compile(
    r"(?i)(?:^|[\s;&|(/])(?:pytest|py\.test|unittest|nose2|tox|nox|jest|vitest|mocha|ava|"
    r"rspec|phpunit|ctest|"
    r"(?:npm|pnpm|yarn|bun)\s+(?:run\s+)?test|go\s+test|cargo\s+(?:test|nextest)|"
    r"(?:mvn|mvnw|gradle|gradlew)\s+(?:\S+\s+)*test|make\s+(?:\S+\s+)*(?:test|check)|"
    r"dotnet\s+test|swift\s+test|mix\s+test|deno\s+test|bats|rake\s+test|"
    r"python[\d.]*\s+(?:-\w+\s+)*-m\s+(?:pytest|unittest)|(?:\S*/)?manage\.py\s+test|"
    r"make\s+(?:\S+\s+)*ci|(?:\./|\S+/)?\S*tests?\S*\.sh|"
    r"(?:npm|pnpm|yarn|bun)\s+run\s+\S*test\S*|hatch\s+(?:run\s+)?test|poetry\s+run\s+pytest|uv\s+run\s+pytest)"
    r"(?:$|[\s;&|)])"
)


@dataclass
class ToolCall:
    name: str
    target: Optional[str] = None    # file_path for write tools, command for shells
    ok: Optional[bool] = None       # None = no result seen


@dataclass
class Evidence:
    calls: List[ToolCall] = field(default_factory=list)
    start_ts: Optional[float] = None
    final_text: str = ""
    readable: bool = False
    model: Optional[str] = None

    def written_paths(self) -> Dict[str, bool]:
        """path -> True if at least one successful write tool call targeted it."""
        out: Dict[str, bool] = {}
        for c in self.calls:
            if c.name in WRITE_TOOLS and c.target:
                out[c.target] = out.get(c.target, False) or c.ok is not False
        return out

    def shell_commands(self) -> List[ToolCall]:
        return [c for c in self.calls if c.name in SHELL_TOOLS and c.target]

    def test_runs(self) -> List[ToolCall]:
        return [c for c in self.shell_commands() if TEST_CMD.search(c.target or "")]


def _ts(value) -> Optional[float]:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content
                         if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str))
    return ""


def read(path: str) -> Evidence:
    ev = Evidence()
    if not isinstance(path, str) or not path or "\0" in path:
        return ev
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    except OSError:
        return ev
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            os.close(fd)
            return ev
        fh = os.fdopen(fd, "rb")
    except OSError:
        os.close(fd)
        return ev
    with fh:
        truncated = st.st_size > MAX_TRANSCRIPT_BYTES
        if truncated:
            fh.seek(st.st_size - MAX_TRANSCRIPT_BYTES)
            fh.readline()  # drop partial line
        data = fh.read(MAX_TRANSCRIPT_BYTES)
    ev.readable = True
    by_id: Dict[str, ToolCall] = {}
    last_text = ""
    for n, raw in enumerate(data.splitlines()):
        if n >= MAX_ENTRIES:
            break
        try:
            entry = json.loads(raw)
        except (ValueError, RecursionError):
            continue
        if not isinstance(entry, dict):
            continue
        if ev.start_ts is None and not truncated:
            ev.start_ts = _ts(entry.get("timestamp"))
        msg = entry.get("message")
        if not isinstance(msg, dict):
            continue
        role = msg.get("role") or entry.get("type")
        content = msg.get("content")
        if role == "assistant":
            if isinstance(msg.get("model"), str) and len(msg["model"]) < 100:
                ev.model = msg["model"]
            t = _text_of(content)
            if t.strip():
                last_text = t
            if isinstance(content, list):
                for b in content:
                    if not isinstance(b, dict) or b.get("type") != "tool_use":
                        continue
                    name = b.get("name") if isinstance(b.get("name"), str) else ""
                    inp = b.get("input") if isinstance(b.get("input"), dict) else {}
                    target = None
                    if name in WRITE_TOOLS:
                        target = inp.get("file_path") or inp.get("notebook_path")
                    elif name in SHELL_TOOLS:
                        target = inp.get("command")
                    call = ToolCall(name, target if isinstance(target, str) else None)
                    ev.calls.append(call)
                    if isinstance(b.get("id"), str):
                        by_id[b["id"]] = call
        elif role == "user" and isinstance(content, list):
            for b in content:
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    call = by_id.get(b.get("tool_use_id"))
                    if call is not None:
                        call.ok = not bool(b.get("is_error"))
    ev.final_text = last_text
    return ev
