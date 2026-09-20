"""Agent sessions: a git linked worktree + metadata, per session.

    Session = git worktree (branch agent/<id>) + path policy + git locks

All version management is plain git:
  status     -> git status
  diff       -> git diff
  checkpoint -> git add -A && git commit
  rollback   -> git reset --hard / git clean -fd
  finish     -> git worktree remove (branch is kept for normal git merge)

The session worktree is the agent's workspace directly (no FUSE, no
sandbox); locks are applied git-natively — see gitx.locks.
"""

import json
import re
import secrets
import time
from pathlib import Path

from . import gitcmd, locks
from .config import GITX_DIR

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def sessions_dir(repo):
    return Path(repo) / GITX_DIR / "sessions"


def worktrees_dir(repo):
    return Path(repo) / GITX_DIR / "worktrees"


def valid_id(sid):
    return bool(_ID_RE.match(sid))


class Session:
    def __init__(self, id, branch, base, base_rev, worktree, created):
        self.id = id
        self.branch = branch
        self.base = base            # original spec, e.g. "HEAD"
        self.base_rev = base_rev    # resolved sha
        self.worktree = worktree    # relative to repo
        self.created = created

    # -- persistence -------------------------------------------------
    def to_dict(self):
        return {
            "id": self.id,
            "branch": self.branch,
            "base": self.base,
            "base_rev": self.base_rev,
            "worktree": self.worktree,
            "created": self.created,
        }

    @classmethod
    def from_dict(cls, d):
        return cls(d["id"], d["branch"], d["base"], d["base_rev"],
                   d["worktree"], d.get("created", ""))

    def wt(self, repo):
        return Path(repo) / self.worktree

    def meta_path(self, repo):
        return sessions_dir(repo) / ("%s.json" % self.id)

    def lock_path(self, repo):
        return sessions_dir(repo) / ("%s.lock" % self.id)


def new_id(name=None):
    if name:
        if not valid_id(name):
            raise ValueError(
                "invalid session name %r (use letters/digits/./_/-, no slashes)" % name)
        return name
    return "s-%s-%s" % (time.strftime("%Y%m%d-%H%M%S"), secrets.token_hex(2))


def create(repo, base="HEAD", name=None):
    repo = Path(repo)
    sid = new_id(name)
    branch = "agent/%s" % sid
    base_rev = gitcmd.rev_parse(repo, base)
    wt = worktrees_dir(repo) / sid
    if wt.exists():
        raise RuntimeError("worktree already exists: %s" % wt)
    gitcmd.git("worktree", "add", "-b", branch, str(wt), base_rev, cwd=repo)
    s = Session(sid, branch, base, base_rev,
                "%s/worktrees/%s" % (GITX_DIR, sid),
                time.strftime("%Y-%m-%dT%H:%M:%S"))
    sessions_dir(repo).mkdir(parents=True, exist_ok=True)
    s.meta_path(repo).write_text(json.dumps(s.to_dict(), indent=2) + "\n")
    return s


def load(repo, sid):
    p = sessions_dir(repo) / ("%s.json" % sid)
    if not p.exists():
        raise RuntimeError("no such session: %s (see `gitx session list`)" % sid)
    return Session.from_dict(json.loads(p.read_text()))


def list_sessions(repo):
    out = []
    sd = sessions_dir(repo)
    if sd.is_dir():
        for p in sorted(sd.glob("*.json")):
            try:
                out.append(Session.from_dict(json.loads(p.read_text())))
            except Exception:
                continue
    return out


def resolve_sid(repo, sid=None):
    """Auto-select the single existing session when sid is omitted."""
    if sid:
        return load(repo, sid)
    sessions = list_sessions(repo)
    if not sessions:
        raise RuntimeError("no sessions — create one with `gitx session create`")
    if len(sessions) > 1:
        raise RuntimeError("multiple sessions exist; specify one of: %s"
                           % " ".join(s.id for s in sessions))
    return sessions[0]


def finish(repo, sid, force=False):
    """Remove the session worktree + metadata; keep the branch for merging."""
    repo = Path(repo)
    s = load(repo, sid)
    wt = s.wt(repo)
    if wt.is_dir():
        dirty = gitcmd.git("status", "--porcelain", cwd=wt)
        if dirty.strip() and not force:
            raise RuntimeError(
                "session %s has uncommitted changes — run `gitx checkpoint %s` "
                "first, or use --force to discard" % (sid, sid))
        # strip chmod locks first: worktree remove needs write perms to
        # unlink the 444/555 files we created
        locks.remove(repo, sid, wt)
        gitcmd.git("worktree", "remove", *(["--force"] if force else []), str(wt), cwd=repo)
    else:
        # worktree directory vanished; clean up git's registration
        gitcmd.git("worktree", "prune", cwd=repo)
    s.meta_path(repo).unlink(missing_ok=True)
    return s
