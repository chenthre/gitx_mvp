"""git-native worktree locks: HIDDEN via sparse-checkout, RO via chmod,
commit-time guard via a pre-commit hook.

No FUSE, no sandbox, no mounts.  The session worktree IS the agent's
workspace (a plain directory); locks are expressed with mechanisms git
itself already has:

    HIDDEN  git sparse-checkout (non-cone, gitignore-style patterns).
            Hidden files are simply not materialized in the worktree —
            ls/cat/stat all get ENOENT, exactly like the file never
            existed there.  Patterns map 1:1 from the policy rules.

    RO      chmod (files 444, dirs 555).  Writes fail with "Permission
            denied" (same agent-visible UX as EROFS); reads and builds
            work fine.  Locks are stripped bluntly (restore the owner
            write bit on everything) and re-applied from the current
            policy; git's index remains the only state.  Locked dirs
            (555) also block delete/rename of their contents.

    guard   a pre-commit hook (core.hooksPath, worktree-scoped) that
            rejects any commit staging a locked path — including commits
            the agent makes with raw git.  Defense in depth: it also
            catches the known unix hole where a top-level RO file can
            be unlinked/replaced (the parent dir must stay writable).

Because there is no process/filesystem boundary, the threat model is
"structured workspace, not containment" — see README.  Locks bind the
agent's *workspace operations*; an agent deliberately walking out of the
worktree or digging through git history is out of scope (that is the
same risk class as running any agent unsandboxed today).
"""

import os
import stat as statmod
import subprocess
from pathlib import Path

from . import gitcmd
from .config import GITX_DIR
from .policy import HIDDEN, RO, RW

_HOOKS_DIR = GITX_DIR + "/hooks"

_PRE_COMMIT_HOOK = """#!/bin/sh
# gitx commit guard — active only in session worktrees (core.hooksPath is
# worktree-scoped).  Fails open if gitx itself cannot run, so a broken
# installation never bricks commits; the filesystem locks still apply.
if ! command -v gitx >/dev/null 2>&1; then
  echo "gitx guard: gitx not on PATH — skipping (fail-open)" >&2
  exit 0
fi
if ! gitx guard --pre-commit; then
  echo "gitx guard: commit rejected (locked file staged — see above)" >&2
  exit 1
fi
exit 0
"""


# ----------------------------------------------------------------------
# paths
# ----------------------------------------------------------------------
def hooks_dir(repo):
    return Path(repo) / _HOOKS_DIR


def worktree_git_dir(wt):
    out = gitcmd.git("-C", str(wt), "rev-parse", "--git-dir")
    p = Path(out.strip())
    if not p.is_absolute():
        p = Path(wt) / p
    return p.resolve()


def repo_root_from_worktree(wt):
    """Main repo root for a linked session worktree."""
    out = gitcmd.git("-C", str(wt), "rev-parse", "--git-common-dir")
    p = Path(out.strip())
    if not p.is_absolute():
        p = Path(wt) / p
    return p.resolve().parent


# ----------------------------------------------------------------------
# sparse-checkout (HIDDEN)
# ----------------------------------------------------------------------
def sparse_patterns(policy):
    """Translate the policy into non-cone sparse-checkout patterns.

    Sparse patterns are gitignore-style include-lists, so the mapping is
    direct: include everything, then exclude/re-include per rule in
    order (last match wins, same semantics as the policy resolver).

    Known gitignore limitation: re-including a subtree under an excluded
    directory does not work — hide/unlock at directory granularity
    instead (hide secrets; unlock secrets; hide secrets/private).
    """
    lines = ["/*"]
    for r in policy.rules:
        if r.mode == HIDDEN:
            lines.append("!" + r.pattern.lstrip("/"))
        else:
            lines.append(r.pattern.lstrip("/"))
    return lines


def _apply_sparse(wt, policy):
    # git sparse-checkout init sets core.sparseCheckout in the WORKTREE's
    # config (git enables extensions.worktreeConfig for this — a shared,
    # benign, git-native flag).  Main checkout is unaffected.
    rc, _, err = gitcmd.git_rc("sparse-checkout", "init", "--no-cone", cwd=wt)
    if rc != 0 and "already" not in err.lower():
        # already-initialized is fine; anything else is fatal
        if rc != 0:
            raise RuntimeError("git sparse-checkout init failed: %s" % err.strip())
    info = worktree_git_dir(wt) / "info"
    info.mkdir(parents=True, exist_ok=True)
    (info / "sparse-checkout").write_text("\n".join(sparse_patterns(policy)) + "\n")
    rc, out, err = gitcmd.git_rc("sparse-checkout", "reapply", cwd=wt)
    if rc != 0:
        raise RuntimeError("git sparse-checkout reapply failed: %s" % err.strip())


# ----------------------------------------------------------------------
# chmod (RO)
# ----------------------------------------------------------------------
def _tracked_files(wt):
    out = gitcmd.git("ls-files", "-z", cwd=wt)
    return [p for p in out.split("\0") if p]


def _walk_files(wt):
    """All file/dir paths in the worktree (tracked + untracked, no .git)."""
    for dirpath, dirnames, filenames in os.walk(wt):
        dirnames[:] = [d for d in dirnames if d != ".git"]
        for name in dirnames + filenames:
            yield os.path.relpath(os.path.join(dirpath, name), wt)


def _chmod_no_write(wt, rel, is_dir):
    full = os.path.join(wt, rel)
    if os.path.islink(full):
        return  # chmod follows symlinks — never touch them
    st = os.lstat(full)
    # keep read/exec bits, drop write bits: files 444(+x), dirs 555
    mode = st.st_mode & 0o555
    if is_dir:
        mode |= 0o111
    os.chmod(full, mode)


def _restore_all_write(wt):
    """Blunt unlock: give the owner write permission back on every entry.

    Deliberately not state-driven — an agent may have RENAMED a locked
    directory to a path that is outside the policy, and
    git (reset --hard / clean -fd / worktree remove) must still be able to
    unlink everything.  git's own index modes are the restore authority
    for content; apply() re-locks right after.
    """
    for dirpath, dirnames, filenames in os.walk(wt):
        dirnames[:] = [d for d in dirnames if d != ".git"]
        for name in dirnames + filenames:
            full = os.path.join(dirpath, name)
            if os.path.islink(full):
                continue
            try:
                st = os.lstat(full)
                if not (st.st_mode & statmod.S_IWUSR):
                    os.chmod(full, st.st_mode | statmod.S_IWUSR)
            except OSError:
                pass


# ----------------------------------------------------------------------
# hook (commit guard)
# ----------------------------------------------------------------------
def _install_hook(repo, wt):
    hd = hooks_dir(repo)
    hd.mkdir(parents=True, exist_ok=True)
    hook = hd / "pre-commit"
    hook.write_text(_PRE_COMMIT_HOOK)
    hook.chmod(0o755)
    # worktree-scoped hooks path (needs extensions.worktreeConfig, which
    # git sparse-checkout already enabled).  Main repo commits untouched.
    gitcmd.git("-C", str(wt), "config", "--worktree", "core.hooksPath", str(hd))


# ----------------------------------------------------------------------
# public API
# ----------------------------------------------------------------------
def apply(repo, sid, wt, policy):
    """Idempotently apply all locks to a session worktree.

    Order matters: restore write perms on everything first (so sparse
    materialization, git operations and re-locking never fight stale
    444/555 bits — including ones at paths the agent moved things to),
    then sparse, then chmod the current RO set.
    """
    repo, wt = Path(repo), Path(wt)
    _restore_all_write(wt)
    _apply_sparse(wt, policy)
    _install_hook(repo, wt)

    for rel in sorted(set(_tracked_files(wt)) | set(_walk_files(wt))):
        if policy.resolve(rel) != RO:
            continue
        full = wt / rel
        if not os.path.lexists(full):
            continue  # agent deleted it; git rollback will restore
        is_dir = full.is_dir() and not full.is_symlink()
        _chmod_no_write(wt, rel, is_dir)


def strip(repo, sid, wt):
    """Remove all chmod locks (sparse patterns left in place)."""
    _restore_all_write(wt)


def remove(repo, sid, wt):
    """Full cleanup for session teardown."""
    strip(repo, sid, wt)
    try:
        gitcmd.git_rc("-C", str(wt), "config", "--worktree", "--unset",
                      "core.hooksPath", cwd=wt)
    except Exception:
        pass


# ----------------------------------------------------------------------
# guard (used by the pre-commit hook and by checkpoint)
# ----------------------------------------------------------------------
def collect_violations(wt, policy, staged_only=True):
    """Paths that are locked (ro/hidden) yet modified/staged in `wt`."""
    wt = Path(wt)
    if staged_only:
        out = gitcmd.git("diff", "--cached", "--name-status", "-z", cwd=wt)
        paths = _parse_name_status(out)
    else:
        out = gitcmd.git("status", "--porcelain", "-z", cwd=wt)
        paths = _parse_porcelain(out)
    violations = []
    for rel in paths:
        m = policy.resolve(rel)
        if m in (RO, HIDDEN):
            violations.append((rel, m))
    return violations


def _parse_name_status(zout):
    """Parse `git diff --name-status -z` output into a path set."""
    toks = zout.split("\0")
    paths, i = set(), 0
    while i < len(toks):
        st = toks[i]
        if not st:
            i += 1
            continue
        paths.add(toks[i + 1])
        if st.startswith("R") or st.startswith("C"):
            paths.add(toks[i + 2])
            i += 3
        else:
            i += 2
    return paths


def _parse_porcelain(zout):
    """Parse `git status --porcelain -z` output into a path set."""
    toks = zout.split("\0")
    paths, i = set(), 0
    while i < len(toks):
        entry = toks[i]
        if not entry:
            i += 1
            continue
        paths.add(entry[3:])
        if entry[0] in "RC":
            paths.add(toks[i + 1])
            i += 2
        else:
            i += 1
    return paths
