"""Thin wrapper around the git CLI.  Git keeps doing 100% of version control;
this module only shells out."""

import subprocess


def git(*args, cwd=None, check=True):
    """Run git and return stdout; raise RuntimeError on failure."""
    cmd = ["git", *args]
    proc = subprocess.run(cmd, cwd=str(cwd) if cwd else None,
                          capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise RuntimeError(
            "`git %s` failed (exit %d): %s" % (" ".join(args), proc.returncode,
                                               proc.stderr.strip()))
    return proc.stdout


def git_rc(*args, cwd=None):
    """Run git, return (returncode, stdout, stderr) without raising."""
    proc = subprocess.run(["git", *args], cwd=str(cwd) if cwd else None,
                          capture_output=True, text=True)
    return proc.returncode, proc.stdout, proc.stderr


def rev_parse(repo, rev):
    out = git("rev-parse", "--verify", "%s^{commit}" % rev, cwd=repo)
    return out.strip()
