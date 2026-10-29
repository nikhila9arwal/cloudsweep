#!/usr/bin/env python3
"""worker.py check | build | run SUBMISSION

The program inside every cloudsweep image (needs python3 >= 3.8 and git).
  check   report that this image can host the worker
  build   (revision-image build, as root) run the project's build recipe in the clean
          checkout and record the commit and the binary's hash
  run     run one AWS Batch child of a submission: fetch inputs, run the command, upload
          outputs and logs, and write manifest.json last to mark the attempt complete
CLOUDSWEEP_STORE is s3://BUCKET or file:///DIR; IMAGE_DIGEST comes from the job definition.
"""

import json
import os
import platform
import resource
import shutil
import signal
import ssl
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import core  # noqa: E402

GRACE_SECONDS = 20  # the child's time to stop after SIGTERM; Batch sends SIGKILL at 30 s


class Terminated(Exception):
    pass


class Stop:
    """SIGTERM/SIGINT: forward to the running command's process group, then finish uploads."""

    def __init__(self):
        self.signal = None
        self.at = None
        self.child = None

    def handle(self, signum, frame):
        if self.signal is None:
            self.signal, self.at = signum, time.monotonic()
        self.forward(signal.SIGTERM)

    def forward(self, signum):
        if self.child is not None:
            try:
                os.killpg(self.child.pid, signum)
            except ProcessLookupError:
                pass

    def check(self):
        if self.signal is not None:
            raise Terminated("terminated by signal %d" % self.signal)


STOP = Stop()


def open_store(url, env):
    if url.startswith("file://"):
        return core.LocalStore(url[len("file://"):])
    if url.startswith("s3://") and "/" not in url[len("s3://"):].strip("/"):
        import s3lite
        region = env.get("AWS_REGION") or env.get("AWS_DEFAULT_REGION")
        core.require(region, "AWS_DEFAULT_REGION is not set")
        return s3lite.S3Store(url[len("s3://"):].strip("/"), region)
    raise ValueError("CLOUDSWEEP_STORE must be s3://BUCKET or file:///DIR")


class Paths:
    def __init__(self, work):
        self.work = Path(work)
        self.src, self.record = self.work / "src", self.work / "build.json"
        self.recipe = self.work / core.RECIPE
        self.out, self.logs = self.work / "out", self.work / "logs"

    def binary(self, recipe):
        return None if recipe["binary"] is None else self.src / recipe["binary"]

    def fresh(self):
        """Empty output and log directories; the image's checkout and build stay as built."""
        for path in (self.out, self.logs):
            if path.exists():
                shutil.rmtree(str(path))
            path.mkdir(parents=True)


def log(message):
    print("worker: " + message, file=sys.stderr, flush=True)


def run_quiet(argv, **kwargs):
    return subprocess.check_output(argv, stderr=subprocess.STDOUT, universal_newlines=True, **kwargs).strip()


def tracked_changes(src):
    """A hash of the tracked files' state relative to the commit: which changed, and their
    content and modes (empty diff for a clean checkout)."""
    status = subprocess.check_output(["git", "-C", str(src), "status", "--porcelain", "--untracked-files=no"])
    diff = subprocess.check_output(["git", "-C", str(src), "diff", "--binary", "HEAD"])
    return core.sha256_bytes(status + b"\0" + diff)


def run_child(argv, **kwargs):
    """Stop the workload and its descendants within the child grace period."""
    STOP.check()
    child = subprocess.Popen(argv, stdin=subprocess.DEVNULL, start_new_session=True, **kwargs)
    STOP.child = child
    try:
        if STOP.signal is not None:
            STOP.forward(signal.SIGTERM)
        while True:
            try:
                return child.wait(timeout=1)
            except subprocess.TimeoutExpired:
                if STOP.signal is not None and time.monotonic() - STOP.at > GRACE_SECONDS:
                    STOP.forward(signal.SIGKILL)
    finally:
        # A shell can exit before its descendants. Stop that group before collecting
        # outputs, even when the leader already exited on SIGTERM.
        if STOP.signal is not None or child.poll() is None:
            STOP.forward(signal.SIGKILL)
            child.wait()
        STOP.child = None


def load_submission(store, submission):
    data = store.get_bytes("submissions/%s/submission.json" % submission)
    core.require(data is not None, "no submission manifest for " + submission)
    manifest = core.validate_submission(json.loads(data.decode()))
    core.require(manifest["submission"] == submission, "manifest names another submission")
    data = store.get_bytes("submissions/%s/commands.jsonl" % submission)
    core.require(data is not None, "no command manifest")
    core.require(core.sha256_bytes(data) == manifest["commands"]["sha256"], "command manifest changed")
    commands = core.parse_commands(data.decode(), manifest["inputs"], manifest["recipe"]["binary"] is not None)
    core.require(len(commands) == manifest["commands"]["count"], "command count changed")
    return manifest, commands


def verified_build(manifest, env, paths):
    """This image's build record, when it is the submission's image and its build is intact."""
    digest = env.get("IMAGE_DIGEST", "")
    core.require(digest == manifest["image_digest"],
                 "this image (%s) is not the submission's image (%s)" % (digest, manifest["image_digest"]))
    build = json.loads(paths.record.read_text())
    core.require(build.get("commit") == manifest["commit"],
                 "this image holds commit %s, not the submission's %s" % (build.get("commit"), manifest["commit"]))
    core.require(build.get("recipe") == manifest["recipe"], "this image was built with another recipe")
    core.require(run_quiet(["git", "-C", str(paths.src), "rev-parse", "HEAD"]) == manifest["commit"]
                 and tracked_changes(paths.src) == build["tracked_changes"],
                 "the checkout differs from its build record")
    binary = paths.binary(manifest["recipe"])
    if binary is not None:
        core.require(core.sha256_file(binary) == build["binary"]["sha256"], "the binary differs from its build record")
    return build


def place_inputs(store, manifest, paths):
    root = paths.src.resolve()
    for name, digest in manifest["inputs"].items():
        target = paths.src / core.input_name(name)
        core.require(not os.path.lexists(str(target)), "input %s would replace a file of the revision" % name)
        core.require(not any((paths.src / parent).is_symlink() for parent in target.relative_to(paths.src).parents),
                     "input %s has a symbolic-link parent" % name)
        core.require(root in [target.parent.resolve()] + list(target.parent.resolve().parents),
                     "input %s would land outside the checkout" % name)
        target.parent.mkdir(parents=True, exist_ok=True)
        store.get_file("inputs/" + digest, target)
        core.require(core.sha256_file(target) == digest, "input %s differs from its submitted digest" % name)
        target.chmod(0o444)


def host_record():
    record = {"kernel": platform.release(), "machine": platform.machine(), "cpus_online": os.cpu_count(),
              "affinity": sorted(os.sched_getaffinity(0))}
    try:
        with open("/proc/cpuinfo") as stream:
            models = [line.split(":", 1)[1].strip() for line in stream if line.startswith("model name")]
        record["cpu_model"] = models[0] if models else ""
    except OSError:
        pass
    for name in ("cpu.max", "cpu.weight", "memory.max"):
        try:
            record["cgroup_" + name.replace(".", "_")] = Path("/sys/fs/cgroup", name).read_text().strip()
        except OSError:
            pass
    return record


def execute(argv, cwd, stdout, stderr):
    """Run one command in its own process group; returns (exit status, resource usage)."""
    before = resource.getrusage(resource.RUSAGE_CHILDREN)
    with open(str(stdout), "wb") as out, open(str(stderr), "wb") as err:
        code = run_child(argv, cwd=str(cwd), stdout=out, stderr=err)
    after = resource.getrusage(resource.RUSAGE_CHILDREN)
    usage = {"user_s": round(after.ru_utime - before.ru_utime, 3),
             "system_s": round(after.ru_stime - before.ru_stime, 3),
             "children_max_rss_kib": after.ru_maxrss}
    return code, usage


def upload_tree(store, prefix, files, root, under=""):
    for path in sorted(root.rglob("*")):
        relative = under + path.relative_to(root).as_posix()
        if path.is_symlink():
            files[relative] = {"skipped": "symbolic link"}
        elif path.is_file():
            try:
                store.put_file(path, prefix + relative)
            except Exception as error:  # recorded; the attempt still completes with what it has
                files[relative] = {"skipped": "upload failed: %s" % error}
                continue
            files[relative] = {"sha256": core.sha256_file(path), "size": path.stat().st_size}


def run_mode(store, submission, env, work):
    paths = Paths(work)
    paths.fresh()
    child = int(env.get("AWS_BATCH_JOB_ARRAY_INDEX", "0"))
    base = "runs/%s/%d/" % (submission, child)
    taken = {key[len(base):].split("/", 1)[0] for key, _ in store.list(base)}
    attempt = core.next_attempt_name(env.get("AWS_BATCH_JOB_ATTEMPT", "1"), taken)
    prefix = core.attempt_prefix(submission, child, attempt)
    started = {"schema": core.SCHEMA, "submission": submission, "child": child, "attempt": attempt,
               "started": core.now_iso(), "image_digest": env.get("IMAGE_DIGEST", ""),
               "batch": {key[10:].lower(): value for key, value in env.items() if key.startswith("AWS_BATCH_")},
               "host": host_record()}
    started_data = core.pretty(started)
    store.put_bytes(started_data, prefix + "started.json")
    files = {"started.json": {"sha256": core.sha256_bytes(started_data), "size": len(started_data)}}
    result = {"status": "failed", "exit_code": None, "argv": None, "error": None, "usage": None}
    phases, clock = {}, time.monotonic()
    stdout, stderr = paths.logs / "stdout.txt", paths.logs / "stderr.txt"
    build, manifest = None, None
    try:
        manifest, commands = load_submission(store, submission)
        core.require(0 <= child < len(commands), "array index %d is outside the command manifest" % child)
        build = verified_build(manifest, env, paths)
        STOP.check()
        place_inputs(store, manifest, paths)
        phases["fetch_s"], clock = round(time.monotonic() - clock, 3), time.monotonic()
        STOP.check()
        binary = paths.binary(manifest["recipe"])
        argv = core.substitute(commands[child], {"out": str(paths.out), "src": str(paths.src),
                                                 "bin": None if binary is None else str(binary),
                                                 "index": str(child)},
                               manifest["inputs"])
        result["argv"] = argv
        log("child %d attempt %s: %s" % (child, attempt, " ".join(argv)))
        code, result["usage"] = execute(argv, paths.src, stdout, stderr)
        phases["run_s"], clock = round(time.monotonic() - clock, 3), time.monotonic()
        result["exit_code"] = code
        result["status"] = ("terminated" if STOP.signal is not None
                            else "succeeded" if code == 0 else "failed")
    except Terminated as error:
        result.update(status="terminated", error=str(error))
    except Exception as error:  # recorded in the manifest; the job fails
        result["error"] = "%s: %s" % (type(error).__name__, error)
    upload_tree(store, prefix, files, paths.out, "out/")
    upload_tree(store, prefix, files, paths.logs)
    phases["upload_s"] = round(time.monotonic() - clock, 3)
    if STOP.signal is not None:
        result["status"] = "terminated"
    final = dict(result, schema=core.SCHEMA, submission=submission, child=child, attempt=attempt,
                 commit=(manifest or {}).get("commit"), image_digest=env.get("IMAGE_DIGEST", ""),
                 binary_sha256=((build or {}).get("binary") or {}).get("sha256"),
                 started=started["started"], finished=core.now_iso(), phases=phases, files=files)
    store.put_bytes(core.pretty(final), prefix + "manifest.json")  # last: the attempt is complete
    log("child %d attempt %s: %s (exit %s)" % (child, attempt, result["status"], result["exit_code"]))
    if result["status"] == "succeeded":
        return 0
    if result["status"] == "terminated":
        return 128 + (STOP.signal or signal.SIGTERM)
    code = result["exit_code"]
    return 128 - code if isinstance(code, int) and code < 0 else (code or 1)


def build_mode(work):
    """Inside a revision-image build, as root: run the recipe's command in the clean checkout,
    then record the commit, the binary's hash and the state of the tracked files."""
    paths = Paths(work)
    recipe = core.check_recipe(json.loads(paths.recipe.read_text()))
    core.require(run_quiet(["git", "-C", str(paths.src), "status", "--porcelain"]) == "", "the checkout is not clean")
    if recipe["command"].strip():
        log("$ " + recipe["command"])
        subprocess.run(["/bin/sh", "-c", recipe["command"]], cwd=str(paths.src), check=True)
    record = {"schema": core.SCHEMA, "commit": run_quiet(["git", "-C", str(paths.src), "rev-parse", "HEAD"]),
              "recipe": recipe, "tracked_changes": tracked_changes(paths.src), "binary": None}
    binary = paths.binary(recipe)
    if binary is not None:
        core.require(binary.is_file() and os.access(str(binary), os.X_OK),
                     "the build did not produce an executable %s" % recipe["binary"])
        record["binary"] = {"path": str(binary), "sha256": core.sha256_file(binary), "size": binary.stat().st_size}
    paths.record.write_bytes(core.pretty(record))
    # Jobs run as an unprivileged user and may write inside the checkout (inputs are placed
    # there). Git tracks only the executable bit, which a+rwX leaves as it was for files.
    subprocess.run(["chmod", "-R", "a+rwX", str(paths.src)], check=True)
    log("built %s: %s" % (record["commit"], "sha256 %s" % record["binary"]["sha256"] if binary else "no binary"))
    return 0


def check_mode():
    """Whether this image can host the worker: Python version and Git."""
    problems, versions = [], {"python": platform.python_version()}
    if sys.version_info < (3, 8):
        problems.append("python3 is %s; the worker needs 3.8 or newer" % platform.python_version())
    try:
        versions["git"] = run_quiet(["git", "--version"])
    except (OSError, subprocess.CalledProcessError):
        problems.append("git is missing")
    # The worker reaches S3 over HTTPS, which needs Python's ssl module and CA certificates.
    paths = ssl.get_default_verify_paths()
    if not ((paths.cafile and os.path.isfile(paths.cafile))
            or (paths.capath and os.path.isdir(paths.capath) and os.listdir(paths.capath))):
        problems.append("no CA certificates for HTTPS (install the ca-certificates package)")
    print(json.dumps({"ok": not problems, "problems": problems, "versions": versions}, indent=2, sort_keys=True))
    return 0 if not problems else 1


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    os.environ.setdefault("GIT_OPTIONAL_LOCKS", "0")
    work = os.environ.get("CLOUDSWEEP_WORK", core.WORK)
    if args == ["check"]:
        return check_mode()
    if args == ["build"]:
        return build_mode(work)
    if len(args) != 2 or args[0] != "run" or not core.SUBMISSION_RE.fullmatch(args[1]):
        print(__doc__, file=sys.stderr)
        return 2
    for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGALRM):
        signal.signal(signum, STOP.handle)
    if os.environ.get("CLOUDSWEEP_TIME_LIMIT"):  # local runs; on AWS, Batch enforces the limit
        signal.alarm(int(os.environ["CLOUDSWEEP_TIME_LIMIT"]))
    return run_mode(open_store(os.environ.get("CLOUDSWEEP_STORE", ""), os.environ), args[1], os.environ, work)


if __name__ == "__main__":
    sys.exit(main())
