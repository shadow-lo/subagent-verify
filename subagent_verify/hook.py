"""Claude Code hook entry point.

Events handled (all fail open: any internal error exits 0 without blocking the session):
  PreToolUse  (Task|Agent)  -> snapshot the working tree + pre-run brief/config before the subagent runs
  SubagentStart             -> same, keyed by agent_id; remind delegated workers of the WORKER REPORT
  PostToolUse (Task|Agent)  -> verify the subagent's claims, tell the main agent
  SubagentStop              -> verify from the snapshot and the subagent's transcript; for strict agents,
                               block the stop once so the subagent can fix or correct itself
"""
from __future__ import annotations

import json
import os
import re
import stat
import sys
import time
from typing import Any, Optional, Tuple

from . import claims as claims_mod
from . import gitutil, policy, snapshot, state, transcript
from .paths import is_within, real_root
from .util import MAX_STDIN_BYTES, clean
from .verify import FAIL, render, verify

AGENT_TOOLS = {"Task", "Agent"}
MAX_RESPONSE_TEXT = 200_000
RESULTS_MAX_BYTES = 1024 * 1024
START_REMINDER = ("subagent-verify is active: when you finish, your claims are checked against git and the "
                  "filesystem. Only list files you really changed, only say tests passed if you ran them, and end "
                  "with the '## WORKER REPORT' block (status, files_changed, commands_run, tests, notes).")


def _read_stdin() -> Optional[dict]:
    data = sys.stdin.buffer.read(MAX_STDIN_BYTES + 1)
    if len(data) > MAX_STDIN_BYTES:
        return None
    try:
        obj = json.loads(data.decode("utf-8", "replace"))
    except (ValueError, RecursionError):
        return None
    return obj if isinstance(obj, dict) else None


def _roots(payload: dict) -> Tuple[Optional[str], Optional[str]]:
    """(root, base): root is the git toplevel (or the project dir outside git); base is the
    Claude Code project dir, against which relative claims are resolved first."""
    for cand in (os.environ.get("CLAUDE_PROJECT_DIR"), payload.get("cwd")):
        base = real_root(cand) if isinstance(cand, str) and cand else None
        if base:
            top = gitutil.toplevel(base)
            top = real_root(top) if top else None
            if top and is_within(base, top):
                return top, base
            return base, base
    return None, None


def _text(obj: Any, depth: int = 0) -> str:
    """Pull human text out of a Task/Agent tool_response of any shape (never the prompt)."""
    if depth > 6:
        return ""
    if isinstance(obj, str):
        return obj
    if isinstance(obj, list):
        return "\n".join(_text(x, depth + 1) for x in obj[:200])
    if isinstance(obj, dict):
        if obj.get("type") == "text" and isinstance(obj.get("text"), str):
            return obj["text"]
        for key in ("content", "result", "text", "output", "message"):
            if key in obj:
                return _text(obj[key], depth + 1)
    return ""


def _subagent_transcript(payload: dict, response: Any) -> Optional[str]:
    """Locate <session dir>/subagents/agent-<id>.jsonl next to the main transcript."""
    agent_id = response.get("agentId") if isinstance(response, dict) else None
    tpath = payload.get("transcript_path")
    sid = payload.get("session_id")
    if not (isinstance(agent_id, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", agent_id)):
        return None
    if not (isinstance(tpath, str) and isinstance(sid, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", sid)):
        return None
    cand = os.path.join(os.path.dirname(tpath), sid, "subagents", f"agent-{agent_id}.jsonl")
    return cand if os.path.isfile(cand) else None


# ---------------------------------------------------------------- results log
_DIR_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)


def _open_dir_chain(root: str, parts) -> Optional[int]:
    """Open root/part1/part2 one component at a time with O_NOFOLLOW (no symlink can redirect
    us mid-way). Creates missing components with mode 0700. Returns a dir fd or None."""
    try:
        fd = os.open(root, _DIR_FLAGS)
    except OSError:
        return None
    try:
        for i, part in enumerate(parts):
            try:
                nfd = os.open(part, _DIR_FLAGS, dir_fd=fd)
            except FileNotFoundError:
                os.mkdir(part, 0o700 if i == len(parts) - 1 else 0o755, dir_fd=fd)
                nfd = os.open(part, _DIR_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = nfd
        st = os.fstat(fd)
        if hasattr(os, "getuid") and st.st_uid != os.getuid():
            os.close(fd)
            return None
        return fd
    except OSError:
        os.close(fd)
        return None


def _open_own_file(dfd: int, name: str, flags: int) -> Optional[int]:
    """Open a regular, single-link file we own inside dfd; refuse symlinks and hard links."""
    try:
        fd = os.open(name, flags | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0), 0o600, dir_fd=dfd)
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if (stat.S_ISREG(st.st_mode) and st.st_nlink == 1
                and (not hasattr(os, "getuid") or st.st_uid == os.getuid())):
            return fd
    except OSError:
        pass
    os.close(fd)
    return None


def _log_result(root: str, settings: policy.Settings, res, agent_type: str) -> None:
    """Append one metadata-only line for quota-pilot. No contents, prompts or paths."""
    if not settings.results_log:
        return
    dfd = _open_dir_chain(root, [".claude", "subagent-verify"])
    if dfd is None:
        return
    try:
        gfd = _open_own_file(dfd, ".gitignore", os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        if gfd is not None:
            with os.fdopen(gfd, "w") as fh:
                fh.write("*\n")
        try:
            st = os.stat("results.jsonl", dir_fd=dfd, follow_symlinks=False)
            if st.st_size > RESULTS_MAX_BYTES:
                os.replace("results.jsonl", "results.jsonl.1", src_dir_fd=dfd, dst_dir_fd=dfd)
        except FileNotFoundError:
            pass
        name = policy.base_name(agent_type)
        line = json.dumps({
            "schema": 1,
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "agent_type": name or "unknown",
            "tier": settings.delegated.get(name),
            "verdict": res.verdict,
            "unverified_claims": res.unverified,
            "missing_report": res.missing_report,
        }, separators=(",", ":"))
        fd = _open_own_file(dfd, "results.jsonl", os.O_WRONLY | os.O_APPEND | os.O_CREAT)
        if fd is not None:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
    except OSError:
        pass
    finally:
        os.close(dfd)


# ---------------------------------------------------------------- pre-run context
def _control_sigs(root: str) -> dict:
    return {p: snapshot.file_sig(os.path.join(root, p)) for p in policy.CONTROL_FILES}


def _pre_context(root: str, settings: policy.Settings) -> dict:
    brief = policy.load_brief(root, settings)
    extra = list(policy.CONTROL_FILES)
    if brief:
        extra += snapshot.extra_paths(root, brief.must_not_touch)
    return {"snap": snapshot.take(root, extra), "brief": policy.brief_to_dict(brief),
            "strict_agents": sorted(settings.strict_agents), "control": _control_sigs(root)}


def _post_context(root: str, settings: policy.Settings, data: Optional[dict]):
    """Combine pre-run and current inputs so a subagent cannot weaken its own check."""
    current = policy.load_brief(root, settings)
    if not data:
        return None, current, []
    if isinstance(data.get("strict_agents"), list):
        settings.strict_agents |= {policy.base_name(x) for x in data["strict_agents"][:200] if policy.base_name(x)}
    brief = policy.merge_briefs(policy.brief_from_dict(data.get("brief")), current)
    pre = data.get("control") if isinstance(data.get("control"), dict) else {}
    now = _control_sigs(root)
    changed = [p for p in policy.CONTROL_FILES if p in pre and pre.get(p) != now.get(p)]
    return data["snap"], brief, changed


def _emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False))
    sys.stdout.flush()


def handle(payload: dict) -> int:
    event = payload.get("hook_event_name")
    root, base = _roots(payload)
    if not root:
        return 0
    settings = policy.load(root)
    if settings.mode == "off":
        return 0
    session_id = payload.get("session_id") if isinstance(payload.get("session_id"), str) else ""

    if event in ("PreToolUse", "PostToolUse"):
        if payload.get("tool_name") not in AGENT_TOOLS:
            return 0
        tool_input = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}
        agent_type = tool_input.get("subagent_type") if isinstance(tool_input.get("subagent_type"), str) else "general-purpose"
        if policy.is_ignored(settings, agent_type):
            return 0
        tool_use_id = payload.get("tool_use_id") if isinstance(payload.get("tool_use_id"), str) else ""
        if tool_input.get("run_in_background") is True:
            return 0  # still running when PostToolUse fires; SubagentStart/SubagentStop cover it
        if event == "PreToolUse":
            state.save(session_id, tool_use_id, root, _pre_context(root, settings))
            return 0
        data = state.pop(session_id, tool_use_id, root)
        response = payload.get("tool_response")
        if isinstance(response, dict) and response.get("status") not in (None, "completed"):
            return 0  # async_launched / failed: nothing finished to verify yet
        before, brief, control_changed = _post_context(root, settings, data)
        text = _text(response)[:MAX_RESPONSE_TEXT]
        tpath = _subagent_transcript(payload, response)
        ev = transcript.read(tpath) if tpath else None
        if ev is not None and not ev.readable:
            ev = None
        if not text and ev:
            text = ev.final_text
        model = tool_input.get("model") if isinstance(tool_input.get("model"), str) else None
        if not model and isinstance(response, dict) and isinstance(response.get("resolvedModel"), str):
            model = response["resolvedModel"]
        level = policy.strictness(settings, agent_type, model)
        delegated = policy.base_name(agent_type) in settings.delegated
        c = claims_mod.extract(text)
        if not c.files and not c.tests_passed_claimed and not delegated and before is None and not brief:
            return 0
        res = verify(root, c, agent=agent_type, level=level, delegated=delegated, base=base,
                     before=before, evidence=ev, brief=brief, control_changed=control_changed)
        _log_result(root, settings, res, agent_type)
        if res.verdict == "ok" and not res.criteria:
            return 0
        msg = render(res)
        if res.verdict == "fail" and res.strict:
            # "block" on PostToolUse does not undo anything; it makes Claude read the reason.
            out = {"decision": "block", "reason": msg, "systemMessage": clean(msg.splitlines()[0], 200)}
        else:
            out = {"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": msg}}
            if res.verdict == "fail":
                out["systemMessage"] = clean(msg.splitlines()[0], 200)
        _emit(out)
        return 0

    if event in ("SubagentStart", "SubagentStop"):
        agent_type = payload.get("agent_type") if isinstance(payload.get("agent_type"), str) else ""
        agent_id = payload.get("agent_id") if isinstance(payload.get("agent_id"), str) else ""
        if policy.is_ignored(settings, agent_type):
            return 0
        key = f"agent:{agent_id}" if agent_id else ""
        if event == "SubagentStart":
            if key:
                state.save(session_id, key, root, _pre_context(root, settings))
            if policy.base_name(agent_type) in settings.delegated:
                _emit({"hookSpecificOutput": {"hookEventName": "SubagentStart", "additionalContext": START_REMINDER}})
            return 0
        data = state.pop(session_id, key, root) if key else None
        before, brief, control_changed = _post_context(root, settings, data)
        ev = transcript.read(payload.get("agent_transcript_path"))
        level = policy.strictness(settings, agent_type, ev.model)
        delegated = policy.base_name(agent_type) in settings.delegated
        text = payload.get("last_assistant_message") if isinstance(payload.get("last_assistant_message"), str) else ""
        text = text or ev.final_text
        if not ev.readable and not text:
            return 0
        c = claims_mod.extract(text)
        if not c.files and not c.tests_passed_claimed and not delegated and not control_changed:
            return 0
        res = verify(root, c, agent=agent_type or "subagent", level=level, delegated=delegated, base=base,
                     before=before, evidence=ev if ev.readable else None, brief=brief,
                     control_changed=control_changed)
        if res.verdict != "fail" or not res.strict or payload.get("stop_hook_active"):
            return 0  # PostToolUse reports to the main agent; never loop
        fails = [i for i in res.items if i.level == FAIL]
        reason = ["subagent-verify found problems with your final report. Before stopping, either do the work "
                  "or correct the report so it only claims what really happened:"]
        reason += [f"- {i.code}{(' ' + i.path) if i.path else ''}: {i.detail}" for i in fails[:15]]
        if delegated:
            reason.append("End with the required '## WORKER REPORT' block (status, files_changed, commands_run, tests, notes).")
        _emit({"decision": "block", "reason": "\n".join(reason)})
        return 0
    return 0


def main() -> int:
    try:
        payload = _read_stdin()
        if payload is None:
            return 0
        return handle(payload)
    except Exception as exc:  # fail open, never leak details
        try:
            sys.stderr.write(f"subagent-verify: internal error ({type(exc).__name__}); skipped\n")
        except Exception:
            pass
        return 0
