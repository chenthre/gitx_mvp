#!/bin/bash
# A realistic mini "agent session": a scripted agent explores the repo,
# fixes a bug, tries to touch files it shouldn't (and gets blocked),
# adapts, and the user reviews/merges with plain git.
#
# Usage: bash examples/demo-agent.sh

set -e
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}"
AG="python3 -m gitx"

T="$(mktemp -d /tmp/gitx-demo.XXXXXX)"
echo "## demo repo: $T"
cd "$T"
git init -q demo && cd demo
git config user.email demo@local && git config user.name demo

# --- the "product": a tiny python app -----------------------------------
mkdir -p app tests generated
cat > app/calc.py <<'EOF'
def add(a, b):
    return a - b   # BUG: should be +

def mul(a, b):
    return a * b
EOF
cat > tests/test_calc.py <<'EOF'
from app.calc import add, mul

def test_add():
    assert add(2, 3) == 5

def test_mul():
    assert mul(2, 3) == 6
EOF
echo "# generated at build time; do not edit" > generated/manifest.txt
cat > run.sh <<'EOF'
#!/bin/sh
exec python3 -m pytest tests -q
EOF
chmod +x run.sh
echo "S3_SECRET=super-secret-credential" > .env
git add -A && git commit -qm "initial project"

# --- user locks what the agent must not touch ---------------------------
$AG init > /dev/null
$AG lock generated 'package-lock.json' > /dev/null 2>&1 || true
$AG hide .env > /dev/null
echo "## policy:"; $AG policy

# --- start a session and run the "agent" --------------------------------
$AG session create --name fixer > /dev/null
echo
echo "## agent runs (scripted mini-agent, see the script body)"
$AG run fixer -- /bin/bash <<'AGENT'
set -e
export PYTHONDONTWRITEBYTECODE=1   # same-second edits vs stale .pyc
echo "[agent] explore: ls + read the bug"
ls
cat app/calc.py
echo
echo "[agent] run the tests (they fail — the bug)"
python3 -m pytest tests -q 2>&1 | tail -2 || true
echo
echo "[agent] fix the bug in app/calc.py (RW => allowed)"
sed -i 's/return a - b/return a + b/' app/calc.py
python3 -m pytest tests -q | tail -1
echo
echo "[agent] try to 'fix' the build manifest too (RO => blocked)"
if sed -i 's/do not edit/EDITED/' generated/manifest.txt 2>/dev/null; then
    echo "  !!! was able to edit a RO file"; exit 1
else
    echo "[agent] blocked: Permission denied -> fine, leave it alone"
fi
echo
echo "[agent] try to read .env (HIDDEN => does not even see it)"
ls -a | grep -q '^.env$' && { echo "  !!! .env visible"; exit 1; } || echo "[agent] .env does not exist for me"
echo
echo "[agent] add a feature file (RW => allowed)"
echo 'def sub(a, b): return a - b' >> app/calc.py
echo "[agent] done"
AGENT

echo
echo "## what the user sees — live, no sync step, plain git:"
$AG status
echo
$AG diff | head -20
echo
echo "## checkpoint + review + merge, all plain git:"
$AG checkpoint fixer -m "agent: fix add(), add sub()" | head -1
$AG finish fixer | head -2
git merge -q agent/fixer
python3 -m pytest tests -q | tail -1
echo
echo "## merged. agent never saw .env, never touched generated/."
echo "## demo repo kept at: $T/demo"
