"""Load / save the project policy file ``.gitx.toml``.

Uses stdlib ``tomllib`` on Python 3.11+; on older Pythons falls back to a
minimal parser that understands the exact (tiny) subset of TOML this tool
writes: top-level ``key = "value"`` / ``key = 1`` lines and ``[[rule]]``
tables with ``path`` / ``mode`` string members.  Comments and blank lines
are allowed.
"""

import json
import os
from pathlib import Path

try:
    import tomllib  # Python 3.11+
except ImportError:  # pragma: no cover
    tomllib = None

from .policy import MODES, Policy, RW

CONFIG_NAME = ".gitx.toml"
GITX_DIR = ".gitx"

CONFIG_HEADER = (
    "# gitx policy file.\n"
    "# Modes: rw = visible+writable, ro = visible+read-only, hidden = invisible.\n"
    "# Patterns are gitignore-like; last matching rule wins.\n"
)


def repo_root(start=None):
    """Walk up from ``start`` (default: cwd) to find the git repo root."""
    d = Path(start or os.getcwd()).resolve()
    while True:
        if (d / ".git").exists():
            return d
        if d.parent == d:
            return None
        d = d.parent


def _mini_toml(text):
    data = {}
    rules = []
    cur = data
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[["):
            if line.replace(" ", "") != "[[rule]]":
                raise ValueError("unsupported table header: %s" % line)
            cur = {}
            rules.append(cur)
            continue
        if line.startswith("["):
            raise ValueError("unsupported table header: %s" % line)
        m = _kv_string(line) or _kv_int(line)
        if not m:
            raise ValueError("cannot parse config line: %s" % raw)
        key, value = m
        cur[key] = value
    data["rule"] = rules
    return data


def _kv_string(line):
    # key = "value"   (optional trailing comment)
    idx = line.find("=")
    if idx == -1:
        return None
    key = line[:idx].strip()
    rest = line[idx + 1 :].strip()
    if not rest.startswith('"'):
        return None
    end = rest.find('"', 1)
    if end == -1:
        return None
    value = rest[1:end]
    tail = rest[end + 1 :].strip()
    if tail and not tail.startswith("#"):
        return None
    return key, value


def _kv_int(line):
    idx = line.find("=")
    if idx == -1:
        return None
    key = line[:idx].strip()
    rest = line[idx + 1 :].strip()
    if not rest.isdigit():
        return None
    return key, int(rest)


def load_policy(repo):
    """Load the Policy from <repo>/.gitx.toml (raises if missing/bad)."""
    path = Path(repo) / CONFIG_NAME
    if not path.exists():
        raise FileNotFoundError(
            "%s not found — run `gitx init` in the repo first" % CONFIG_NAME)
    text = path.read_text(encoding="utf-8")
    if tomllib is not None:
        data = tomllib.loads(text)
    else:
        data = _mini_toml(text)
    version = data.get("version", 1)
    if version != 1:
        raise ValueError("unsupported config version %r (expected 1)" % (version,))
    default = data.get("default", RW)
    if default not in MODES:
        raise ValueError("invalid default mode %r" % (default,))
    rules = []
    for i, r in enumerate(data.get("rule", [])):
        if "path" not in r or "mode" not in r:
            raise ValueError("rule #%d missing path/mode" % i)
        rules.append(r)
    return Policy.from_dict({"default": default, "rules": rules})


def save_policy(repo, policy):
    path = Path(repo) / CONFIG_NAME
    lines = [CONFIG_HEADER.rstrip("\n"), "version = 1", 'default = %s' % json.dumps(policy.default), ""]
    for r in policy.rules:
        lines.append("[[rule]]")
        lines.append("path = %s" % json.dumps(r.pattern))
        lines.append("mode = %s" % json.dumps(r.mode))
        lines.append("")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def upsert_rule(repo, pattern, mode):
    """Add or update a rule (same normalized pattern -> update in place)."""
    from .policy import normalize_pattern, Rule

    pattern = normalize_pattern(pattern)
    policy = load_policy(repo)
    for r in policy.rules:
        if r.pattern == pattern:
            r.mode = mode
            break
    else:
        policy.rules.append(Rule(pattern, mode))
    save_policy(repo, policy)
    return policy, pattern
