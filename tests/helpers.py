"""Shared test setup: import paths, a deployment's instance types, and Git fixtures."""

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "src" / "cloudsweep"
sys.path.insert(0, str(ROOT / "src"))

from cloudsweep import core  # noqa: E402,F401

TYPES = {"m7a.large": {"vcpus": 2, "memory_mib": 8192, "usd_per_hour": "0.11592"},
         "m7a.xlarge": {"vcpus": 4, "memory_mib": 16384, "usd_per_hour": "0.23184"},
         "m7a.4xlarge": {"vcpus": 16, "memory_mib": 65536, "usd_per_hour": "0.92736"}}

GIT_ENV = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.org",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.org")


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo)] + list(args), check=True, stdout=subprocess.PIPE,
                          universal_newlines=True, env=GIT_ENV).stdout.strip()


def make_repo(path, files):
    """A Git repository at path holding files (name -> text), committed; returns the commit."""
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "--quiet", "-b", "main")
    for name, text in files.items():
        target = path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
        if name.endswith(".sh"):
            target.chmod(0o755)
    git(path, "add", "-A")
    git(path, "commit", "--quiet", "-m", "fixture")
    return git(path, "rev-parse", "HEAD")
