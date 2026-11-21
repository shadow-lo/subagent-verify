"""Extract what a subagent *says* it did from its final message.

Two sources:
1. A structured "## WORKER REPORT" block (quota-pilot workers). Authoritative.
2. First-person / bullet-style prose claims ("I updated `src/a.py`", "- Created tests/x.py",
   "src/a.py dosyasını güncelledim") and "Files changed:" style lists. Conservative on purpose:
   descriptive sentences ("the config is created in config.py") are not claims.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

MAX_TEXT = 200_000
MAX_LINES = 3000
MAX_CLAIMS = 200

CREATE, MODIFY, DELETE = "create", "modify", "delete"


@dataclass
class Claim:
    token: str          # path as written by the subagent (untrusted)
    kind: str           # create | modify | delete
    source: str         # report | text
    strong: bool = True  # backticked, contains '/', or from the report


@dataclass
class Report:
    present: bool = False
    status: Optional[str] = None
    files_changed: List[str] = field(default_factory=list)
    commands_run: List[str] = field(default_factory=list)
    tests: Optional[str] = None
    notes: str = ""
    missing_fields: List[str] = field(default_factory=list)
    unparsed_files: List[str] = field(default_factory=list)


@dataclass
class Claims:
    report: Report
    files: List[Claim]
    tests_passed_claimed: bool
    completion_claimed: bool


# ---------------------------------------------------------------- WORKER REPORT
_REPORT_HDR = re.compile(r"^\s{0,3}(?:#{1,6}\s*|\*\*\s*)WORKER\s+REPORT\s*(?:#*|\*\*)\s*:?\s*$", re.I | re.M)
_KV = re.compile(r"^\s*[-*]?\s*(?:\*\*|__|`)?([A-Za-z_]+)(?:\*\*|__|`)?\s*:\s*(?:\*\*|__)?\s*(.*?)\s*(?:\*\*|__)?\s*$")
REPORT_FIELDS = ("status", "files_changed", "commands_run", "tests", "notes")
_FENCE = re.compile(r"^\s{0,3}(```|~~~)")


def strip_fences(text: str) -> str:
    """Blank out fenced code blocks (keeps line count), so templates/examples are not parsed."""
    out, in_fence = [], False
    for line in text.splitlines():
        if _FENCE.match(line):
            in_fence = not in_fence
            out.append("")
        else:
            out.append("" if in_fence else line)
    return "\n".join(out)


def _split_list(value: str) -> List[str]:
    value = value.strip().strip("[]").strip()
    if not value or value.lower() in ("none", "n/a", "-", "yok", "[]"):
        return []
    items = []
    for part in re.split(r"[,;]", value):
        part = part.strip().strip("`'\"").strip()
        if part:
            items.append(part)
    return items[:MAX_CLAIMS]


def parse_report(text: str) -> Report:
    text = strip_fences(text)
    matches = list(_REPORT_HDR.finditer(text))
    if not matches:
        return Report()
    rep = Report(present=True)
    seen = set()
    body = text[matches[-1].end():].splitlines()[1:40]
    for line in body:
        if not line.strip():
            continue
        if line.lstrip().startswith("#"):
            break
        m = _KV.match(line)
        if not m:
            continue
        key, val = m.group(1).lower(), m.group(2).strip().strip("`").strip()
        seen.add(key)
        if key == "status":
            v = val.lower().strip("` ")
            rep.status = v if v in ("done", "partial", "blocked") else "invalid"
        elif key == "files_changed":
            rep.files_changed = _split_list(val)
        elif key == "commands_run":
            rep.commands_run = _split_list(val)
        elif key == "tests":
            v = val.lower().strip("` ")
            rep.tests = v if v in ("passed", "failed", "not_run") else "invalid"
        elif key == "notes":
            rep.notes = val
    rep.missing_fields = [f for f in REPORT_FIELDS if f not in seen]
    return rep


# ---------------------------------------------------------------- prose claims
_EN_CREATE = r"(?:created|added|wrote|written|generated|new file)"
_EN_MODIFY = (r"(?:modified|updated|edited|changed|fixed|refactored|renamed|implemented|patched|"
              r"rewrote|rewritten|saved|adjusted|replaced|extended|moved)")
_EN_DELETE = r"(?:deleted|removed)"
_TR_CREATE = r"(?:oluşturdum|oluşturuldu|ekledim|eklendi|yazdım|yazıldı)"
_TR_MODIFY = (r"(?:güncelledim|güncellendi|değiştirdim|değiştirildi|düzenledim|düzenlendi|"
              r"düzelttim|düzeltildi|kaydettim|kaydedildi|yeniden yazdım|taşıdım)")
_TR_DELETE = r"(?:sildim|silindi|kaldırdım|kaldırıldı)"

_VERB = {
    CREATE: re.compile(rf"(?i)(?<![\w-])(?:{_EN_CREATE}|{_TR_CREATE})(?![\w-])"),
    MODIFY: re.compile(rf"(?i)(?<![\w-])(?:{_EN_MODIFY}|{_TR_MODIFY})(?![\w-])"),
    DELETE: re.compile(rf"(?i)(?<![\w-])(?:{_EN_DELETE}|{_TR_DELETE})(?![\w-])"),
}
_ANY_VERB = re.compile("|".join(f"(?:{p.pattern[4:]})" for p in _VERB.values()), re.I)

# A line is a claim if it starts (after a bullet / number) with a verb, or a first-person
# subject precedes the verb, or it is a Turkish first-person past form.
_LEAD = re.compile(r"^\s*(?:[-*+•]|\d+[.)])?\s*(?:\*\*)?\s*", re.U)
_FIRST_PERSON = re.compile(r"(?i)\b(?:I|I've|I have|we|we've|we have)\b")
_TR_FIRST = re.compile(r"(?i)(?:dım|dim|dum|düm|tım|tim|tum|tüm)\b")
_NEGATION = re.compile(
    r"(?i)\b(?:no changes?|not (?:modified|changed|touched|edited|updated|created)|"
    r"did(?:n'?t| not) (?:modify|change|touch|edit|update|create|delete|remove)|unchanged|untouched|"
    r"left (?:as is|alone)|without (?:changing|modifying|touching)|"
    r"(?:would|should|could|might|must)(?:n'?t| not)?\s+(?:also\s+)?(?:be|have|get|need)|"
    r"need(?:s|ed)? to|going to|plan(?:ned)? to|want(?:ed)? to|todo|follow[- ]up|next step|"
    r"nothing (?:needed|to|was)|no need|didn'?t (?:find|need)|did not (?:find|need)|not needed|"
    r"wasn'?t|was not|weren'?t|were not|haven'?t|have not|hasn'?t|has not|already (?:up to date|correct)|"
    r"değiştirmedim|dokunmadım|değişmedi|güncellemedim|aynı kaldı|gerekiyor|yapılmalı|gerek yok|"
    r"değiştirilmedi|yapacağım|edeceğim|ister misin|istersen)\b|\?\s*$"
)
_LIST_HEADER = re.compile(
    r"(?i)^\s*(?:#+\s*|\*\*)?\s*(?:files? (?:changed|modified|created|touched|edited|added|deleted|removed)|"
    r"(?:changed|modified|created|new|edited|added|deleted|removed) files?|changes|"
    r"değiştirilen dosyalar|değişen dosyalar|oluşturulan dosyalar|silinen dosyalar|yapılan değişiklikler)"
    r"\s*(?:\*\*)?\s*:?\s*(?:\*\*)?\s*$"
)
_BACKTICK = re.compile(r"`([^`\n]{1,300})`")
_PLAIN_PATH = re.compile(
    r"(?<![\w/.@:-])((?:\.{1,2}/)?(?:[\w.-]+/)*[\w-][\w.-]*\.[A-Za-z][A-Za-z0-9]{0,9})(?![\w/-])"
)
_TESTS_PASSED = re.compile(
    r"(?i)\b(?:all )?(?:\d+ )?tests? (?:now )?(?:pass(?:ed|es)?|are passing|is passing|succeeded|green)\b|"
    r"\btestler(?:in tamamı)? (?:geçti|başarılı|geçiyor)"
)
_TESTS_NEG = re.compile(r"(?i)(?:\b(?:not|never|no|some|most|if|once|until|when|should|will|would)\b|n't\b|değil)")
_COMPLETION = re.compile(r"(?i)\b(?:done|completed|finished|implemented|tamamlandı|bitti|tamamladım)\b")


def _looks_like_path(tok: str) -> bool:
    if not tok or len(tok) > 300 or " " in tok or "://" in tok or tok.startswith("-"):
        return False
    if re.search(r"[(){}\[\]<>|;$`\"'*?=,]", tok):
        return False
    if "/" in tok:
        return bool(re.search(r"[\w]", tok))
    return bool(re.search(r"\.[A-Za-z][A-Za-z0-9]{0,9}$", tok)) or tok in (
        "Makefile", "Dockerfile", "LICENSE", "README", "Gemfile", "Procfile", "Justfile")


def _report_path_ok(tok: str) -> bool:
    return bool(tok) and len(tok) <= 300 and "://" not in tok and not re.search(r"[\x00-\x1f\x7f]", tok)


def _tokens(line: str):
    """Yield (token, strong, position) for path-like tokens in the line."""
    seen = set()
    for m in _BACKTICK.finditer(line):
        tok = m.group(1).strip().rstrip(":.,")
        tok = re.sub(r":\d+(?::\d+)?$", "", tok)  # file.py:12
        if _looks_like_path(tok) and tok not in seen:
            seen.add(tok)
            yield tok, True, m.start()
    blank = lambda m: " " * len(m.group(0))  # noqa: E731  keep positions stable
    stripped = _BACKTICK.sub(blank, line)
    stripped = re.sub(r"\[[^\]]*\]\([^)]*\)", blank, stripped)
    for m in _PLAIN_PATH.finditer(stripped):
        tok = m.group(1)
        if _looks_like_path(tok) and tok not in seen:
            seen.add(tok)
            yield tok, "/" in tok, m.start()


_BULLET_BASE = re.compile(
    r"(?i)^\s*(?:[-*+•]|\d+[.)])\s+(?:\*\*)?(add|create|write|update|modify|edit|change|fix|refactor|rename|"
    r"implement|patch|save|delete|remove)\b")
_BASE_KIND = {"add": CREATE, "create": CREATE, "write": CREATE, "delete": DELETE, "remove": DELETE}
NEG_WINDOW = 15   # characters before the verb (or path) that a negation may sit in


def _verb_near(line: str, pos: int):
    """(start, end, kind) of the verb closest before `pos`, else the first one after it."""
    best_before, best_after = None, None
    for kind, pat in _VERB.items():
        for m in pat.finditer(line):
            if m.start() < pos and (best_before is None or m.start() > best_before[0]):
                best_before = (m.start(), m.end(), kind)
            elif m.start() >= pos and (best_after is None or m.start() < best_after[0]):
                best_after = (m.start(), m.end(), kind)
    if best_before is None:
        b = _BULLET_BASE.match(line)
        if b and b.start(1) < pos:
            best_before = (b.start(1), b.end(1), _BASE_KIND.get(b.group(1).lower(), MODIFY))
    return best_before or best_after


def _kind_near(line: str, pos: int) -> Optional[str]:
    v = _verb_near(line, pos)
    if not v:
        return None
    if v[2] == DELETE and re.search(r"(?i)\bfrom\b|'?(?:dan|den|tan|ten)\b", line[v[0]:]):
        return MODIFY
    if v[2] == CREATE and v[1] <= pos and re.search(r"(?i)\b(?:to|in|into|inside)\s*`?$", line[v[1]:pos]):
        return MODIFY   # "added X to `f`" changes f, it does not create it
    return v[2]


def _negated(line: str, tok_pos: int, tok_len: int) -> bool:
    """A negation only cancels a claim when it sits between the verb and the path, or just
    before whichever comes first ("I have not updated x", "x should be updated")."""
    v = _verb_near(line, tok_pos)
    if v is None:
        return bool(_NEGATION.search(line))
    start = max(0, min(v[0], tok_pos) - NEG_WINDOW)
    end = max(v[1], tok_pos + tok_len)
    return bool(_NEGATION.search(line[start:end]))


def _kind_of(line: str, header_kind: Optional[str] = None) -> Optional[str]:
    has = {k: bool(p.search(line)) for k, p in _VERB.items()}
    if has[DELETE] and not has[MODIFY] and not has[CREATE]:
        if re.search(r"(?i)\bfrom\b|'?(?:dan|den|tan|ten)\b", line):
            return MODIFY
        return DELETE
    if has[CREATE] and not has[MODIFY]:
        return CREATE
    if has[MODIFY] or has[CREATE] or has[DELETE]:
        return MODIFY
    return header_kind


def _is_claim_line(line: str) -> bool:
    rest = _LEAD.sub("", line, count=1)
    m = _ANY_VERB.search(rest)
    if not m:
        return False
    before = rest[: m.start()]
    if not before.strip() or re.fullmatch(r"\s*(?:`[^`]*`|[\w./-]+)\s*:?\s*", before):
        return True                      # "Updated x" / "- `x`: updated"
    if _FIRST_PERSON.search(before):
        return True
    if _TR_FIRST.search(m.group(0)):
        return True
    return False


def parse_text_claims(text: str) -> List[Claim]:
    claims: List[Claim] = []
    in_fence = False
    header_kind: Optional[str] = None
    in_report = False
    for raw in text.splitlines()[:MAX_LINES]:
        line = raw[:500]
        if line.lstrip().startswith(("```", "~~~")):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if _REPORT_HDR.match(line):
            in_report = True
            continue
        if in_report:
            if line.lstrip().startswith("#"):
                in_report = False
            else:
                continue
        if not line.strip():
            header_kind = None
            continue
        if _LIST_HEADER.match(line):
            low = line.lower()
            header_kind = (DELETE if re.search(r"delet|remov|silinen", low)
                           else CREATE if re.search(r"creat|new|added|oluşturulan", low) else MODIFY)
            continue
        is_item = bool(re.match(r"^\s*(?:[-*+•]|\d+[.)])\s+", line))
        if header_kind and is_item:
            kind = _kind_of(line, header_kind)
        elif _is_claim_line(line):
            kind = _kind_of(line)
        elif _BULLET_BASE.match(line):
            kind = _BASE_KIND.get(_BULLET_BASE.match(line).group(1).lower(), MODIFY)
        else:
            if header_kind and not is_item:
                header_kind = None
            continue
        if not kind:
            continue
        for tok, strong, pos in _tokens(line):
            if _negated(line, pos, len(tok) + 2):
                continue
            tok_kind = _kind_near(line, pos) or kind
            claims.append(Claim(tok, tok_kind, "text", strong))
            if len(claims) >= MAX_CLAIMS:
                return claims
    return claims


def extract(text: str) -> Claims:
    text = (text or "")[:MAX_TEXT]
    rep = parse_report(text)
    files: List[Claim] = []
    seen: Dict[str, int] = {}
    for tok in rep.files_changed:
        if _report_path_ok(tok):
            seen[tok] = len(files)
            files.append(Claim(tok, MODIFY, "report", True))
        else:
            rep.unparsed_files.append(tok)
    for c in parse_text_claims(text):
        if c.token in seen:
            prev = files[seen[c.token]]
            if prev.source == "report" and c.kind in (CREATE, DELETE):
                prev.kind = c.kind      # the prose says how; the report says what
            continue
        seen[c.token] = len(files)
        files.append(c)
    plain = strip_fences(text)
    tests_passed = rep.tests == "passed"
    for ln in plain.splitlines()[:MAX_LINES]:
        for m in _TESTS_PASSED.finditer(ln[:500]):
            if not _TESTS_NEG.search(ln[max(0, m.start() - 25):m.end()]):
                tests_passed = True
    completion = rep.status == "done" if rep.present else bool(_COMPLETION.search(plain[-2000:]))
    return Claims(rep, files[:MAX_CLAIMS], tests_passed, completion)
