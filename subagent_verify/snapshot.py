"""Working-tree snapshots taken before a subagent runs, and the diff after it finishes.

A snapshot stores only repo-relative paths and content fingerprints (sha256, or size+mtime
once the hashing budget is spent). It never stores file contents. Git is used only to list
files (`ls-files`), never to compare content, so no repo-defined filter program can run."""
from __future__ import annotations

import hashlib
import os
import stat
import time
from typing import Dict, Iterable, List, Optional, Set, Tuple

from . import gitutil
from .paths import safe_rel

SKIP_DIRS = {
    ".git", "node_modules", ".venv", "venv", "__pycache__", ".tox", ".mypy_cache",
    ".pytest_cache", ".ruff_cache", "dist", "build", ".next", "target", ".cache", ".gradle",
}
MAX_HASH_BYTES = 32 * 1024 * 1024          # per file
HASH_BUDGET_BYTES = 512 * 1024 * 1024      # per snapshot; beyond it size+mtime is used
MAX_FILES = 50_000
MAX_EXTRA_FILES = 2_000
ABSENT = "-"


class _Budget:
    def __init__(self, total: int) -> None:
        self.left = total

    def take(self, n: int) -> bool:
        if n <= self.left:
            self.left -= n
            return True
        return False


def file_sig(abs_path: str, budget: Optional[_Budget] = None) -> str:
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
    if stat.S_ISDIR(st.st_mode):
        return "D"
    if not stat.S_ISREG(st.st_mode):
        return f"O:{stat.S_IFMT(st.st_mode)}"
    weak = f"S:{st.st_size}:{st.st_mtime_ns}"
    if st.st_size > MAX_HASH_BYTES or (budget is not None and not budget.take(st.st_size)):
        return weak
    digest = sha256_file(abs_path)
    return "H:" + digest if digest else weak


def sha256_file(abs_path: str, git_blob: Optional[str] = None) -> Optional[str]:
    """sha256 of a regular file (no symlinks, no special files). With git_blob="sha1"/"sha256",
    return the git blob id of the raw bytes instead."""
    try:
        fd = os.open(abs_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    except OSError:
        return None
    try:
        with os.fdopen(fd, "rb") as fh:
            st = os.fstat(fh.fileno())
            if not stat.S_ISREG(st.st_mode) or st.st_size > MAX_HASH_BYTES:
                return None
            h = hashlib.new(git_blob) if git_blob else hashlib.sha256()
            if git_blob:
                h.update(b"blob %d\0" % st.st_size)
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
            return h.hexdigest()
    except (OSError, ValueError):
        return None


def _walk(root: str, start: str, limit: int, skip_dirs=True) -> Iterable[str]:
    count = 0
    for dirpath, dirnames, filenames in os.walk(start, followlinks=False):
        if skip_dirs:
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        else:
            dirnames[:] = [d for d in dirnames if d != ".git"]
        for name in filenames:
            count += 1
            if count > limit:
                return
            rel = os.path.relpath(os.path.join(dirpath, name), root).replace(os.sep, "/")
            if safe_rel(rel):
                yield rel


def extra_paths(root: str, patterns: List[str]) -> List[str]:
    """Existing files under brief must_not_touch patterns, including git-ignored ones (.env)."""
    out: List[str] = []
    for pat in patterns[:300]:
        if not safe_rel(pat.rstrip("/") or "x") or pat.rstrip("/") in ("", ".git"):
            continue
        abs_p = os.path.join(root, pat.rstrip("/"))
        if os.path.islink(abs_p):
            out.append(pat.rstrip("/"))
        elif os.path.isdir(abs_p):
            out.extend(_walk(root, abs_p, MAX_EXTRA_FILES - len(out), skip_dirs=False))
        elif os.path.lexists(abs_p):
            out.append(pat.rstrip("/"))
        if len(out) >= MAX_EXTRA_FILES:
            break
    return out


def take(root: str, extra: Optional[List[str]] = None) -> dict:
    """Snapshot the project. Git repos: every tracked + untracked (non-ignored) file plus HEAD.
    Other directories: every file (bounded walk). `extra` adds specific paths (e.g. ignored
    files protected by a brief)."""
    snap = {"v": 2, "ts": time.time(), "git": False, "head": None, "files": {}, "complete": True}
    budget = _Budget(HASH_BUDGET_BYTES)
    listed = gitutil.list_files(root)
    if listed is not None:
        snap["git"] = True
        snap["head"] = gitutil.head(root)
        rels = sorted(listed)
    else:
        rels = list(_walk(root, root, MAX_FILES + 1))
    if len(rels) > MAX_FILES:
        rels = rels[:MAX_FILES]
        snap["complete"] = False
    files: Dict[str, str] = {}
    for rel in rels:
        files[rel] = file_sig(os.path.join(root, rel), budget)
    for rel in extra or []:
        if rel not in files and safe_rel(rel):
            files[rel] = file_sig(os.path.join(root, rel), budget)
    snap["files"] = files
    snap["extra"] = sorted(set(extra or []) - set(rels))
    return snap


def diff(before: dict, root: str) -> Tuple[Optional[Set[str]], dict]:
    """Return (paths changed since `before` or None if not comparable, the new snapshot)."""
    after = take(root, before.get("extra") or [])
    if before.get("git") != after["git"]:
        return None, after  # repo appeared/disappeared: cannot compare reliably
    bf, af = before.get("files") or {}, after.get("files") or {}
    changed = set()
    for p in set(bf) | set(af):
        b = bf.get(p)
        a = af.get(p)
        if b is None:
            # not listed before (e.g. was ignored, or did not exist): judge by what is there now
            a = a if a is not None else file_sig(os.path.join(root, p))
            if a != ABSENT and _new_since(os.path.join(root, p), before.get("ts")):
                changed.add(p)
            continue
        if a is None:
            a = file_sig(os.path.join(root, p))  # now ignored or untracked-deleted
        if a != b:
            if b.startswith("S:") or a.startswith("S:"):
                # weak fingerprints on one side: fall back to mtime
                if a == ABSENT or b == ABSENT or _new_since(os.path.join(root, p), before.get("ts")):
                    changed.add(p)
            else:
                changed.add(p)
    if before.get("head") and after.get("head") and before["head"] != after["head"]:
        committed = gitutil.changed_between(root, before["head"], after["head"])
        if committed:
            # committed but with identical working-tree bytes before/after is not a change
            # by this run unless the file content moved too; keep only those that differ
            changed |= {p for p in committed if bf.get(p) != af.get(p, file_sig(os.path.join(root, p)))}
    return {p for p in changed if safe_rel(p)}, after


def _new_since(abs_path: str, since: Optional[float]) -> bool:
    if since is None:
        return False
    r = mtime_since(abs_path, since)
    return bool(r)


def mtime_since(abs_path: str, since: float) -> Optional[bool]:
    try:
        return os.lstat(abs_path).st_mtime >= since - 1.0
    except OSError:
        return None


def differs_from_head(root: str, rels: List[str]) -> Set[str]:
    """Paths whose bytes differ from HEAD (or that are new / deleted). Hashes raw bytes
    ourselves, so no filter runs; a file under CRLF/LFS filters may look changed."""
    head = gitutil.head(root)
    if not head:
        return set(r for r in rels if os.path.lexists(os.path.join(root, r)))
    algo = "sha256" if len(head) == 64 else "sha1"
    blobs = gitutil.head_blobs(root, rels)
    out = set()
    for r in rels:
        cur = sha256_file(os.path.join(root, r), git_blob=algo)
        if blobs.get(r) != cur:
            if r in blobs or cur is not None:
                out.add(r)
    return out
