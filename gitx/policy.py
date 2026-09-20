"""Path policy: resolve repo-relative paths to RW / RO / HIDDEN.

Matching semantics (gitignore-inspired, deliberately simplified):

- Modes: ``rw`` (visible, writable), ``ro`` (visible, read-only),
  ``hidden`` (invisible: ENOENT).
- A pattern without ``/`` matches at any depth (like .gitignore),
  e.g. ``.env`` matches ``.env`` and ``sub/.env``.
- A pattern containing ``/`` is anchored at the repo root.
- ``*`` matches within one segment, ``?`` a single char, ``**`` spans
  segments.  A trailing ``/**`` also matches the directory itself.
- A rule matches the named path AND everything below it, so
  ``lock generated/**`` (or just ``lock generated``) locks the whole
  subtree, including deleting/renaming the directory.
- **Last matching rule wins** — later rules override earlier ones
  (this is how ``unlock`` works).
- ``.git`` and ``.gitx`` are always HIDDEN and cannot be overridden.
"""

import re

RW = "rw"
RO = "ro"
HIDDEN = "hidden"

MODES = (RW, RO, HIDDEN)

# Never visible to the agent, not overridable by config rules.
FORCED_HIDDEN_TOP = (".git", ".gitx")

_GLOBSTAR = "\x00GS\x00"


def _segment_regex(seg):
    """Translate one path segment (no '/' inside) to a regex fragment."""
    out = []
    i = 0
    n = len(seg)
    while i < n:
        c = seg[i]
        if c == "*":
            out.append("[^/]*")
            i += 1
        elif c == "?":
            out.append("[^/]")
            i += 1
        elif c == "[":
            j = seg.find("]", i + 1)
            if j == -1:
                out.append(re.escape(c))
                i += 1
                continue
            inner = seg[i + 1 : j]
            if inner.startswith("!"):
                inner = "^" + inner[1:]
            # keep ranges like a-z working, escape backslashes
            inner = inner.replace("\\", "\\\\")
            out.append("[" + inner + "]")
            i = j + 1
            continue
        else:
            out.append(re.escape(c))
            i += 1
    return "".join(out)


def compile_pattern(pattern):
    """Compile a policy pattern to a regex that fullmatches repo-relative
    paths (the path itself and anything below it)."""
    p = normalize_pattern(pattern)
    if p in ("", "*", "**"):
        return re.compile(r".*")
    anchored = p.startswith("/")  # '/secrets' = repo-root only
    p = p.lstrip("/")
    # gitignore-style: a pattern without '/' matches at any depth
    if not anchored and "/" not in p:
        p = "**/" + p
    segs = p.split("/")
    # trailing '**' is covered by the descendant suffix
    if segs[-1] == "**":
        segs = segs[:-1]
        if not segs:
            return re.compile(r".*")
    leading_globstar = segs[0] == "**"
    if leading_globstar:
        segs = segs[1:]
        if not segs:
            return re.compile(r".*")
    parts = [_GLOBSTAR if s == "**" else _segment_regex(s) for s in segs]
    joined = "/".join(parts)
    # 'a/**/b'  ->  a/(?:[^/]+/)*b   (zero or more intermediate segments)
    joined = joined.replace(_GLOBSTAR + "/", "(?:[^/]+/)*")
    # a leftover bare marker (e.g. 'a/**') -> optional descendant chain
    joined = joined.replace("/" + _GLOBSTAR, "(?:/(?:[^/]+/)*[^/]*)?")
    joined = joined.replace(_GLOBSTAR, "(?:[^/]+/)*")
    if leading_globstar:
        joined = "(?:[^/]+/)*" + joined
    return re.compile("(?:%s)(?:/.*)?" % joined)


def normalize_pattern(pattern):
    """Normalize a pattern; raises ValueError on clearly-bad input.

    A leading '/' is kept: it anchors the pattern at the repo root
    (gitignore-style); patterns without '/' match at any depth.
    """
    if not isinstance(pattern, str):
        raise ValueError("pattern must be a string")
    p = pattern.strip().rstrip("/")
    while "//" in p:
        p = p.replace("//", "/")
    if not p or p == "/":
        raise ValueError("empty pattern")
    for seg in p.strip("/").split("/"):
        if seg == "..":
            raise ValueError("pattern must not contain '..': %r" % pattern)
    return p


class Rule:
    __slots__ = ("pattern", "mode", "regex")

    def __init__(self, pattern, mode):
        if mode not in MODES:
            raise ValueError("invalid mode %r (expected one of %s)" % (mode, "/".join(MODES)))
        self.pattern = normalize_pattern(pattern)
        self.mode = mode
        self.regex = compile_pattern(self.pattern)

    def matches(self, relpath):
        return self.regex.fullmatch(relpath) is not None

    def __repr__(self):
        return "Rule(%r, %r)" % (self.pattern, self.mode)


class Policy:
    def __init__(self, default=RW, rules=None):
        if default not in MODES:
            raise ValueError("invalid default mode %r" % (default,))
        self.default = default
        self.rules = list(rules or [])

    def resolve(self, relpath, explain=False):
        """Resolve a repo-relative path (no leading '/'; '' = root).

        Returns the mode, or (mode, rule_index_or_None) with explain=True.
        """
        path = relpath.strip("/") if relpath else ""
        if not path:
            # the root itself: governed by the default mode
            mode, idx = self.default, None
        else:
            top = path.split("/", 1)[0]
            if top in FORCED_HIDDEN_TOP:
                return (HIDDEN, None) if explain else HIDDEN
            mode, idx = self.default, None
            for i, rule in enumerate(self.rules):
                if rule.regex.fullmatch(path):
                    mode, idx = rule.mode, i
        return (mode, idx) if explain else mode

    def to_dict(self):
        return {"default": self.default, "rules": [
            {"path": r.pattern, "mode": r.mode} for r in self.rules]}

    @classmethod
    def from_dict(cls, data):
        return cls(
            default=data.get("default", RW),
            rules=[Rule(r["path"], r["mode"]) for r in data.get("rules", [])],
        )
