"""Unit tests for the policy matcher (no filesystem needed)."""

import unittest

from gitx.policy import HIDDEN, Policy, RW, RO, Rule


def resolve(patterns, default, path):
    p = Policy(default, [Rule(pat, m) for pat, m in patterns])
    return p.resolve(path)


class TestBasics(unittest.TestCase):
    def test_default(self):
        self.assertEqual(resolve([], "rw", "anything/file.txt"), RW)
        self.assertEqual(resolve([], "ro", "anything/file.txt"), RO)

    def test_exact_and_descendants(self):
        self.assertEqual(resolve([("secrets", "hidden")], "rw", "secrets"), HIDDEN)
        self.assertEqual(resolve([("secrets", "hidden")], "rw", "secrets/token"), HIDDEN)
        self.assertEqual(resolve([("secrets", "hidden")], "rw", "secrets/a/b/c"), HIDDEN)
        # no-slash patterns match at any depth (gitignore-style) ...
        self.assertEqual(resolve([("secrets", "hidden")], "rw", "src/secrets"), HIDDEN)
        # ... but a leading '/' anchors at the repo root
        self.assertEqual(resolve([("/secrets", "hidden")], "rw", "src/secrets"), RW)
        self.assertEqual(resolve([("/secrets", "hidden")], "rw", "secrets/x"), HIDDEN)

    def test_no_slash_matches_any_depth(self):
        self.assertEqual(resolve([(".env", "hidden")], "rw", ".env"), HIDDEN)
        self.assertEqual(resolve([(".env", "hidden")], "rw", "sub/.env"), HIDDEN)
        self.assertEqual(resolve([(".env", "hidden")], "rw", "a/b/.env"), HIDDEN)
        self.assertEqual(resolve([(".env", "hidden")], "rw", "a/.envrc"), RW)

    def test_slash_anchors_at_root(self):
        self.assertEqual(resolve([("generated/**", "ro")], "rw", "generated/x"), RO)
        self.assertEqual(resolve([("generated/**", "ro")], "rw", "generated"), RO)  # dir itself
        self.assertEqual(resolve([("generated/**", "ro")], "rw", "sub/generated/x"), RW)  # slash -> rooted

    def test_star_within_segment(self):
        self.assertEqual(resolve([("*.lock", "ro")], "rw", "a.lock"), RO)
        self.assertEqual(resolve([("*.lock", "ro")], "rw", "sub/a.lock"), RO)  # any depth
        self.assertEqual(resolve([("*.lock", "ro")], "rw", "dir/a.lock"), RO)
        self.assertEqual(resolve([("*.lock", "ro")], "rw", "dir/a.b.lock"), RO)
        self.assertEqual(resolve([("*.lock", "ro")], "rw", "dir/a"), RW)
        self.assertEqual(resolve([("docs/*.md", "ro")], "rw", "docs/x.md"), RO)
        self.assertEqual(resolve([("docs/*.md", "ro")], "rw", "docs/sub/x.md"), RW)  # * stays in segment

    def test_question_mark(self):
        self.assertEqual(resolve([("file?.txt", "ro")], "rw", "file1.txt"), RO)
        self.assertEqual(resolve([("file?.txt", "ro")], "rw", "file12.txt"), RW)

    def test_leading_globstar(self):
        self.assertEqual(resolve([("**/snapshots", "hidden")], "rw", "snapshots"), HIDDEN)
        self.assertEqual(resolve([("**/snapshots", "hidden")], "rw", "a/snapshots"), HIDDEN)
        self.assertEqual(resolve([("**/snapshots", "hidden")], "rw", "a/b/snapshots/x"), HIDDEN)

    def test_middle_globstar(self):
        pat = [("src/**/generated.ts", "ro")]
        self.assertEqual(resolve(pat, "rw", "src/generated.ts"), RO)
        self.assertEqual(resolve(pat, "rw", "src/a/generated.ts"), RO)
        self.assertEqual(resolve(pat, "rw", "src/a/b/generated.ts"), RO)
        self.assertEqual(resolve(pat, "rw", "src/a/generated.tsx"), RW)

    def test_trailing_globstar_descendants(self):
        self.assertEqual(resolve([("a/**", "ro")], "rw", "a"), RO)
        self.assertEqual(resolve([("a/**", "ro")], "rw", "a/b"), RO)
        self.assertEqual(resolve([("a/**", "ro")], "rw", "a/b/c/d.txt"), RO)
        self.assertEqual(resolve([("a/**", "ro")], "rw", "ab"), RW)

    def test_root_and_globs(self):
        self.assertEqual(resolve([("**", "ro")], "rw", "x/y/z"), RO)
        self.assertEqual(resolve([("**", "ro")], "rw", "x"), RO)
        self.assertEqual(resolve([("*", "ro")], "rw", "x"), RO)
        self.assertEqual(resolve([("*", "ro")], "rw", "x/y"), RO)  # descendants of root match

    def test_last_match_wins(self):
        pats = [("build", "hidden"), ("build/out.log", "rw")]
        self.assertEqual(resolve(pats, "rw", "build/out.log"), RW)
        self.assertEqual(resolve(pats, "rw", "build/other"), HIDDEN)
        # unlock re-locks deeper paths
        pats = [("a/**", "ro"), ("a/b/**", "rw"), ("a/b/c.lock", "ro")]
        self.assertEqual(resolve(pats, "rw", "a/x"), RO)
        self.assertEqual(resolve(pats, "rw", "a/b/x"), RW)
        self.assertEqual(resolve(pats, "rw", "a/b/c.lock"), RO)

    def test_forced_hidden(self):
        p = Policy("rw", [Rule(".git", "rw"), Rule(".gitx", "rw")])
        self.assertEqual(p.resolve(".git"), HIDDEN)
        self.assertEqual(p.resolve(".git/objects/ab/c"), HIDDEN)
        self.assertEqual(p.resolve(".gitx/worktrees/s1/x"), HIDDEN)
        # not just as top-level component
        self.assertEqual(p.resolve("src/.git"), RW)

    def test_root_resolves_to_default(self):
        p = Policy("ro", [])
        self.assertEqual(p.resolve(""), RO)

    def test_invalid(self):
        with self.assertRaises(ValueError):
            Rule("x", "execute")
        with self.assertRaises(ValueError):
            Rule("", "ro")
        with self.assertRaises(ValueError):
            Rule("a/../b", "ro")

    def test_char_class(self):
        self.assertEqual(resolve([("file[0-9].txt", "ro")], "rw", "file5.txt"), RO)
        self.assertEqual(resolve([("file[0-9].txt", "ro")], "rw", "filex.txt"), RW)
        self.assertEqual(resolve([("file[!0-9].txt", "ro")], "rw", "filex.txt"), RO)


if __name__ == "__main__":
    unittest.main()
