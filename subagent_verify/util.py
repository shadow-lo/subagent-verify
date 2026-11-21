"""Small, dependency-free helpers: text sanitising, secret redaction, safe IO."""
from __future__ import annotations

import json
import os
import re
import stat
import tempfile
from typing import Any

MAX_STDIN_BYTES = 8 * 1024 * 1024        # hook input cap
MAX_JSON_FILE_BYTES = 1 * 1024 * 1024    # config / brief / delegated-agents cap

_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f\u00ad\u061c\u180e\u200b-\u200f\u2028-\u202e\u2060-\u2069\ufeff\ufff9-\ufffb]")

# Patterns for things that look like credentials. Matches are replaced, never echoed.
_R = "[REDACTED]"
_NB = r"(?<![A-Za-z0-9])"          # start boundary that also works after "_"
_KEYWORD = r"[A-Za-z0-9_.\-]*(?:password|passwd|pwd|secret|token|api[_-]?key|apikey|access[_-]?key|private[_-]?key|client[_-]?secret|credential|auth)[A-Za-z0-9_.\-]*"
_SECRET_PATTERNS = [
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)", re.S), _R),
    (re.compile(_NB + r"(?:AKIA|ASIA)[0-9A-Z]{16}"), _R),
    (re.compile(_NB + r"gh[pousr]_[A-Za-z0-9_]{20,}"), _R),
    (re.compile(_NB + r"github_pat_[A-Za-z0-9_]{20,}"), _R),
    (re.compile(_NB + r"glpat-[A-Za-z0-9_\-]{20,}"), _R),
    (re.compile(_NB + r"(?:sk|rk|pk)_(?:live|test)_[A-Za-z0-9]{10,}"), _R),
    (re.compile(_NB + r"sk-(?:ant-|proj-|live-|test-)?[A-Za-z0-9_\-]{16,}"), _R),
    (re.compile(_NB + r"xox[abprs]-[A-Za-z0-9\-]{10,}"), _R),
    (re.compile(_NB + r"AIza[0-9A-Za-z_\-]{30,}"), _R),
    (re.compile(_NB + r"npm_[A-Za-z0-9]{20,}"), _R),
    (re.compile(_NB + r"hf_[A-Za-z0-9]{20,}"), _R),
    (re.compile(_NB + r"(?:dop|doo|dor)_v1_[a-f0-9]{32,}"), _R),
    (re.compile(_NB + r"eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"), _R),
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._\-~+/]{12,}=*"), r"\1 " + _R),
    (re.compile(r"(?i)\b([a-z][a-z0-9+.\-]*://)[^\s:/@]+:[^\s@/]+@"), r"\1" + _R + "@"),
    (re.compile(r"(?i)(" + _KEYWORD + r")(\s*[:=]\s*|\s+is\s+)(?:\"[^\"]{3,}\"|'[^']{3,}'|[^\s,;\"']{3,})"), r"\1\2" + _R),
    # long high-entropy blobs (hex or base64) are almost never needed in a report
    (re.compile(_NB + r"[A-Fa-f0-9]{40,}(?![A-Za-z0-9])"), _R),
    (re.compile(_NB + r"(?=[A-Za-z0-9+/_\-]*[0-9])(?=[A-Za-z0-9+/_\-]*[A-Z])(?=[A-Za-z0-9+/_\-]*[a-z])[A-Za-z0-9+/_\-]{40,}={0,2}"), _R),
]


def redact(text: str) -> str:
    """Replace anything that looks like a secret with [REDACTED]."""
    for pat, repl in _SECRET_PATTERNS:
        text = pat.sub(repl, text)
    return text


def clean(text: Any, maxlen: int = 200, *, oneline: bool = True) -> str:
    """Make untrusted text safe to echo: strip control/bidi chars, redact, truncate."""
    if text is None:
        return ""
    if not isinstance(text, str):
        text = str(text)
    text = text[: maxlen * 4]
    if oneline:
        text = re.sub(r"[\r\n\t\v\f\x1c-\x1e\x85\u2028\u2029]", " ", text)
    text = _CONTROL.sub("", text)
    text = redact(text)
    text = re.sub(r"[ ]{2,}", " ", text).strip()
    if len(text) > maxlen:
        text = text[: maxlen - 1].rstrip() + "…"
    return text


def read_json_file(path: str, max_bytes: int = MAX_JSON_FILE_BYTES) -> Any:
    """Read a small JSON file without following it into special files. Returns None on any problem."""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size > max_bytes:
            return None
        with os.fdopen(fd, "rb") as fh:
            fd = -1
            data = fh.read(max_bytes + 1)
        if len(data) > max_bytes:
            return None
        return json.loads(data.decode("utf-8", "replace"))
    except (OSError, ValueError, RecursionError):
        return None
    finally:
        if fd >= 0:
            os.close(fd)


def ensure_private_dir(path: str) -> bool:
    """Create path (mode 0700) if needed and confirm it is a real directory we own."""
    try:
        os.makedirs(path, mode=0o700, exist_ok=True)
        st = os.lstat(path)
        if not stat.S_ISDIR(st.st_mode) or stat.S_ISLNK(st.st_mode):
            return False
        if hasattr(os, "getuid") and st.st_uid != os.getuid():
            return False
        if st.st_mode & 0o077:
            os.chmod(path, 0o700)
        return True
    except OSError:
        return False


def atomic_write_json(path: str, obj: Any) -> bool:
    """Write JSON to path atomically with mode 0600."""
    directory = os.path.dirname(path)
    try:
        fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=directory)
    except OSError:
        return False
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, separators=(",", ":"))
        os.replace(tmp, path)
        return True
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return False
