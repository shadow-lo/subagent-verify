"""Working-tree snapshots taken before a subagent runs, and the diff after it finishes.

A snapshot stores only repo-relative paths and content fingerprints (sha256 or size+mtime).
It never stores file contents."""
from __future__ import annotations

import hashlib
import os
import stat
import time
from typing import Dict, Optional, Set, Tuple

from . import gitutil
from .paths import safe_rel

SKIP_DIRS = {
    ".git", "node_modules", ".venv", "venv", "__pycache__", ".tox", ".mypy_cache",
    ".pytest_cache", ".ruff_cache", "dist", "build", ".next", "target", ".cache", ".gradle",
}
MAX_HASH_BYTES = 32 * 1024 * 1024
MAX_HASHED_FILES = 5000
MAX_WALK_FILES = 20000
CLEAN = "="       # tracked and identical to HEAD (git mode)
ABSENT = "-"      # does not exist


def file_sig(abs_path: str, allow_hash: bool = True) -> str:
    """Fingerprint a path without following symlinks."""
    try:
        st = os.lstat(abs_path)
    except FileNotFoundError:
        return ABSENT
    except OSError:
        return "E"
    if stat.S_ISLNK(st.st_mode):
        try:
            return "L:" + hashlib.sha256(os.readlink(abs_path).encode("utf-8", "surrogateescape")).hexdigest()
        except OSError:
            return "L:?"
    if not stat.S_ISREG(st.st_mode):
        return f"O:{stat.S_IFMT(st.st_mode)}"
    if not allow_hash or st.st_size > MAX_HASH_BYTES:
        return f"S:{st.st_size}:{st.st_mtime_ns}"
    h = hashlib.sha256()
    try:
        fd = os.open(abs_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
        with os.fdopen(fd, "rb") as fh:
            if not stat.S_ISREG(os.fstat(fh.fileno()).st_mode):
                return "O:?"
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
    except OSError:
        return f"S:{st.st_size}:{st.st_mtime_ns}"
    return "H:" + h.hexdigest()


def take(root: str) -> dict:
    """Snapshot the project. Git repos: fingerprint every dirty/untracked path plus HEAD.
    Other directories: fingerprint every file (bounded walk)."""
    snap = {"v": 1, "ts": time.time(), "git": False, "head": None, "files": {}, "complete": True}
    st = gitutil.status(root)
    if st is not None:
        snap["git"] = True
        snap["head"] = gitutil.head(root)
        files: Dict[str, str] = {}
        for i, rel in enumerate(sorted(st)):
            files[rel] = file_sig(os.path.join(root, rel), allow_hash=i < MAX_HASHED_FILES)
        snap["files"] = files
        return snap
    files = {}
    count = 0
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            count += 1
            if count > MAX_WALK_FILES:
                snap["complete"] = False
                snap["files"] = files
                return snap
            abs_p = os.path.join(dirpath, name)
            rel = os.path.relpath(abs_p, root).replace(os.sep, "/")
            if safe_rel(rel):
                files[rel] = file_sig(abs_p, allow_hash=count <= MAX_HASHED_FILES)
    snap["files"] = files
    return snap


def diff(before: dict, root: str) -> Tuple[Optional[Set[str]], dict]:
    """Return (paths changed since `before` or None if not comparable, the new snapshot)."""
    after = take(root)
    bf, af = before.get("files") or {}, after.get("files") or {}
    default = CLEAN if after["git"] else ABSENT
    changed = {p for p in set(bf) | set(af) if bf.get(p, default) != af.get(p, default)}
    if before.get("git") and after["git"] and before.get("head") and after.get("head") \
            and before["head"] != after["head"]:
        committed = gitutil.changed_between(root, before["head"], after["head"])
        if committed:
            changed |= committed
    if before.get("git") != after["git"]:
        return None, after  # repo appeared/disappeared: cannot compare reliably
    return {p for p in changed if safe_rel(p)}, after


def mtime_since(abs_path: str, since: float) -> Optional[bool]:
    try:
        return os.lstat(abs_path).st_mtime >= since - 1.0
    except OSError:
        return None
