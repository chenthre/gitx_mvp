"""gitx CLI.

Commands:

  gitx init                         create .gitx.toml (default: rw)
  gitx lock <path>...               visible, read-only
  gitx hide <path>...               invisible to the agent
  gitx unlock <path>...             visible, writable (override rule)
  gitx policy [--path P]            show rules / resolve one path
  gitx ls [PATH] [-R] [-v]          list entries with their lock states

  gitx session create [--base REV] [--name NAME]
  gitx session list / show <id>
  gitx run <id> [-- CMD...]         run an agent in the session worktree
  gitx status [id]                  git status of the session worktree
  gitx diff [id]                    git diff of the session worktree
  gitx checkpoint [id] -m MSG       git add -A + commit
  gitx rollback [id] [--base]       git reset --hard + clean -fd
  gitx finish <id> [--force]        drop worktree, keep branch for merge
"""

import argparse
import fcntl
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from . import __version__, config, gitcmd, locks, session as sessmod
from .policy import FORCED_HIDDEN_TOP, MODES, RW


def _repo(required=True):
    repo = config.repo_root()
    if repo is None and required:
        raise RuntimeError(
            "not inside a git repository — `git init` first, then `gitx init`")
    return repo


def _err(msg):
    print("gitx: error: %s" % msg, file=sys.stderr)


# ----------------------------------------------------------------------
# init / lock / hide / unlock / policy
# ----------------------------------------------------------------------
def cmd_init(args):
    repo = _repo()
    cfg = repo / config.CONFIG_NAME
    if cfg.exists():
        print("already initialized: %s (unchanged)" % cfg)
    else:
        cfg.write_text(
            config.CONFIG_HEADER
            + "\nversion = 1\ndefault = \"%s\"\n" % args.default)
        print("wrote %s (default = %s)" % (cfg, args.default))
    d = repo / config.GITX_DIR
    (d / "sessions").mkdir(parents=True, exist_ok=True)
    (d / "worktrees").mkdir(parents=True, exist_ok=True)
    # keep the main checkout clean: exclude .gitx/ locally
    excl = repo / ".git" / "info" / "exclude"
    try:
        excl.parent.mkdir(parents=True, exist_ok=True)
        existing = excl.read_text() if excl.exists() else ""
        if ".gitx/" not in existing:
            with excl.open("a") as f:
                if existing and not existing.endswith("\n"):
                    f.write("\n")
                f.write(".gitx/\n")
    except OSError:
        pass
    print("initialized gitx in %s" % repo)
    return 0


def _cmd_lock_generic(args, mode):
    repo = _repo()
    for p in args.paths:
        policy, norm = config.upsert_rule(repo, p, mode)
        print("%-6s %s" % (mode, norm))
    return 0


def cmd_lock(args):
    return _cmd_lock_generic(args, "ro")


def cmd_hide(args):
    return _cmd_lock_generic(args, "hidden")


def cmd_unlock(args):
    return _cmd_lock_generic(args, "rw")


def cmd_policy(args):
    repo = _repo()
    policy = config.load_policy(repo)
    print("default: %s" % policy.default)
    for r in policy.rules:
        print("%-6s %s" % (r.mode.upper(), r.pattern))
    print("%-6s (built-in, always)" % "HIDDEN .git")
    print("%-6s (built-in, always)" % "HIDDEN .gitx")
    if args.path:
        rel = args.path.strip("/")
        mode, idx = policy.resolve(rel, explain=True)
        origin = ("default" if idx is None else
                  "rule #%d: %s %s" % (idx, policy.rules[idx].mode,
                                       policy.rules[idx].pattern))
        print("resolve %s -> %s (%s)" % (rel, mode, origin))
    return 0


# ----------------------------------------------------------------------
# ls: per-entry lock states in a directory
# ----------------------------------------------------------------------
_MODE_COLOR = {"rw": "\033[32m", "ro": "\033[33m", "hidden": "\033[31m"}
_ANSI_RESET = "\033[0m"


def cmd_ls(args):
    """List directory entries with their resolved lock state.

    This is a user-side view (runs on the host), so HIDDEN entries are
    listed too — you see exactly what the agent won't.
    """
    repo = _repo()
    policy = config.load_policy(repo)
    if args.session:
        s = sessmod.load(repo, args.session)
        root = Path(s.wt(repo)).resolve()
        if not root.is_dir():
            raise RuntimeError("session worktree missing: %s" % root)
        base = root
    else:
        root = Path(repo).resolve()
        cwd = Path.cwd().resolve()
        base = cwd if (cwd == root or root in cwd.parents) else root

    target = (base / args.path).resolve() if args.path else base
    if target != root and root not in target.parents:
        raise ValueError("path %r is outside the %s"
                         % (args.path,
                            "session worktree" if args.session else "repository"))
    if not target.exists():
        raise FileNotFoundError("no such path: %s" % (args.path or "."))

    color = sys.stdout.isatty() and not os.environ.get("NO_COLOR")
    out = []

    def emit(full, display):
        rel = os.path.relpath(str(full), str(root))
        mode, idx = policy.resolve(rel, explain=True)
        line = "%-6s %s" % (mode, display)
        if args.verbose:
            top = rel.split("/", 1)[0] if rel else ""
            if top in FORCED_HIDDEN_TOP:
                origin = "built-in (always hidden)"
            elif idx is None:
                origin = "default"
            else:
                origin = "rule #%d: %s %s" % (idx, policy.rules[idx].mode,
                                              policy.rules[idx].pattern)
            line += "   <- %s" % origin
        if color:
            line = _MODE_COLOR[mode] + mode + _ANSI_RESET + line[len(mode):]
        out.append(line)

    if args.session:
        out.append("# session %s worktree: %s" % (s.id, root))

    if target.is_file():
        emit(target, os.path.relpath(str(target), str(root)))
    elif args.recursive:

        def walk(d):
            for name in sorted(os.listdir(d), key=str.lower):
                full = os.path.join(d, name)
                is_dir = os.path.isdir(full) and not os.path.islink(full)
                emit(Path(full),
                     os.path.relpath(full, str(target)) + ("/" if is_dir else ""))
                if is_dir and name not in FORCED_HIDDEN_TOP:
                    walk(full)

        walk(str(target))
    else:
        for name in sorted(os.listdir(target), key=str.lower):
            full = target / name
            is_dir = full.is_dir() and not full.is_symlink()
            emit(full, name + ("/" if is_dir else ""))

    print("\n".join(out) if out else "(empty)")
    return 0


# ----------------------------------------------------------------------
# session
# ----------------------------------------------------------------------
def cmd_session(args):
    repo = _repo()
    if args.session_cmd == "create":
        s = sessmod.create(repo, base=args.base, name=args.name)
        print("session %s" % s.id)
        print("  branch:   %s" % s.branch)
        print("  base:     %s (%s)" % (s.base_rev[:12], s.base))
        print("  worktree: %s" % s.wt(repo))
        print("  run:      gitx run %s -- <agent-command>" % s.id)
        return 0
    if args.session_cmd == "list":
        sessions = sessmod.list_sessions(repo)
        if not sessions:
            print("no sessions — `gitx session create`")
            return 0
        for s in sessions:
            print("%-24s branch=%-32s base=%s" % (s.id, s.branch, s.base_rev[:10]))
        return 0
    if args.session_cmd == "show":
        s = sessmod.load(repo, args.id)
        print(json.dumps(s.to_dict(), indent=2))
        return 0
    raise RuntimeError("unknown session subcommand")


# ----------------------------------------------------------------------
# run: apply locks + exec the agent in the session worktree
# ----------------------------------------------------------------------
class _SessionLock:
    """Advisory lock so two commands don't fight over one session."""

    def __init__(self, repo, s):
        self.path = s.lock_path(repo)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fh = None

    def __enter__(self):
        self.fh = open(self.path, "w")
        try:
            fcntl.flock(self.fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise RuntimeError(
                "session is busy (another gitx command is using it)")
        self.fh.write(str(os.getpid()))
        self.fh.flush()
        return self

    def __exit__(self, *exc):
        fcntl.flock(self.fh, fcntl.LOCK_UN)
        self.fh.close()
        try:
            self.path.unlink()
        except OSError:
            pass
        return False


def _repo_path(repo, s):
    wt = s.wt(repo)
    if not (wt / ".git").exists():
        raise RuntimeError(
            "session worktree %s is missing — it may have been pruned; "
            "remove the session (`gitx finish %s --force`) and create "
            "a new one" % (wt, s.id))
    return wt


def cmd_run(args):
    """Run an agent command with the session worktree as its cwd.

    That's the whole job: the worktree IS the workspace (a plain
    directory, no mounts, no sandbox), locks are already applied
    git-natively (sparse-checkout + chmod + commit guard), and the
    agent runs in its normal environment (HOME, PATH, auth, config).
    """
    repo = _repo()
    s = sessmod.resolve_sid(repo, args.session)
    wt = _repo_path(repo, s)
    policy = config.load_policy(repo)

    argv = list(getattr(args, "agent_cmd", None) or [])
    if argv and argv[0] == "--":
        argv = argv[1:]
    if not argv:
        argv = [os.environ.get("SHELL") or "/bin/bash"]

    # (re-)apply locks: also picks up policy changes made since the
    # last run and repairs any chmod the agent did to locked files
    locks.apply(repo, s.id, wt, policy)

    print("session  %s (branch %s)" % (s.id, s.branch))
    print("workspace %s (cwd for the agent)" % wt)
    print("policy   default=%s, %d rule(s)" % (policy.default, len(policy.rules)))
    print("---- agent output " + "-" * 40)
    sys.stdout.flush()

    with _SessionLock(repo, s):
        try:
            proc = subprocess.run(argv, cwd=str(wt))
            rc = proc.returncode
        except FileNotFoundError as e:
            raise RuntimeError("agent command not found: %s" % e)

    print(("---- agent exit code: %d " + "-" * 30) % rc)
    sys.stdout.flush()
    st = gitcmd.git("status", "--short", cwd=wt)
    if st.strip():
        print("changed files (git status --short):")
        print(st.rstrip())
    else:
        print("no changes in the session worktree")
    return rc


def cmd_guard(args):
    """Pre-commit guard — invoked by the session worktree's hook.

    Rejects commits that stage changes to ro/hidden paths.  Called from
    .gitx/hooks/pre-commit with cwd = session worktree (it therefore
    also guards commits the agent makes with raw git).
    """
    wt = Path.cwd()
    try:
        repo = locks.repo_root_from_worktree(wt)
        policy = config.load_policy(repo)
    except (RuntimeError, FileNotFoundError, ValueError) as e:
        print("gitx guard: cannot load policy (%s) — allowing commit" % e,
              file=sys.stderr)
        return 0  # fail-open: never brick commits on setup problems
    violations = locks.collect_violations(wt, policy, staged_only=True)
    if violations:
        print("gitx guard: locked path(s) staged for commit:", file=sys.stderr)
        for rel, mode in violations:
            print("  %-6s %s" % (mode, rel), file=sys.stderr)
        print("unlock the file (`gitx unlock <path>`), or discard changes "
              "(`gitx rollback`)", file=sys.stderr)
        return 1
    return 0


# ----------------------------------------------------------------------
# git-backed session commands
# ----------------------------------------------------------------------
def cmd_status(args):
    repo = _repo()
    s = sessmod.resolve_sid(repo, args.session)
    wt = _repo_path(repo, s)
    print("# branch %s @ %s" % (s.branch,
                                gitcmd.git("rev-parse", "--short", "HEAD", cwd=wt)))
    out = gitcmd.git("status", "--short", cwd=wt)
    print(out.rstrip() if out.strip() else "clean")
    # surface lock violations (tampering) on top of the raw status
    try:
        policy = config.load_policy(repo)
        violations = locks.collect_violations(wt, policy, staged_only=False)
        if violations:
            print("# LOCK VIOLATIONS (locked paths changed — see `gitx rollback`):")
            for rel, mode in violations:
                print("#   %-6s %s" % (mode, rel))
    except Exception:
        pass
    return 0


def cmd_diff(args):
    repo = _repo()
    s = sessmod.resolve_sid(repo, args.session)
    out = gitcmd.git("diff", "HEAD", cwd=_repo_path(repo, s))
    print(out.rstrip() if out.strip() else "(no changes)")
    return 0


def cmd_checkpoint(args):
    repo = _repo()
    s = sessmod.resolve_sid(repo, args.session)
    wt = _repo_path(repo, s)
    if not args.message:
        args.message = "agent checkpoint %s" % time.strftime("%Y-%m-%d %H:%M:%S")
    policy = config.load_policy(repo)
    with _SessionLock(repo, s):
        dirty = gitcmd.git("status", "--porcelain", cwd=wt)
        if not dirty.strip():
            print("nothing to checkpoint (worktree clean)")
            return 0
        violations = locks.collect_violations(wt, policy, staged_only=False)
        if violations:
            raise RuntimeError(
                "refusing to checkpoint: locked path(s) were changed:\n" +
                "\n".join("  %-6s %s" % v for v in violations) +
                "\ndiscard them with `gitx rollback %s`, or `gitx unlock <path>` "
                "to allow changes" % s.id)
        gitcmd.git("add", "-A", cwd=wt)
        gitcmd.git("commit", "-m", args.message, cwd=wt)
        print("checkpointed: %s" % args.message)
    return 0


def cmd_rollback(args):
    repo = _repo()
    s = sessmod.resolve_sid(repo, args.session)
    wt = _repo_path(repo, s)
    target = s.base_rev if args.base else "HEAD"
    policy = config.load_policy(repo)
    with _SessionLock(repo, s):
        # locks must be stripped first: reset --hard / clean need to
        # unlink the 444/555 files we chmodded (git would fail with
        # "unable to unlink ... Permission denied")
        locks.strip(repo, s.id, wt)
        gitcmd.git("reset", "--hard", target, cwd=wt)
        gitcmd.git("clean", "-fd", cwd=wt)
        locks.apply(repo, s.id, wt, policy)
        note = ("back to session base %s (%s)" % (s.base_rev[:12], s.base)
                if args.base else "back to last commit (HEAD)")
        print("rolled back %s — note: `reset --hard %s` moves branch %s"
              % (note, target, s.branch))
    return 0


def cmd_finish(args):
    repo = _repo()
    s = sessmod.resolve_sid(repo, args.session)
    with _SessionLock(repo, s):
        sessmod.finish(repo, s.id, force=args.force)
    print("session %s finished; branch %s kept" % (s.id, s.branch))
    print("review:   git diff %s...%s" % (s.base_rev, s.branch))
    print("merge:    git merge %s" % s.branch)
    return 0


# ----------------------------------------------------------------------
# arg parsing
# ----------------------------------------------------------------------
def build_parser():
    p = argparse.ArgumentParser(
        prog="gitx",
        description="Git worktree views with rw/ro/hidden file locks for agents")
    p.add_argument("--version", action="version", version="gitx %s" % __version__)
    sub = p.add_subparsers(dest="cmd", metavar="COMMAND", required=True)

    sp = sub.add_parser("init", help="initialize .gitx.toml in this repo")
    sp.add_argument("--default", choices=MODES, default=RW,
                    help="default mode for unlisted paths (default: rw)")
    sp.set_defaults(func=cmd_init)

    for name, func, help_ in (("lock", cmd_lock, "make paths read-only (RO)"),
                              ("hide", cmd_hide, "hide paths from the agent"),
                              ("unlock", cmd_unlock, "make paths writable again (RW)")):
        sp = sub.add_parser(name, help=help_)
        sp.add_argument("paths", nargs="+", metavar="PATH",
                        help="gitignore-like pattern, e.g. 'src/**' or '.env'")
        sp.set_defaults(func=func)

    sp = sub.add_parser("policy", help="show the effective policy")
    sp.add_argument("--path", metavar="P", help="also resolve one path")
    sp.set_defaults(func=cmd_policy)

    sp = sub.add_parser("ls", help="list a directory with lock states (rw/ro/hidden)")
    sp.add_argument("path", nargs="?", default=None,
                    help="directory or file to inspect (default: current dir)")
    sp.add_argument("-R", "--recursive", action="store_true",
                    help="recurse into subdirectories")
    sp.add_argument("-v", "--verbose", action="store_true",
                    help="show which rule matched each path")
    sp.add_argument("--session", metavar="ID",
                    help="list the session's worktree instead of this checkout")
    sp.set_defaults(func=cmd_ls)

    sp = sub.add_parser("session", help="session management")
    ssub = sp.add_subparsers(dest="session_cmd", metavar="SUB", required=True)
    spc = ssub.add_parser("create", help="create a session worktree")
    spc.add_argument("--base", default="HEAD", help="base commit-ish (default HEAD)")
    spc.add_argument("--name", help="session name (default: auto id)")
    ssub.add_parser("list", help="list sessions")
    sps = ssub.add_parser("show", help="show session metadata")
    sps.add_argument("id")
    sp.set_defaults(func=cmd_session)

    sp = sub.add_parser("run", help="run an agent command in the session worktree")
    sp.add_argument("session", nargs="?", help="session id (default: the only one)")
    sp.set_defaults(func=cmd_run)

    for name, func, help_ in (
            ("status", cmd_status, "git status of the session worktree"),
            ("diff", cmd_diff, "git diff of the session worktree")):
        sp = sub.add_parser(name, help=help_)
        sp.add_argument("session", nargs="?", help="session id (default: the only one)")
        sp.set_defaults(func=func)

    sp = sub.add_parser("checkpoint", help="commit all changes (git add -A + commit)")
    sp.add_argument("session", nargs="?", help="session id (default: the only one)")
    sp.add_argument("-m", "--message", help="commit message")
    sp.set_defaults(func=cmd_checkpoint)

    sp = sub.add_parser("rollback", help="discard changes (git reset --hard + clean)")
    sp.add_argument("session", nargs="?", help="session id (default: the only one)")
    sp.add_argument("--base", action="store_true",
                    help="roll all the way back to the session base commit")
    sp.set_defaults(func=cmd_rollback)

    sp = sub.add_parser("finish", help="remove worktree, keep branch for merging")
    sp.add_argument("session", help="session id")
    sp.add_argument("--force", action="store_true", help="discard uncommitted changes")
    sp.set_defaults(func=cmd_finish)

    sp = sub.add_parser("guard", help="internal: pre-commit lock guard (hook entry)")
    sp.add_argument("--pre-commit", action="store_true", help=argparse.SUPPRESS)
    sp.set_defaults(func=cmd_guard)

    return p


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    # split the agent command off at the first "--" so options can appear
    # in any position before it (argparse REMAINDER would swallow them)
    agent_cmd = []
    if "--" in argv:
        idx = argv.index("--")
        agent_cmd, argv = argv[idx + 1:], argv[:idx]
    parser = build_parser()
    args = parser.parse_args(argv)
    args.agent_cmd = agent_cmd
    try:
        return args.func(args)
    except RuntimeError as e:
        _err(str(e))
        return 1
    except (ValueError, FileNotFoundError) as e:
        _err(str(e))
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
