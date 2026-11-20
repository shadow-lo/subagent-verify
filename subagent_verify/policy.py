"""Configuration, quota-pilot delegated agents, and issue-to-brief briefs.

Trust rule: files inside the project (config, briefs, delegated-agents.json) are untrusted
data. They may only make verification *stricter*. Turning checks off or ignoring an agent
type is only possible through environment variables set by the user."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

from .paths import safe_rel
from .util import clean, read_json_file

MODES = ("off", "warn", "strict")
DEFAULT_IGNORED = {"Explore", "Plan", "claude-code-guide", "web-fetch", "statusline-setup"}
# quota-pilot contract: these workers must always end with a WORKER REPORT and are strict.
BUILTIN_DELEGATED = {"haiku-worker": "haiku", "sonnet-worker": "sonnet"}
_NAME = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")


def base_name(agent_type: Optional[str]) -> str:
    """'quota-pilot:haiku-worker' -> 'haiku-worker'."""
    if not isinstance(agent_type, str) or not _NAME.match(agent_type):
        return ""
    return agent_type.rsplit(":", 1)[-1]


@dataclass
class Settings:
    mode: str = "warn"
    strict_agents: Set[str] = field(default_factory=set)
    ignored_agents: Set[str] = field(default_factory=lambda: set(DEFAULT_IGNORED))
    delegated: Dict[str, str] = field(default_factory=lambda: dict(BUILTIN_DELEGATED))
    results_log: bool = True
    brief_path: Optional[str] = None


def _names(value) -> Set[str]:
    if not isinstance(value, list):
        return set()
    return {base_name(v) for v in value[:200] if base_name(v)}


def _load_delegated(path: str, settings: Settings) -> None:
    data = read_json_file(path)
    if not isinstance(data, dict) or data.get("schema") != 1 or not isinstance(data.get("agents"), list):
        return
    for a in data["agents"][:100]:
        if not isinstance(a, dict):
            continue
        name = base_name(a.get("name"))
        if not name:
            continue
        tier = a.get("tier") if a.get("tier") in ("haiku", "sonnet", "opus") else None
        settings.delegated[name] = tier or settings.delegated.get(name) or "unknown"
        if a.get("verify") == "strict" or tier == "haiku":
            settings.strict_agents.add(name)
        # "off"/"normal" from a file never lowers anything: ignored on purpose.


def load(root: str, env: Optional[dict] = None) -> Settings:
    env = os.environ if env is None else env
    s = Settings()
    s.strict_agents |= set(BUILTIN_DELEGATED)

    # project config (untrusted): may only add strictness
    cfg = read_json_file(os.path.join(root, ".claude", "subagent-verify.json"))
    if isinstance(cfg, dict):
        if cfg.get("mode") == "strict":
            s.mode = "strict"
        s.strict_agents |= _names(cfg.get("strict_agents"))
        if cfg.get("results_log") is False:
            s.results_log = False
        bp = cfg.get("brief")
        if isinstance(bp, str) and safe_rel(bp):
            s.brief_path = os.path.join(root, bp)

    # quota-pilot delegated agents
    candidates = [os.path.join(root, ".claude", "quota-pilot", "delegated-agents.json")]
    home = env.get("HOME")
    if home:
        candidates.append(os.path.join(home, ".claude", "quota-pilot", "delegated-agents.json"))
    if env.get("SUBAGENT_VERIFY_DELEGATED"):
        candidates.append(env["SUBAGENT_VERIFY_DELEGATED"])
    for c in candidates:
        _load_delegated(c, s)

    # environment (trusted: set by the user)
    m = env.get("SUBAGENT_VERIFY_MODE", "").strip().lower()
    if m in MODES:
        s.mode = m
    if env.get("SUBAGENT_VERIFY_IGNORE") is not None:
        s.ignored_agents = {base_name(x.strip()) for x in env["SUBAGENT_VERIFY_IGNORE"].split(",") if base_name(x.strip())}
    s.strict_agents |= {base_name(x.strip()) for x in env.get("SUBAGENT_VERIFY_STRICT", "").split(",") if base_name(x.strip())}
    if env.get("SUBAGENT_VERIFY_RESULTS_LOG", "").lower() in ("0", "false", "off", "no"):
        s.results_log = False
    if env.get("SUBAGENT_VERIFY_BRIEF"):
        s.brief_path = env["SUBAGENT_VERIFY_BRIEF"]
    return s


def is_strict(settings: Settings, agent_type: Optional[str], model: Optional[str] = None) -> bool:
    name = base_name(agent_type)
    if settings.mode == "strict":
        return True
    if name and name in settings.strict_agents:
        return True
    return isinstance(model, str) and "haiku" in model.lower()


def is_ignored(settings: Settings, agent_type: Optional[str]) -> bool:
    name = base_name(agent_type)
    if name and (name in settings.strict_agents or name in settings.delegated):
        return False
    return bool(name) and name in settings.ignored_agents


# ---------------------------------------------------------------- issue-to-brief
@dataclass
class Brief:
    label: str
    criteria: List[str]
    likely: List[str]
    must_not_touch: List[str]


def load_brief(root: str, settings: Settings) -> Optional[Brief]:
    path = settings.brief_path or os.path.join(root, ".claude", "briefs", "current.json")
    data = read_json_file(path)
    if not isinstance(data, dict) or data.get("schema") != 1:
        return None
    src = data.get("source") if isinstance(data.get("source"), dict) else {}
    issue = src.get("issue")
    repo = src.get("repo")
    label = "brief"
    if isinstance(issue, int) and 0 < issue < 10**9:
        label = f"brief for issue #{issue}"
        if isinstance(repo, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}", repo):
            label = f"brief for {repo}#{issue}"
    criteria = []
    ac = data.get("acceptance_criteria")
    if isinstance(ac, list):
        for item in ac[:30]:
            if isinstance(item, dict):
                cid = clean(item.get("id"), 12)
                txt = clean(item.get("text"), 160)
                if txt:
                    criteria.append(f"{cid}: {txt}" if cid else txt)
    files = data.get("files") if isinstance(data.get("files"), dict) else {}

    def _paths(key):
        v = files.get(key)
        return [p for p in v[:300] if isinstance(p, str) and safe_rel(p)] if isinstance(v, list) else []

    return Brief(label, criteria, _paths("likely"), _paths("must_not_touch"))
