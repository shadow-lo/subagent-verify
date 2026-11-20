"""Private on-disk store for pre-run snapshots (outside the project, mode 0700/0600).
Keys are hashed so untrusted ids can never become path components."""
from __future__ import annotations

import hashlib
import os
import time
from typing import Optional

from .util import atomic_write_json, ensure_private_dir, read_json_file

MAX_AGE = 24 * 3600
MAX_STATE_BYTES = 64 * 1024 * 1024


def state_dir(env: Optional[dict] = None) -> str:
    env = os.environ if env is None else env
    if env.get("SUBAGENT_VERIFY_STATE_DIR"):
        return env["SUBAGENT_VERIFY_STATE_DIR"]
    base = env.get("XDG_STATE_HOME") or os.path.join(env.get("HOME") or os.path.expanduser("~"), ".local", "state")
    return os.path.join(base, "subagent-verify")


def _key(session_id: str, tool_use_id: str) -> str:
    raw = f"{session_id}\0{tool_use_id}".encode("utf-8", "surrogateescape")
    return hashlib.sha256(raw).hexdigest() + ".json"


def _prune(directory: str) -> None:
    now = time.time()
    try:
        with os.scandir(directory) as it:
            for e in it:
                if e.name.endswith(".json") and e.is_file(follow_symlinks=False):
                    try:
                        if now - e.stat(follow_symlinks=False).st_mtime > MAX_AGE:
                            os.unlink(e.path)
                    except OSError:
                        pass
    except OSError:
        pass


def save(session_id: str, tool_use_id: str, root: str, snap: dict, env: Optional[dict] = None) -> bool:
    if not (isinstance(session_id, str) and isinstance(tool_use_id, str) and tool_use_id):
        return False
    d = state_dir(env)
    if not ensure_private_dir(d):
        return False
    _prune(d)
    return atomic_write_json(os.path.join(d, _key(session_id, tool_use_id)), {"root": root, "snap": snap})


def pop(session_id: str, tool_use_id: str, root: str, env: Optional[dict] = None) -> Optional[dict]:
    if not (isinstance(session_id, str) and isinstance(tool_use_id, str) and tool_use_id):
        return None
    d = state_dir(env)
    if not ensure_private_dir(d):
        return None
    path = os.path.join(d, _key(session_id, tool_use_id))
    data = read_json_file(path, MAX_STATE_BYTES)
    try:
        os.unlink(path)
    except OSError:
        pass
    if not isinstance(data, dict) or data.get("root") != root or not isinstance(data.get("snap"), dict):
        return None
    snap = data["snap"]
    if not isinstance(snap.get("files"), dict) or time.time() - float(snap.get("ts") or 0) > MAX_AGE:
        return None
    return snap
