import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(REPO, "scripts", "subagent_verify.py")
sys.path.insert(0, REPO)


def git(cwd, *args):
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "-c", "commit.gpgsign=false", *args],
                   cwd=cwd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class HookCase(unittest.TestCase):
    """Each test gets a fresh git repo, HOME and state dir."""

    use_git = True

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sv-test-")
        self.root = os.path.realpath(os.path.join(self.tmp, "repo"))
        os.makedirs(self.root)
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(self.home)
        self.state = os.path.join(self.tmp, "state")
        if self.use_git:
            git(self.root, "init", "-q")
        self.write("src/app.py", "print('hi')\n")
        self.write("README.md", "# demo\n")
        if self.use_git:
            git(self.root, "add", "-A")
            git(self.root, "commit", "-qm", "init")
        self.n = 0

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- helpers
    def write(self, rel, text):
        p = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as fh:
            fh.write(text)
        return p

    def env(self, **extra):
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": self.home,
               "SUBAGENT_VERIFY_STATE_DIR": self.state}
        env.update(extra)
        return env

    def run_hook(self, payload, raw=None, **env):
        data = raw if raw is not None else json.dumps(payload).encode()
        proc = subprocess.run([sys.executable, "-I", SCRIPT], input=data, capture_output=True,
                              env=self.env(**env), cwd=self.tmp, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr.decode())
        out = proc.stdout.decode().strip()
        return json.loads(out) if out else None

    def pre(self, agent="general-purpose", tool_use_id=None, **env):
        self.n += 1
        self.tuid = tool_use_id or f"toolu_{self.n}"
        return self.run_hook({"hook_event_name": "PreToolUse", "tool_name": "Task", "session_id": "sess1",
                              "tool_use_id": self.tuid, "cwd": self.root,
                              "tool_input": {"subagent_type": agent, "prompt": "x"}}, **env)

    def post(self, text, agent="general-purpose", tool_use_id=None, model=None, response=None, **env):
        ti = {"subagent_type": agent, "prompt": "x"}
        if model:
            ti["model"] = model
        return self.run_hook({"hook_event_name": "PostToolUse", "tool_name": "Task", "session_id": "sess1",
                              "tool_use_id": tool_use_id or self.tuid, "cwd": self.root,
                              "tool_input": ti,
                              "tool_response": response if response is not None else
                              {"content": [{"type": "text", "text": text}]}}, **env)

    def message(self, out):
        if out is None:
            return ""
        if "reason" in out:
            return out["reason"]
        return out.get("hookSpecificOutput", {}).get("additionalContext", "")


def report(files="none", status="done", tests="not_run", commands="none", notes="ok"):
    return (f"\n\n## WORKER REPORT\nstatus: {status}\nfiles_changed: {files}\n"
            f"commands_run: {commands}\ntests: {tests}\nnotes: {notes}\n")


def slurp(path):
    with open(path) as fh:
        return fh.read()
