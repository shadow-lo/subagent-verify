# Security and data handling

subagent-verify is a local Claude Code hook written in Python using only the standard library. It makes no network calls, sends no telemetry, and has no dependencies.

## Threat model

The hook reads text that it must not trust: the subagent's final message, the subagent transcript, file names in your repository, an issue-to-brief brief (derived from a GitHub issue that anyone may have written), and quota-pilot's `delegated-agents.json`. All of it is handled as data.

| Risk | What the hook does |
|---|---|
| Shell injection | No shell is ever used. Every `git` call is a fixed argv list via `subprocess.run(shell=False)` with a timeout. Paths are passed after `--`, and `GIT_LITERAL_PATHSPECS=1` stops them from being read as globs. Nothing from a brief (including its `test_command`) is ever executed. |
| Repo config running programs | The hook never runs `git status`, `diff-files` or `hash-object` (they can run a `filter.<x>.clean` program a subagent could plant via `.git/config` + `.gitattributes`). It lists files with `git ls-files`, reads HEAD with `ls-tree`/`rev-parse`, and hashes content itself. Every git call also sets `core.attributesFile=/dev/null`, `core.sshCommand=false`, `core.fsmonitor=false`, `core.hooksPath=/dev/null`, `diff.external=`, `protocol.allow=never`, `credential.helper=`, plus `--no-ext-diff --no-textconv` where relevant. The environment is reduced to `PATH`/`HOME`/temp vars, with `GIT_CONFIG_NOSYSTEM=1`, `GIT_OPTIONAL_LOCKS=0` (never takes `index.lock`) and `GIT_TERMINAL_PROMPT=0`. |
| Path traversal / symlink escape | Every claimed path is resolved with `realpath` and must stay inside the project root; `..`, `~`, absolute paths outside the root, NUL/control characters, symlinks pointing outside and anything under `.git/` are rejected and never opened. Brief paths must be repo-relative. |
| Writing outside the project | The results log is opened one directory component at a time with `O_NOFOLLOW` relative to the project root, and the file must be a regular file we own with exactly one link (symlinks and hard links to e.g. `~/.bashrc` are refused). |
| Reading special files | Files are opened with `O_NOFOLLOW`/`O_NONBLOCK` and must be regular files (`/dev/zero`, FIFOs and the like are refused). Input, transcript, JSON and git output sizes are capped. |
| Data leakage | Output contains only repo-relative paths (claims outside the project are reported without the path), short status codes and cleaned one-line notes. File contents, prompts and diffs are never echoed. Anything that looks like a secret (GitHub/GitLab/Slack/AWS/Google/Anthropic/OpenAI/Stripe/npm/Hugging Face tokens, JWTs, private keys, `*_TOKEN=` / `*_PASSWORD=` / `*_API_KEY=`-style pairs, credentials in URLs, long mixed-case base64 blobs; file paths and commit SHAs are left intact). All patterns are bounded so attacker-written text (issue briefs) cannot cause regex backtracking (ReDoS) is replaced with `[REDACTED]`. Control, bidi, zero-width and line/paragraph-separator characters (U+2028/2029) are stripped so text cannot hide, reorder itself or forge extra lines. |
| Prompt injection | Everything echoed is length-capped, one line, and the message tells the main agent that paths and claims are data. |
| Module hijacking | The launcher is run as `python3 -I` (isolated mode): no `PYTHON*` variables, no user site-packages, and the current directory is not on `sys.path`. |
| Disabling the check from a repo | Files inside the project (`.claude/subagent-verify.json`, briefs, `delegated-agents.json`) can only make checks **stricter**; they cannot turn the hook off, ignore an agent, disable the results log, redirect the brief or relabel the built-in tiers. That needs environment variables (`SUBAGENT_VERIFY_MODE=off`, `SUBAGENT_VERIFY_IGNORE`, ...). |
| The checked subagent weakening its own check | The brief, strict-agent list and fingerprints of the control files are captured **before** the subagent runs. Afterwards the pre-run and current briefs are merged (protections can only be added), and any change to a control file is a `CONTROL_CHANGED` failure. |
| Loops | `SubagentStop` blocks a strict subagent at most once (`stop_hook_active` is respected). Any internal error exits 0 so the session is never broken. |

## Where data lives

- **Snapshots:** `$XDG_STATE_HOME/subagent-verify/` (default `~/.local/state/subagent-verify/`), directory `0700`, files `0600`. They contain repo-relative paths and SHA-256 fingerprints only, never contents. File names are SHA-256 hashes of the session and tool-use ids, so untrusted ids never become paths. Each snapshot is deleted when it is used and anything older than 24 hours is pruned.
- **Results log (for quota-pilot):** `<project>/.claude/subagent-verify/results.jsonl`, mode `0600`, directory `0700`, with its own `.gitignore` (`*`). One line per check with schema, time, agent type, tier, verdict, count of unverified claims and whether the worker report was missing. No paths, contents or prompts. Disable with `SUBAGENT_VERIFY_RESULTS_LOG=0`.

## Known limits

- A directory inside the project could in theory be swapped for a symlink by a concurrent process while a snapshot is hashing; the result only goes into the private 0600 snapshot store, never into output.
- Detection is heuristic for free-form prose. Structured `## WORKER REPORT` blocks (quota-pilot) are checked exactly.
- The hook is a check, not a sandbox. Claude Code's permission settings remain the real enforcement layer.

## Reporting a problem

Please open a GitHub issue **without** secrets or private data, or use GitHub's private vulnerability reporting if it is enabled on the repository.
