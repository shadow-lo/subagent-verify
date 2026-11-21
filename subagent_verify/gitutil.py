"""Hardened, read-only git calls. No shell, fixed argv, sanitised environment.

Only plumbing commands that never run content filters are used (`ls-files`, `ls-tree`,
`rev-parse`, `check-ignore`, tree-to-tree `diff --name-only`). In particular we never call
`git status`, `diff-files` or `hash-object`, because those may run a `filter.<x>.clean` program
that a subagent could plant through `.git/config` + `.gitattributes`. Content is hashed by us."""
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
    "-c", "core.attributesFile=/dev/null",
    "-c", "core.sshCommand=false",
    "-c", "uploadpack.packObjectsHook=",
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


def list_files(root: str) -> Optional[dict]:
    """{rel: "t"} for tracked files and {rel: "u"} for untracked, non-ignored files.
    Uses only `ls-files`, which never runs filters or fsmonitor."""
    tracked = run_git(root, ["ls-files", "-z", "--cached", "--full-name"])
    if tracked is None:
        return None
    others = run_git(root, ["ls-files", "-z", "--others", "--exclude-standard", "--full-name"])
    result = {}
    for blob, tag in ((tracked, "t"), (others or b"", "u")):
        for p in blob.split(b"\0"):
            if not p:
                continue
            rel = p.decode("utf-8", "surrogateescape")
            if safe_rel(rel):
                result.setdefault(rel, tag)
    return result


def head_blobs(root: str, rels) -> dict:
    """{rel: blob id at HEAD} for the given paths (ls-tree reads objects, runs nothing)."""
    rels = [r for r in rels if safe_rel(r)][:500]
    if not rels:
        return {}
    out = run_git(root, ["ls-tree", "-z", "--full-tree", "HEAD", "--", *rels])
    result = {}
    for entry in (out or b"").split(b"\0"):
        meta, _, path = entry.partition(b"\t")
        parts = meta.split()
        if len(parts) == 3 and parts[1] == b"blob":
            result[path.decode("utf-8", "surrogateescape")] = parts[2].decode("ascii", "replace")
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
