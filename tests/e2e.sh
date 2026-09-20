#!/bin/bash
# End-to-end test matrix for gitx (guide §14, git-native architecture).
#
# Architecture under test (no FUSE, no sandbox, no mounts):
#   session worktree = agent workspace
#   HIDDEN -> git sparse-checkout   (files not materialized)
#   RO     -> chmod 444/555         (writes: Permission denied)
#   guard  -> pre-commit hook       (locked paths never commit)

set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}"
AG="python3 -m gitx"

PASS=0
FAIL=0
FAILED_NAMES=()

ok()   { PASS=$((PASS+1)); echo "  ok   - $1"; }
bad()  { FAIL=$((FAIL+1)); FAILED_NAMES+=("$1"); echo "  FAIL - $1"; }

# check NAME SCRIPT [RUNOPTS...] — run inside the session worktree
check() {
    local name="$1" script="$2"; shift 2
    if $AG run "$SID" "$@" -- /bin/bash -c "$script" >/dev/null 2>&1; then ok "$name"; else bad "$name"; fi
}
check_fail() {
    local name="$1" script="$2"; shift 2
    if $AG run "$SID" "$@" -- /bin/bash -c "$script" >/dev/null 2>&1; then bad "$name"; else ok "$name"; fi
}

echo "== setup =="
T="$(mktemp -d /tmp/gitx-e2e.XXXXXX)"
trap 'chmod -R u+w "$T" 2>/dev/null; rm -rf "$T"' EXIT
cd "$T"
git init -q repo && cd repo
git config user.email t@t.local && git config user.name tester
mkdir -p src tests generated secrets
echo 'export const a = 1;' > src/a.ts
echo 'test 1' > tests/t.txt
echo 'generated data' > generated/out.txt
echo 'locked deps' > package-lock.json
echo 'SECRET=abc' > .env
echo 'token' > secrets/token
git add -A && git commit -qm "init"
$AG init >/dev/null || { echo "init failed"; exit 1; }
$AG lock package-lock.json 'generated/**' >/dev/null
$AG hide .env 'secrets/**' >/dev/null
$AG session create --name t1 >/dev/null
SID=t1
WT="$T/repo/.gitx/worktrees/$SID"
echo "repo: $T/repo  session: $SID"
echo

# --------------------------------------------------------------------------
echo "== 1. RW: read/write/create/delete/rename/mkdir (guide 14.1) =="
check "RW read existing file" 'cat src/a.ts | grep -q "a = 1"'
check "RW modify existing file" 'echo "// touched" >> src/a.ts && grep -q touched src/a.ts'
check "RW create file" 'echo new > src/new.ts && test -f src/new.ts'
check "RW delete file" 'rm src/new.ts && ! test -e src/new.ts'
check "RW rename file" 'mv src/a.ts src/a2.ts && mv src/a2.ts src/a.ts'
check "RW mkdir/rmdir" 'mkdir src/newdir && rmdir src/newdir'
check "RW python build-style read" 'python3 -c "print(open(\"src/a.ts\").read())" | grep -q "a = 1"'
check "agent keeps its normal environment" 'test "$HOME" != "" && python3 --version >/dev/null && git --version >/dev/null'

echo "== 2. live git projection =="
if ! git -C "$WT" diff --quiet; then ok "agent write visible in git diff immediately"; else bad "agent write visible in git diff immediately"; fi
git -C "$WT" diff | grep -q "touched" && ok "git diff content matches agent change" || bad "git diff content matches agent change"

echo "== 3. RO: reads fine, writes fail (guide 14.2) =="
check "RO read" 'cat package-lock.json | grep -q "locked deps"'
check "RO stat works" 'test -f package-lock.json && stat package-lock.json >/dev/null'
check "RO usable as build input" 'python3 -c "print(open(\"generated/out.txt\").read())" | grep -q generated'
check_fail "RO write" 'echo x > package-lock.json'
check_fail "RO append" 'echo x >> package-lock.json'
check_fail "RO truncate" 'truncate -s 0 package-lock.json'
check_fail "RO file unlink in locked dir" 'rm generated/out.txt'
check_fail "RO rm -rf locked dir" 'rm -rf generated'
check_fail "RO create inside locked dir" 'echo x > generated/new.txt'

# known unix holes (same uid): chmod-u+w, unlink/rename of TOP-LEVEL locked
# entries (their parent — the worktree root — must stay writable).  The
# safety net: violations stay git-visible, the commit guard rejects them,
# and rollback restores.  Nested locked dirs are fully protected.
echo "== 3b. unix holes: git-visible + guarded + rollback =="
check "chmod-u+w is possible (documented hole)" 'chmod u+w package-lock.json'
check "next gitx run re-locks (tamper repair)" 'test ! -w package-lock.json'
check "top-level RO unlink is possible (documented hole)" 'rm package-lock.json'
if $AG status $SID | grep -q "package-lock.json"; then ok "status flags the deletion as violation"; else bad "status flags the deletion as violation"; fi
check "guard rejects commit of the deleted RO file" 'git add -A 2>/dev/null; git commit -m nope 2>&1 | grep -q "gitx guard"'
$AG rollback $SID >/dev/null
check "rollback restores deleted RO file + lock" 'test -f package-lock.json && ! test -w package-lock.json && grep -q "locked deps" package-lock.json'
check "locked dir rename is possible (documented hole)" 'mv generated generated2 && test -d generated2'
$AG rollback $SID >/dev/null
check "rollback cleans up the moved dir and re-locks" 'test -d generated && ! test -d generated2 && ! test -w generated/out.txt'

echo "== 4. HIDDEN: not on disk at all (guide 14.3) =="
check "HIDDEN absent from readdir" '! ls -a . | grep -qx ".env" && ! ls -a . | grep -qx secrets'
check "HIDDEN stat ENOENT" '! test -e .env'
check_fail "HIDDEN open" 'cat .env'
check_fail "HIDDEN list dir" 'ls secrets'
check_fail "HIDDEN read via dir" 'cat secrets/token'
# agent-created NEW file at a hidden path: allowed (it is not the secret);
# the real hidden content stays unreadable and git keeps ignoring the path
check "creating a file at a hidden path is not the secret" 'echo agentfile > .env && ! grep -q SECRET .env'
rm -f "$WT/.env"   # not a tracked state — just tidy up for the status test
if git -C "$WT" status --porcelain | grep -qE "\.env|secrets"; then bad "HIDDEN absent from git status"; else ok "HIDDEN absent from git status"; fi
check "agent can still use git (status/diff/log)" 'git status --short >/dev/null && git log --oneline -1 >/dev/null'

echo "== 5. commit guard (defense in depth) =="
check "tamper + raw git commit rejected" 'chmod u+w package-lock.json; echo t > package-lock.json; git add package-lock.json; git commit -m x 2>&1 | grep -q "gitx guard"'
git -C "$WT" reset -q HEAD 2>/dev/null
$AG rollback $SID >/dev/null
if $AG checkpoint $SID -m cp 2>&1 | grep -q "refusing"; then bad "checkpoint refuses only on violations (none here)"; else ok "checkpoint refuses only on violations (none here)"; fi

echo "== 6. git lifecycle (guide 14.4) =="
$AG run $SID -- /bin/bash -c 'echo "// more" >> src/a.ts' >/dev/null 2>&1
$AG checkpoint $SID -m "cp1" >/dev/null && ok "checkpoint commits agent changes" || bad "checkpoint commits agent changes"
git -C "$WT" log --oneline -1 | grep -q cp1 && ok "checkpoint is a plain git commit" || bad "checkpoint is a plain git commit"
check "agent can keep working after checkpoint" 'echo more >> src/a.ts'
$AG rollback $SID >/dev/null && ok "rollback to HEAD" || bad "rollback to HEAD"
st=$(git -C "$WT" status --porcelain)
[ -z "$st" ] && ok "worktree clean after rollback" || bad "worktree clean after rollback ($st)"
$AG rollback $SID --base >/dev/null && ok "rollback --base" || bad "rollback --base"
[ "$(git -C "$WT" rev-parse HEAD)" = "$(git rev-parse HEAD)" ] \
    && ok "rollback --base returns to session base" \
    || bad "rollback --base returns to session base"
DIRTY=$(git status --porcelain | grep -v '^?? .gitx.toml$')
[ -z "$DIRTY" ] && ok "main checkout stays clean" || bad "main checkout stays clean ($DIRTY)"

$AG run $SID -- /bin/bash -c 'echo "final change" > src/final.ts' >/dev/null 2>&1
$AG checkpoint $SID -m "cp2" >/dev/null
$AG finish $SID >/dev/null && ok "finish removes worktree, keeps branch" || bad "finish removes worktree, keeps branch"
git branch --list "agent/$SID" | grep -q . && ok "branch agent/$SID kept" || bad "branch agent/$SID kept"
git diff HEAD...agent/$SID --quiet && bad "branch carries agent commits" || ok "branch carries agent commits"
git merge -q agent/$SID >/dev/null 2>&1 && ok "plain git merge works" || bad "plain git merge works"
git show HEAD:src/final.ts | grep -q "final change" && ok "merged content intact" || bad "merged content intact"

echo "== 7. main checkout isolation =="
mainfiles="$(ls | tr '\n' ' ')"
git read-tree -mu HEAD 2>/dev/null
[ "$(ls | tr '\n' ' ')" = "$mainfiles" ] && ok "main checkout unaffected by session sparse config" \
    || bad "main checkout unaffected by session sparse config"
git config core.hooksPath >/dev/null 2>&1 && bad "main repo hooks untouched" || ok "main repo hooks untouched"

echo "== 8. misc =="
if $AG policy --path src/a.ts | grep -q "src/a.ts -> rw"; then ok "policy --path resolves rw"; else bad "policy --path resolves rw"; fi
if $AG policy --path secrets/token | grep -q "secrets/token -> hidden"; then ok "policy --path resolves hidden"; else bad "policy --path resolves hidden"; fi

# ls: per-entry lock states (user-side view of the worktree)
$AG session create --name t2 >/dev/null
if $AG ls --session t2 | grep -qE '^ro +package-lock\.json$'; then ok "ls --session shows RO entries"; else bad "ls --session shows RO entries"; fi
if $AG ls --session t2 | grep -qE '^hidden +\.env$'; then ok "ls --session shows HIDDEN entries"; else bad "ls --session shows HIDDEN entries"; fi
$AG status >/dev/null 2>&1 && ok "auto-select single session" || bad "auto-select single session"
$AG finish t2 >/dev/null

echo
echo "======================================"
echo "PASS: $PASS  FAIL: $FAIL"
if [ "$FAIL" -gt 0 ]; then
    printf 'failed: %s\n' "${FAILED_NAMES[@]}"
    exit 1
fi
echo "ALL TESTS PASSED"
