"""gitx — a controlled RW/RO/HIDDEN filesystem view over a real git worktree.

The agent's workspace is a plain git worktree. Paths stay in real time
under git version control, so `git diff` is always the authoritative
record of what the agent changed; HIDDEN is expressed with
sparse-checkout, RO with chmod, and a pre-commit hook guards commits.
"""

__version__ = "0.3.0"
