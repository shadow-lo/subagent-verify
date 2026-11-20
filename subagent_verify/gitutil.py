"""Hardened, read-only git calls. No shell, fixed argv, sanitised environment, and repo
config that could run programs (fsmonitor, external diff, textconv, filters) disabled."""
from __future__ import annotations

import os
import shutil
import subprocess
from typing import List, Optional

from .paths import safe_rel

GIT_TIMEOUT = 10
MAX_GIT_OUTPUT = 16 * 1024 * 1024

# -c overrides applied to every call. These neutralise repo-local config that can
# execute programs or touch the network during otherwise read-only commands.
_SAFE_CONFIG = [
    "-c", "core.fsmonitor=false",
    "-c", "core.untrackedCache=false",
    "-c", "core.hooksPath=/dev/null",
    "-c", "diff.external=",
    "-c", "core.pager=cat",
    "-c", "protocol.allow=never",
    "-c", "credential.helper=",
    "-c", "core.quotePath=false",
]

_ENV_KEEP = ("PATH", "HOME", "SYSTEMROOT", "TMPDIR", "TEMP", "TMP")


def _env() -> dict:
    env = {k: os.environ[k] for k in _ENV_KEEP if k in os.environ}
    env.update({
        "LC_ALL": "C",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_OPTIONAL_LOCKS": "0",     # never take index.lock from a hook
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_PAGER": "cat",
        "GIT_LITERAL_PATHSPECS": "1",  # paths are never globs
    })
    return env


def git_binary() -> Optional[str]:
    return shutil.which("git")


def run_git(root: str, args: List[str], timeout: int = GIT_TIMEOUT) -> Optional[bytes]:
    """Run `git -C root <args>`; return stdout bytes or None on any failure."""
    git = git_binary()
    if not git:
        return None
    try:
        proc = subprocess.run(
            [git, "-C", root, *_SAFE_CONFIG, *args],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=_env(),
            timeout=timeout,
            check=False,
            shell=False,
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    if proc.returncode != 0 or len(proc.stdout) > MAX_GIT_OUTPUT:
        return None
    return proc.stdout


def toplevel(path: str) -> Optional[str]:
    out = run_git(path, ["rev-parse", "--show-toplevel"])
    if not out:
        return None
    top = out.decode("utf-8", "replace").strip()
    return os.path.realpath(top) if top else None


def head(root: str) -> Optional[str]:
    out = run_git(root, ["rev-parse", "--verify", "-q", "HEAD"])
    if not out:
        return None
    sha = out.decode("ascii", "replace").strip()
    return sha if all(c in "0123456789abcdef" for c in sha) and len(sha) in (40, 64) else None


def status(root: str) -> Optional[dict]:
    """Return {rel_path: two-letter XY status} for every dirty or untracked path."""
    out = run_git(root, ["status", "--porcelain=v1", "-z", "--untracked-files=all", "--no-renames"])
    if out is None:
        return None
    result = {}
    for entry in out.split(b"\0"):
        if len(entry) < 4:
            continue
        xy = entry[:2].decode("ascii", "replace")
        rel = entry[3:].decode("utf-8", "surrogateescape")
        if safe_rel(rel):
            result[rel] = xy
    return result


def changed_between(root: str, old: str, new: str) -> Optional[set]:
    """Paths changed between two commits (committed by the subagent, for example)."""
    if not (old and new):
        return None
    out = run_git(root, ["diff", "--name-only", "-z", "--no-renames", "--no-ext-diff", "--no-textconv", old, new, "--"])
    if out is None:
        return None
    return {p.decode("utf-8", "surrogateescape") for p in out.split(b"\0") if p and safe_rel(p.decode("utf-8", "surrogateescape"))}


def is_ignored(root: str, rel: str) -> bool:
    if not safe_rel(rel):
        return False
    git = git_binary()
    if not git:
        return False
    try:
        proc = subprocess.run(
            [git, "-C", root, *_SAFE_CONFIG, "check-ignore", "-q", "--no-index", "--", rel],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            env=_env(), timeout=GIT_TIMEOUT, check=False, shell=False,
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return False
    return proc.returncode == 0


def is_tracked(root: str, rel: str) -> bool:
    if not safe_rel(rel):
        return False
    out = run_git(root, ["ls-files", "-z", "--error-unmatch", "--", rel])
    return bool(out)
