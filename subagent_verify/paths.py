"""Path safety: every path we touch must resolve inside the project root."""
from __future__ import annotations

import os
import re
from typing import Optional

MAX_PATH_LEN = 1024
_BAD_CHARS = re.compile(r"[\x00-\x1f\x7f]")


def real_root(path: str) -> Optional[str]:
    """Canonical absolute directory, or None."""
    if not isinstance(path, str) or not path or _BAD_CHARS.search(path) or len(path) > 4096:
        return None
    try:
        rp = os.path.realpath(path)
    except (OSError, ValueError):
        return None
    return rp if os.path.isdir(rp) else None


def is_within(child: str, root: str) -> bool:
    try:
        return os.path.commonpath([child, root]) == root
    except ValueError:
        return False


def resolve_in_root(candidate: str, root: str, base: Optional[str] = None) -> Optional[str]:
    """Return the repo-relative POSIX path of candidate if (after symlink resolution) it is
    inside root and not inside .git. Return None otherwise. Never touches file contents."""
    if not isinstance(candidate, str):
        return None
    candidate = candidate.strip()
    if not candidate or len(candidate) > MAX_PATH_LEN or _BAD_CHARS.search(candidate):
        return None
    if candidate.startswith("~"):
        return None
    if os.path.isabs(candidate):
        joined = candidate
    else:
        joined = os.path.join(base or root, candidate)
    try:
        # Resolve the parent fully (it may contain symlinks), but keep the last component
        # so that a claimed-deleted file (which no longer exists) can still be judged.
        parent, name = os.path.split(os.path.normpath(joined))
        if name in ("", ".", ".."):
            return None
        rparent = os.path.realpath(parent)
        full = os.path.join(rparent, name)
        if os.path.islink(full):
            target = os.path.realpath(full)
            if not is_within(target, root):
                return None
    except (OSError, ValueError):
        return None
    if not is_within(full, root) or full == root:
        return None
    rel = os.path.relpath(full, root).replace(os.sep, "/")
    if rel == ".." or rel.startswith("../"):
        return None
    first = rel.split("/", 1)[0]
    if first == ".git":
        return None
    return rel


def safe_rel(rel: str) -> bool:
    """Validate an already repo-relative path (e.g. from git output or a brief)."""
    if not isinstance(rel, str) or not rel or len(rel) > MAX_PATH_LEN or _BAD_CHARS.search(rel):
        return False
    if rel.startswith("/") or rel.startswith("~") or re.match(r"^[A-Za-z]:", rel):
        return False
    parts = rel.split("/")
    return ".." not in parts


def prefix_match(rel: str, pattern: str) -> bool:
    """Brief-style match: trailing '/' means directory prefix, otherwise exact path."""
    if pattern.endswith("/"):
        return rel.startswith(pattern) or rel + "/" == pattern
    return rel == pattern
