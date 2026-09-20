"""Setup fallback for `pip install -e .` on setuptools < 64 (no PEP 660).

Kept in sync with pyproject.toml (which is the source of truth on modern
tooling).
"""

from setuptools import setup

setup(
    name="gitx",
    version="0.3.0",
    description="Controlled RW/RO/HIDDEN git worktree views for coding agents",
    packages=["gitx"],
    python_requires=">=3.8",
    install_requires=[],
    entry_points={"console_scripts": ["gitx = gitx.cli:main"]},
)
