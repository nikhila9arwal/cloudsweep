"""Shared manifest, command, placement and cost rules; no AWS calls or network.

This file is also copied into every worker image, so it imports only the standard library
and stays compatible with Python 3.8.
"""

import hashlib
import json
import os
import re
import secrets
import tempfile
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

SCHEMA = 1
WORK = "/work"  # inside every image: the checkout is WORK/src, outputs WORK/out, logs WORK/logs
RECIPE = "cloudsweep-recipe.json"  # the build recipe a revision image was built with, under WORK

COMMIT_RE = re.compile(r"[0-9a-f]{40}")
SHA_RE = re.compile(r"[0-9a-f]{64}")
DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
SUBMISSION_RE = re.compile(r"[0-9]{8}T[0-9]{6}Z-[0-9a-f]{6}")
ATTEMPT_RE = re.compile(r"[1-9][0-9]{0,3}(-[1-9][0-9]{0,3})?")
PLACEHOLDER_RE = re.compile(r"\{\{|\}\}|\{([^{}]*)\}|[{}]")
PLAIN_PLACEHOLDERS = ("out", "src", "bin", "index")
MAX_CHILDREN = 10000
DEFAULT_MEMORY_MIB = 3500
# ECS registers less memory than the instance has (the ECS agent and OS reserve some).
# Assuming 90% placeable is conservative at every size.
SCHEDULABLE_MEMORY = Decimal("0.9")
ATTEMPTS = 2  # the job definition's retry strategy: a host failure reruns a job once


def pretty(value):
    return (json.dumps(value, sort_keys=True, indent=2) + "\n").encode()


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def sha256_file(path):
    digest = hashlib.sha256()
    with open(str(path), "rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def now_iso(now=None):
    return (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")


def require(condition, message):
    if not condition:
        raise ValueError(message)


def check_recipe(recipe):
    """The build recipe: a shell command run in the clean checkout, and optionally the
    checkout-relative path of the program that {bin} names."""
    require(isinstance(recipe, dict) and set(recipe) == {"command", "binary"}, "invalid build recipe")
    require(isinstance(recipe["command"], str), "the build command must be a string")
    require(recipe["binary"] is None or (isinstance(recipe["binary"], str) and recipe["binary"]),
            "the binary must be a checkout-relative path or absent")
    if recipe["binary"] is not None:
        input_name(recipe["binary"])
    return recipe


def build_key(commit, image_digest, recipe):
    """A revision image's identity: source revision, base image and build recipe."""
    require(COMMIT_RE.fullmatch(str(commit)), "commit must be a full 40-hex Git commit id")
    require(DIGEST_RE.fullmatch(str(image_digest)), "image digest must be sha256:<64 hex>")
    return sha256_bytes(json.dumps({"schema": SCHEMA, "commit": commit, "image": image_digest,
                                    "recipe": check_recipe(recipe)},
                                   sort_keys=True, separators=(",", ":")).encode())


def new_submission_id(now=None, token=None):
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    return stamp + "-" + (token or secrets.token_hex(3))


def input_name(name):
    """A repository-relative POSIX path (inputs are placed there inside the checkout)."""
    require(isinstance(name, str) and name and "\\" not in name and "\0" not in name,
            "a name must be a repository-relative POSIX path")
    parts = name.split("/")
    require(all(p not in ("", ".", "..") for p in parts),
            "a name must be a repository-relative path without '.' or '..': " + name)
    require(parts[0] != ".git", "nothing may be placed inside .git")
    return name


def check_command(argv, inputs, has_binary=True):
    require(isinstance(argv, list) and argv and all(isinstance(a, str) for a in argv),
            "a command is a non-empty JSON list of strings")
    for arg in argv:
        for match in PLACEHOLDER_RE.finditer(arg):
            if match.group(0) in ("{{", "}}"):
                continue
            name = match.group(1)
            require(name is not None, "stray brace in argument (write {{ or }} for a literal): " + arg)
            if name.startswith("input:"):
                require(name[len("input:"):] in inputs,
                        "placeholder names an input that is not submitted: {" + name + "}")
            else:
                require(name in PLAIN_PLACEHOLDERS, "unknown placeholder {" + name + "}")
                require(name != "bin" or has_binary,
                        "{bin} needs [build] binary in cloudsweep.ini; name the program another way")
    return argv


def substitute(argv, values, inputs):
    """Replace {out} {src} {bin} {index} and {input:NAME}; {{ and }} are literal braces."""
    check_command(argv, inputs, values.get("bin") is not None)

    def replace(match):
        token = match.group(0)
        if token in ("{{", "}}"):
            return token[0]
        name = match.group(1)
        if name.startswith("input:"):
            return values["src"] + "/" + name[len("input:"):]
        return values[name]
    return [PLACEHOLDER_RE.sub(replace, arg) for arg in argv]


def parse_commands(text, inputs=(), has_binary=True):
    """A command manifest: one JSON argv list per line; blank and # lines are skipped."""
    commands = []
    for number, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        try:
            argv = json.loads(stripped)
            commands.append(check_command(argv, set(inputs), has_binary))
        except ValueError as error:
            raise ValueError("command line %d: %s" % (number, error)) from error
    require(commands, "the command manifest has no commands")
    require(len(commands) <= MAX_CHILDREN, "one submission holds at most %d commands" % MAX_CHILDREN)
    return commands


def commands_bytes(commands):
    return b"".join(json.dumps(argv, separators=(",", ":")).encode() + b"\n" for argv in commands)


def attempt_prefix(submission, child, attempt):
    require(SUBMISSION_RE.fullmatch(submission), "invalid submission id")
    require(isinstance(child, int) and 0 <= child < MAX_CHILDREN, "invalid child index")
    require(ATTEMPT_RE.fullmatch(str(attempt)), "invalid attempt name")
    return "runs/%s/%d/%s/" % (submission, child, attempt)


def next_attempt_name(attempt, taken):
    """Batch's attempt number, or a suffixed name when that directory already holds objects."""
    base = str(int(attempt))
    require(ATTEMPT_RE.fullmatch(base), "invalid attempt number")
    if base not in taken:
        return base
    for extra in range(2, 10000):
        name = "%s-%d" % (base, extra)
        if name not in taken:
            return name
    raise ValueError("too many attempt directories")


def attempt_order(name):
    head, _, tail = name.partition("-")
    return (int(head), int(tail or 1))


def submission_manifest(submission, commit, image_digest, base_digest, recipe, inputs, commands,
                        vcpus, memory_mib, hours, label="", created=None):
    manifest = {
        "schema": SCHEMA, "submission": submission, "created": created or now_iso(),
        "commit": commit, "image_digest": image_digest, "base_digest": base_digest,
        "recipe": recipe, "inputs": dict(sorted(inputs.items())),
        "commands": {"sha256": sha256_bytes(commands_bytes(commands)), "count": len(commands)},
        "resources": {"vcpus": vcpus, "memory_mib": memory_mib, "hours": str(hours)},
        "label": label,
    }
    validate_submission(manifest)
    return manifest


def validate_submission(manifest):
    require(isinstance(manifest, dict) and type(manifest.get("schema")) is int
            and manifest["schema"] == SCHEMA, "unknown manifest schema")
    for field, pattern in (("submission", SUBMISSION_RE), ("commit", COMMIT_RE),
                           ("image_digest", DIGEST_RE), ("base_digest", DIGEST_RE)):
        require(isinstance(manifest.get(field), str) and pattern.fullmatch(manifest[field]), "invalid " + field)
    check_recipe(manifest.get("recipe"))
    require(isinstance(manifest.get("inputs"), dict), "inputs must map names to digests")
    for name, digest in manifest["inputs"].items():
        input_name(name)
        require(isinstance(digest, str) and SHA_RE.fullmatch(digest), "invalid input digest for " + name)
    commands = manifest.get("commands")
    require(isinstance(commands, dict) and isinstance(commands.get("sha256"), str)
            and SHA_RE.fullmatch(commands["sha256"]) and type(commands.get("count")) is int
            and 1 <= commands["count"] <= MAX_CHILDREN, "invalid commands record")
    resources = manifest.get("resources")
    require(isinstance(resources, dict) and type(resources.get("vcpus")) is int and resources["vcpus"] >= 1
            and type(resources.get("memory_mib")) is int and resources["memory_mib"] >= 128,
            "invalid resources")
    timeout_seconds(resources.get("hours"))
    return manifest


def timeout_seconds(hours):
    """Validate a time limit (60 seconds to two weeks) and return it in seconds."""
    try:
        value = Decimal(str(hours))
    except InvalidOperation as error:
        raise ValueError("time limit must be a finite duration of 60 seconds to two weeks") from error
    require(value.is_finite() and Decimal(1) / 60 <= value <= 24 * 14,
            "time limit must be a finite duration of 60 seconds to two weeks")
    return int(value * 3600)


# Instance sizing. A deployment's instance types are a mapping name -> {"vcpus", "memory_mib",
# "usd_per_hour"}, written by deploy from the EC2 and Pricing APIs.

def check_instance_types(types):
    require(isinstance(types, dict) and types, "no instance types")
    for name, spec in types.items():
        require(isinstance(name, str) and re.fullmatch(r"[a-z0-9-]+\.[a-z0-9-]+", name), "invalid instance type " + str(name))
        require(isinstance(spec, dict) and type(spec.get("vcpus")) is int and spec["vcpus"] >= 1
                and type(spec.get("memory_mib")) is int and spec["memory_mib"] >= 512,
                "invalid size for " + name)
        try:
            price = Decimal(str(spec.get("usd_per_hour")))
        except InvalidOperation:
            price = Decimal(-1)
        require(price.is_finite() and price > 0, "invalid price for " + name)
    return types


def by_size(types):
    """(name, spec) pairs, smallest first."""
    return sorted(types.items(), key=lambda item: (item[1]["vcpus"], item[1]["memory_mib"],
                                                   Decimal(str(item[1]["usd_per_hour"])), item[0]))


def schedulable_mib(spec):
    """The memory assumed placeable on one instance of this size."""
    return int(spec["memory_mib"] * SCHEDULABLE_MEMORY)


def instance_for(types, vcpus, memory_mib):
    """The cheapest listed size that can place the job, as (name, spec). Refuses requests
    that no listed size can place, instead of leaving them to wait forever."""
    require(type(vcpus) is int and vcpus >= 1 and type(memory_mib) is int and memory_mib >= 128,
            "resources require at least 1 vCPU and 128 MiB")
    fits = [(name, spec) for name, spec in by_size(types)
            if vcpus <= spec["vcpus"] and memory_mib <= schedulable_mib(spec)]
    if fits:
        return min(fits, key=lambda item: Decimal(str(item[1]["usd_per_hour"])))
    name, spec = by_size(types)[-1]
    raise ValueError("no listed instance fits %d vCPUs and %d MiB: the largest, %s, places %d vCPUs and "
                     "%d MiB (assumed %s of its memory); request less or deploy larger instance types"
                     % (vcpus, memory_mib, name, spec["vcpus"], schedulable_mib(spec), SCHEDULABLE_MEMORY))


def largest(types):
    return by_size(types)[-1]


def job_cost(types, vcpus, memory_mib, hours):
    """One run's expected cost: the whole cheapest instance that fits it, for its time limit.
    Runs sharing an instance cost less; a retry after a host failure, startup and idle time add."""
    return Decimal(str(instance_for(types, vcpus, memory_mib)[1]["usd_per_hour"])) * Decimal(str(hours))


def worst_cost(types, vcpus, memory_mib, hours):
    """One run's worst case: alone on the most expensive listed instance that fits it, for its
    whole time limit. Batch may place a job on a larger instance than the best fit when the
    best fit is short of capacity, so this, not job_cost, bounds one attempt."""
    instance_for(types, vcpus, memory_mib)  # refuses requests no size fits
    return max(Decimal(str(spec["usd_per_hour"])) for spec in types.values()
               if vcpus <= spec["vcpus"] and memory_mib <= schedulable_mib(spec)) * Decimal(str(hours))


def estimate(types, jobs, vcpus, memory_mib, hours):
    """Execution cost at the requested limits, expected and worst case, per run and in total."""
    name, spec = instance_for(types, vcpus, memory_mib)
    each, worst = job_cost(types, vcpus, memory_mib, hours), worst_cost(types, vcpus, memory_mib, hours)
    cents = Decimal("0.01")
    return {"instance": name, "instance_usd_per_hour": str(spec["usd_per_hour"]),
            "run_each_usd": str(each.quantize(cents)), "total_estimate_usd": str((each * jobs).quantize(cents)),
            "run_worst_usd": str(worst.quantize(cents)), "total_worst_usd": str((worst * jobs).quantize(cents)),
            "excludes": "startup and idle tails, storage, logs, data transfer"}


def usd_per_vcpu_hour(types):
    """The highest per-vCPU price among the listed sizes (for capacity estimates)."""
    return max(Decimal(str(spec["usd_per_hour"])) / spec["vcpus"] for spec in types.values())


class LocalStore:
    """A directory standing in for the bucket (local trial runs and tests)."""

    def __init__(self, root):
        self.root = Path(root)

    def path(self, key):
        require(key and not key.startswith("/") and ".." not in key.split("/"), "invalid key " + key)
        return self.root / key

    def put_bytes(self, data, key):
        target = self.path(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        handle, temporary = tempfile.mkstemp(dir=str(target.parent), prefix=".part-")
        with os.fdopen(handle, "wb") as stream:
            stream.write(data)
        os.chmod(temporary, 0o644)
        os.replace(temporary, str(target))

    def put_file(self, local, key):
        self.put_bytes(Path(local).read_bytes(), key)

    def get_bytes(self, key):
        target = self.path(key)
        return target.read_bytes() if target.is_file() else None

    def get_file(self, key, local):
        data = self.get_bytes(key)
        require(data is not None, "missing object " + key)
        Path(local).parent.mkdir(parents=True, exist_ok=True)
        Path(local).write_bytes(data)

    def list(self, prefix):
        found = []
        if not self.root.exists():
            return found
        for path in sorted(self.root.rglob("*")):
            if path.is_file() and not path.name.startswith(".part-"):
                key = path.relative_to(self.root).as_posix()
                if key.startswith(prefix):
                    found.append((key, path.stat().st_size))
        return found
