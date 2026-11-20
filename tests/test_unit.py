import os
import tempfile
import unittest

from helpers import REPO  # noqa: F401  (sets sys.path)
from subagent_verify import claims, paths, policy, util


class TestClaims(unittest.TestCase):
    def test_worker_report(self):
        c = claims.extract("work\n## WORKER REPORT\nstatus: done\nfiles_changed: a.py, `src/b.py`\n"
                           "commands_run: pytest -q\ntests: passed\nnotes: fine\n")
        self.assertTrue(c.report.present)
        self.assertEqual(c.report.status, "done")
        self.assertEqual([f.token for f in c.files], ["a.py", "src/b.py"])
        self.assertEqual(c.report.commands_run, ["pytest -q"])
        self.assertTrue(c.tests_passed_claimed)

    def test_report_none_and_invalid(self):
        c = claims.extract("## WORKER REPORT\nstatus: maybe\nfiles_changed: none\ntests: sure\n")
        self.assertEqual(c.files, [])
        self.assertEqual(c.report.status, "invalid")
        self.assertEqual(c.report.tests, "invalid")

    def test_prose_kinds(self):
        c = claims.extract("I updated `a.py` and created `b.py`.\n- Deleted `old/x.py`\n"
                           "- Removed the unused import from src/y.py\n")
        kinds = {f.token: f.kind for f in c.files}
        self.assertEqual(kinds, {"a.py": "modify", "b.py": "create", "old/x.py": "delete", "src/y.py": "modify"})

    def test_descriptions_are_not_claims(self):
        c = claims.extract("The config is created in src/config.py.\nThis function updates state in `src/s.py`.\n"
                           "You should update src/t.py.\nI did not modify src/u.py.\n")
        self.assertEqual(c.files, [])

    def test_code_fence_ignored(self):
        c = claims.extract("```\nI updated src/fake.py\n```\n")
        self.assertEqual(c.files, [])

    def test_turkish(self):
        c = claims.extract("src/y.py dosyasını güncelledim.\n`docs/a.md` oluşturdum.\nsrc/z.py dosyasına dokunmadım.\n")
        kinds = {f.token: f.kind for f in c.files}
        self.assertEqual(kinds, {"src/y.py": "modify", "docs/a.md": "create"})

    def test_files_changed_list(self):
        c = claims.extract("Files changed:\n- src/a.py\n- `docs/b.md`\n\nUnrelated src/c.py line\n")
        self.assertEqual([f.token for f in c.files], ["src/a.py", "docs/b.md"])

    def test_urls_and_versions_not_paths(self):
        c = claims.extract("I updated https://example.com/a.py and bumped to v2.0\n")
        self.assertEqual(c.files, [])

    def test_caps(self):
        text = "\n".join(f"- Updated `f{i}.py`" for i in range(1000))
        self.assertLessEqual(len(claims.extract(text).files), claims.MAX_CLAIMS)


class TestPaths(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = os.path.realpath(self.tmp.name)
        os.makedirs(os.path.join(self.root, "src"))
        open(os.path.join(self.root, "src", "a.py"), "w").close()

    def tearDown(self):
        self.tmp.cleanup()

    def test_inside(self):
        self.assertEqual(paths.resolve_in_root("src/a.py", self.root), "src/a.py")
        self.assertEqual(paths.resolve_in_root("./src/../src/a.py", self.root), "src/a.py")
        self.assertEqual(paths.resolve_in_root(os.path.join(self.root, "src/a.py"), self.root), "src/a.py")

    def test_traversal_and_outside(self):
        for bad in ("../x", "../../etc/passwd", "/etc/passwd", "~/.ssh/id_rsa", "src/\0a.py",
                    ".git/config", "./.git/hooks/pre-commit", "", ".", "..", "a\nb"):
            self.assertIsNone(paths.resolve_in_root(bad, self.root), bad)

    def test_symlink_escape(self):
        os.symlink("/etc", os.path.join(self.root, "etc_link"))
        os.symlink("/etc/passwd", os.path.join(self.root, "pw"))
        self.assertIsNone(paths.resolve_in_root("etc_link/passwd", self.root))
        self.assertIsNone(paths.resolve_in_root("pw", self.root))

    def test_safe_rel(self):
        self.assertTrue(paths.safe_rel("a/b.py"))
        for bad in ("/a", "../a", "a/../../b", "C:/x", "~x", "a\x00"):
            self.assertFalse(paths.safe_rel(bad), bad)


class TestUtil(unittest.TestCase):
    def test_redact(self):
        samples = ["ghp_" + "a" * 36, "github_pat_" + "b" * 40, "AKIAABCDEFGHIJKLMNOP", "sk-ant-" + "c" * 30,
                   "xoxb-1234567890-abc", "-----BEGIN RSA PRIVATE KEY-----\nMIIx\n-----END RSA PRIVATE KEY-----",
                   "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.c2lnbmF0dXJlMTIz"]
        for s in samples:
            self.assertNotIn(s, util.redact(f"x {s} y"), s)
        self.assertEqual(util.redact("password=hunter22"), "password=[REDACTED]")
        self.assertEqual(util.redact("https://bob:pw123@host/x"), "https://[REDACTED]@host/x")

    def test_clean(self):
        out = util.clean("a\x1b[31mb\u202ec\nd" + "x" * 500, 50)
        self.assertNotIn("\x1b", out)
        self.assertNotIn("\u202e", out)
        self.assertNotIn("\n", out)
        self.assertLessEqual(len(out), 50)


class TestPolicy(unittest.TestCase):
    def test_base_name(self):
        self.assertEqual(policy.base_name("quota-pilot:haiku-worker"), "haiku-worker")
        self.assertEqual(policy.base_name("bad name;rm"), "")

    def test_builtin_delegated_strict(self):
        with tempfile.TemporaryDirectory() as d:
            s = policy.load(d, env={"HOME": d})
            self.assertTrue(policy.is_strict(s, "quota-pilot:haiku-worker"))
            self.assertTrue(policy.is_strict(s, "general-purpose", model="haiku"))
            self.assertFalse(policy.is_strict(s, "general-purpose"))
            self.assertTrue(policy.is_ignored(s, "Explore"))
            self.assertFalse(policy.is_ignored(s, "sonnet-worker"))


if __name__ == "__main__":
    unittest.main()
