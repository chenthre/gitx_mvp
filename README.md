# gitx — controlled RW/RO/HIDDEN Git worktree views for coding agents

> **In one sentence**: use **git's own mechanisms** to give agents a
> workspace with `RW / RO / HIDDEN` file locks. Everything the agent
> legitimately changes is under git version control in real time —
> diff / checkpoint / rollback are plain git, with **no mounts, no
> sandbox, and no second copy of state**.

[中文版 README](README_cn.md)

## Why

When you let a coding agent (pi, claude, codex, aider) loose on a
repository, you usually want to constrain what it can see and touch:

```text
repo/
├── src/                 agent may modify
├── tests/               agent may modify
├── generated/           needed at runtime, but must not be modified
├── package-lock.json    needed at runtime, but must not be modified
├── .env                 agent must not see
└── secrets/             agent must not see
```

Simply deleting files breaks the runtime environment; merely asking the
agent not to touch them isn't a real constraint. gitx gives you three
understandable locks instead — **rw / ro / hidden** — expressed entirely
with git's own primitives.

## Architecture (git-native, zero mounts)

```
your repo (normal checkout, untouched)
└── .gitx/worktrees/<id>/        ← a git worktree; literally the agent's cwd
     │
     ├── HIDDEN  git sparse-checkout   files never materialize: ls/cat/stat → ENOENT
     ├── RO      chmod 444/555          writes fail with Permission denied;
     │                                   nested dirs block even delete/rename
     ├── guard   pre-commit hook        changes to locked paths never enter a
     │                                   commit (even via raw git by the agent)
     └── the rest  RW                   normal writes, live in git status/diff
```

| Lock | Mechanism (all existing tools) | What the agent sees |
|---|---|---|
| `rw` | none | normal reads/writes, live in git |
| `ro` | `chmod` (files 444, dirs 555) | `Permission denied` (same UX as EROFS); reading/building works |
| `hidden` | `git sparse-checkout` (non-cone, gitignore-style patterns) | file does not exist (`ls` can't see it, `cat` → ENOENT) |

Version management is 100% git: `status` = git status, `checkpoint` =
git add + commit, `rollback` = git reset + clean, and after `finish` you
merge the branch with a plain `git merge`.

## Quick start

Requirements (Linux): `git` ≥ 2.25 (sparse-checkout), Python ≥ 3.8.
**No fusepy / bwrap / root needed.** `pip3 install -e .` (editable
install needs setuptools ≥ 64; otherwise use the zero-install wrapper
`bin/gitx`).

```bash
cd your-repo
gitx init                          # creates .gitx.toml (default = "rw")

gitx lock package-lock.json        # RO
gitx lock 'generated/**'           # RO — a whole subtree
gitx hide .env                     # HIDDEN
gitx hide 'secrets/**'

gitx ls                            # show each file's lock state
gitx session create                # git worktree add -b agent/<id>
gitx run <id> -- pi                # ← that's it (see below)
```

## Running a real agent CLI

```bash
gitx run <id> -- pi                # or claude, codex, aider, any command/script
gitx run <id> -- pi -p "fix the failing test"
```

The agent's cwd is the session worktree; **everything else (HOME, PATH,
config, credentials) is your own environment** — since there are no
mounts/sandboxes, there's nothing to "recreate". `gitx run` only: applies
the locks (idempotent — it also repairs chmod tampering the agent did on
locked files) → execs the agent with the worktree as cwd.

The agent can also use git directly (status/diff/log/commit) — its
commits pass through the same pre-commit guard.

## Session lifecycle (all plain git)

```bash
gitx session create [--base <rev>] [--name <name>]
gitx run <id> -- pi "fix the failing test"

gitx status <id>               # git status + lock-violation warnings
gitx diff <id>                 # git diff HEAD
gitx checkpoint <id> -m "msg"  # git add -A && git commit (refuses violations)
gitx rollback <id>             # git reset --hard HEAD && git clean -fd
gitx rollback <id> --base      # back to the session's starting commit
gitx finish <id>               # git worktree remove; branch agent/<id> kept

git diff main...agent/<id> && git merge agent/<id>
```

## Inspecting locks: `gitx ls`

A user-side view (runs on the host); HIDDEN files are listed too — you
see exactly what the agent can't:

```
$ gitx ls                        # or gitx ls -v / -R src / --session t1
hidden .env        <- rule #2: hidden .env
hidden .git/       <- built-in (always hidden)
ro     generated/  <- rule #1: ro generated/**
rw     src/        <- default
```

`gitx policy [--path P]` shows the rules themselves and resolves a single
path.

## Policy semantics (pattern rules)

Rules live in `.gitx.toml` (commit it — it's your project's permission
policy). gitignore-style, **last matching rule wins**; `gitx unlock X`
appends an overriding `mode="rw"` rule.

- A pattern without `/` matches at any depth (`.env` also matches
  `sub/.env`); a leading `/` anchors at the repo root.
- `*` matches within one segment, `?` one character, `**` spans
  segments; a rule matches the path and everything below it.
- `.git` and `.gitx` are built-in HIDDEN and cannot be overridden.

**A known sparse-checkout limitation**: hiding a directory and then
unlocking its subtree doesn't take effect (gitignore's directory-pruning
semantics). Work at directory granularity: `hide secrets` →
`unlock secrets` → `hide secrets/private`.

## Threat model (honest statement)

There is no process/filesystem boundary; locks constrain **normal file
operations inside the workspace** — "structured constraints", not "hard
containment":

- **Guaranteed**: HIDDEN files are never materialized (ordinary tools
  can't read them); RO writes fail (nested dirs block even
  unlink/rename); changes to locked paths enter no commit (double
  guard: `gitx checkpoint` + pre-commit hook); every violation is
  visible in `gitx status`; rollback always restores.
- **Unix leak surface (unavoidable under the same uid)**: an agent can
  `chmod u+w` a RO file, and can unlink/rename a **top-level** RO file
  (the parent dir must stay writable) — but all of that is
  git-visible, guard-intercepted, and rollback-able; nested RO dirs are
  fully protected. Every session run re-applies (repairs) permissions.
- **Not guaranteed**: an agent deliberately walking outside the worktree
  (`cat ../.env` to read the main checkout, or anything in your home) or
  digging through git history (`git show HEAD:.env`) — that's the same
  risk class as running any agent unsandboxed in your terminal today.
  Secrets shouldn't live in a git repo in the first place; hard
  containment is a sandbox problem, not a git problem (the v0.2
  FUSE+bwrap design was validated and then superseded by this version).
- HIDDEN file *names* remain visible in `git ls-files` (reading their
  content requires a deliberate `git show`); files an agent *creates*
  under hidden paths are its own files (not your secrets), and git
  ignores those paths.

## Code structure (deliberately small)

```
gitx/
├── cli.py      # command dispatch + run/guard/status/checkpoint/rollback
├── policy.py   # RW/RO/HIDDEN + gitignore-style patterns (pure logic, unit-tested)
├── config.py   # .gitx.toml read/write (tomllib on 3.11+, minimal fallback parser)
├── gitcmd.py   # thin git CLI wrapper
├── locks.py    # the three git-native lock mechanisms: sparse / chmod / hook
└── session.py  # git worktree + session metadata + flock
```

## Tests

```bash
python3 -m unittest tests.test_policy   # policy matching unit tests
bash tests/e2e.sh                       # 57-case matrix: RW/RO/HIDDEN invariants,
                                        #   leak-surface visibility + guard + recovery,
                                        #   git lifecycle, main-checkout isolation
bash examples/demo-agent.sh             # end-to-end demo: fix bug → blocked by lock → adapt → merge
bash examples/run-pi.sh <repo> -- -p "…"  # real pi (needs pi + auth)
```

## Known limitations / next steps

The full design document — including the superseded v0.2 FUSE approach,
the experiment's goals, and failure-mode thinking — lives in
[`docs/design-guide.md`](docs/design-guide.md) (§15/§16 list the questions
the experiment still needs data on). Worth collecting: which locks are
actually useful? How do agents react to `Permission denied`? Does HIDDEN
hurt task completion? Is directory- vs file-level locking the right
granularity?

## License

[MIT](LICENSE)