"""The project's own recipe: cloudsweep.ini at the repository root, committed with the code.

Each revision image is built with the recipe in that revision's committed cloudsweep.ini;
the base image and the acceptance run use the file in the working tree.
"""

import configparser
import json
import os
import re
import shlex
import subprocess
from pathlib import Path

from . import core

NAME = "cloudsweep.ini"
PREFIX_RE = re.compile(r"[a-z][a-z0-9-]{0,19}")


class Refusal(Exception):
    """cloudsweep refuses; the message says why and what to do instead."""


def git(repo, *args, check=True):
    done = subprocess.run(["git", "-C", str(repo)] + list(args), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          universal_newlines=True)
    if check and done.returncode:
        raise Refusal("git %s failed in %s: %s" % (" ".join(args[:2]), repo, done.stderr.strip()))
    return done.stdout.strip()


def repository(start=None):
    """The top of the Git checkout that contains start (default: the current directory)."""
    start = Path(start or os.getcwd())
    top = git(start, "rev-parse", "--show-toplevel", check=False)
    if not top:
        raise Refusal("%s is not inside a Git repository; cloudsweep runs committed revisions" % start)
    return Path(top)


def main_checkout(repo):
    """The repository's main working tree (worktrees share its .cloudsweep/ directory)."""
    common = git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir", check=False)
    if not common:  # Git older than 2.31
        common = str((Path(repo) / git(repo, "rev-parse", "--git-common-dir")).resolve())
    common = Path(common)
    return common.parent if common.name == ".git" else Path(repo)


def parse(text, origin):
    parser = configparser.ConfigParser(interpolation=None, inline_comment_prefixes=("#",))
    try:
        parser.read_string(text, source=origin)
    except configparser.Error as error:
        raise Refusal("%s: %s" % (origin, error))
    return parser


def load(repo, commit=None):
    """The parsed recipe from the working tree, or from a commit when one is given."""
    if commit is None:
        path = Path(repo) / NAME
        if not path.is_file():
            raise Refusal("%s is missing; run `cloudsweep init` in the project first" % path)
        return parse(path.read_text(), str(path))
    if not tracked(repo, commit, NAME):
        raise Refusal("commit %s has no %s; commit it first (revision images are built from "
                      "committed files only)" % (commit[:12], NAME))
    return parse(git(repo, "show", "%s:%s" % (commit, NAME)), "%s:%s" % (commit[:12], NAME))


def tracked(repo, commit, path):
    """Whether the revision holds a file at path."""
    return subprocess.run(["git", "-C", str(repo), "cat-file", "-e", "%s:%s" % (commit, path)],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0


def name(config, fallback=None):
    value = config.get("project", "name", fallback=fallback)
    if not value or not PREFIX_RE.fullmatch(value):
        raise Refusal("[project] name must be 1-20 lower-case letters, digits or hyphens, starting with "
                      "a letter (it names every AWS resource): %r" % value)
    return value


def recipe(config):
    """The build recipe: {"command": shell text, "binary": checkout-relative path or None}."""
    command = config.get("build", "command", fallback="").strip()
    binary = config.get("build", "binary", fallback="").strip() or None
    try:
        return core.check_recipe({"command": command, "binary": binary})
    except ValueError as error:
        raise Refusal("[build] in %s: %s" % (NAME, error))


def image(config, repo):
    """The base image's Dockerfile, build context and optional toolchain check."""
    dockerfile = Path(repo) / config.get("image", "dockerfile", fallback="Dockerfile")
    context = Path(repo) / config.get("image", "context", fallback=str(dockerfile.parent.relative_to(repo)))
    if not dockerfile.is_file():
        raise Refusal("[image] dockerfile %s does not exist" % dockerfile)
    if not context.is_dir():
        raise Refusal("[image] context %s is not a directory" % context)
    check = config.get("image", "check", fallback="").strip() or None
    platform = config.get("image", "platform", fallback="").strip() or None
    return {"dockerfile": dockerfile, "context": context, "check": check, "platform": platform}


def run_defaults(config):
    """[run] defaults for submit: vcpus, memory_mib, hours (each optional)."""
    found = {}
    for key, cast in (("vcpus", int), ("memory_mib", int), ("hours", str)):
        if config.has_option("run", key):
            try:
                found[key] = cast(config.get("run", key))
            except ValueError:
                raise Refusal("[run] %s must be a number" % key)
    return found


def acceptance(config):
    """The optional fixed run a new base image must pass before it is published."""
    if not config.has_section("acceptance"):
        return None
    section = config["acceptance"]
    try:
        argv = json.loads(section.get("command", ""))
    except ValueError:
        raise Refusal('[acceptance] command must be a JSON argv list, e.g. ["{bin}", "--out", "{out}"]')
    inputs = shlex.split(section.get("inputs", ""))
    check = section.get("check", "").strip()
    if not check:
        raise Refusal("[acceptance] needs check: a host command that exits 0 when {out} holds the "
                      "expected result")
    hours = section.get("hours", "1")
    return {"command": argv, "inputs": inputs, "check": check, "hours": hours}


TEMPLATE = """\
# cloudsweep.ini: how cloudsweep builds and runs this project on AWS Batch.
# Commit this file. Each run uses the copy in the commit it runs.

[project]
# Names every AWS resource and the cost-allocation tag: 1-20 lower-case letters, digits, hyphens.
name = {name}

[image]
# The base image: your toolchain (compilers, libraries, python3 and git).
# cloudsweep adds its worker on top. Rebuild and publish it when the toolchain changes.
dockerfile = {dockerfile}
# Optional: a shell command run inside the image to check the toolchain (exit 0 = ok).
# check = g++ --version

[build]
# A shell command run as root in a clean checkout of the commit (the working directory)
# when its revision image is built. Leave empty for code that needs no build step.
command = {command}
# Optional: the program that {{bin}} names in commands, relative to the checkout.
binary = {binary}

[run]
# Defaults for `cloudsweep submit`; each can be overridden on the command line.
vcpus = 1
memory_mib = 3500
hours = 1

# Optional: a fixed run that must give a known result before a new base image is published.
# [acceptance]
# command = ["{{bin}}", "--steps", "1000", "--out", "{{out}}"]
# inputs = tests/fixture.dat
# check = python3 tools/compare.py {{out}} tests/reference
"""

DOCKERFILE = """\
# The toolchain your simulator builds and runs with. cloudsweep needs python3 (3.8+) and
# git in it, and adds its worker on top. Pin versions (base image digest, package
# versions) when results must be reproducible across rebuilds.
FROM docker.io/library/ubuntu:24.04
# The apt sandbox line lets this build under rootless podman without subordinate user IDs.
RUN echo 'APT::Sandbox::User "root";' > /etc/apt/apt.conf.d/99sandbox \\
 && apt-get update \\
 && apt-get install -y --no-install-recommends build-essential cmake git python3 ca-certificates \\
 && rm -rf /var/lib/apt/lists/*
"""
