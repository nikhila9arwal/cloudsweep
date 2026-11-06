"""Run sweeps on your own machines: Slurm clusters, machines you reach over ssh, and this host.

Describe machines once in ~/.config/cloudsweep/machines.ini, or for one project in its untracked
.cloudsweep/machines.ini, which takes precedence (docs/machines.md):

  [deltaai]
  kind = slurm                  # one sbatch --array job per submission
  sbatch = --account=bbxx-dtai-gh --partition=ghx4

  [pembroke2]
  kind = ssh                    # a pool on the machine itself, one command per slot

`--on local` runs a pool on this host and needs no section. cloudsweep reaches other machines
with the system ssh and rsync under their ssh alias (host, by default the section name), so ssh's
own configuration applies, including a ControlMaster connection to a machine that needs a
password and Duo. Each command runs the worker in the machine's own environment, against a
file:// store under workdir; fetch copies a submission back with rsync and verifies it as it
does from AWS.
"""

import configparser
import json
import os
import re
import shlex
import socket
import subprocess
import tempfile
import time
from pathlib import Path

from . import core, engine, fetch, project, settings
from .project import Refusal

FILE = "machines.ini"
COMMON = {"kind", "host", "workdir", "setup"}
KEYS = {"slurm": COMMON | {"sbatch", "cpu_sbatch", "gpu_sbatch", "gpu_type"},
        "ssh": COMMON | {"slots", "gpus", "systemd"}}
GPU_TYPE_RE = re.compile(r"[A-Za-z0-9_.-]+")
LOCAL = "local"  # as a host: this machine, without ssh; as a machine name: built in
NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
HOST_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._@-]*")
WORKDIR_RE = re.compile(r"(?!.*(^|/)\.\.(/|$))[A-Za-z0-9._+@%=,/][A-Za-z0-9._+@%=,/-]*")  # unquoted in scripts
DEFAULT_WORKDIR = "cloudsweep"  # relative paths are relative to the machine's home directory
REMOTE_FILES = ("remote.py", "worker.py", "core.py")
RESULT = "CLOUDSWEEP-RESULT "
MISSING_CODE = 97  # remote exit status: this version of cloudsweep's code is not in workdir yet
ACTIVE = ("PENDING", "RUNNING")
SIGNAL_SECONDS = 120  # Slurm sends SIGTERM this long before the time limit, so the worker can upload


def files(repo):
    """machines.ini files, first match wins: the project's, then the user's."""
    user = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "cloudsweep" / FILE
    return [settings.state_dir(repo) / FILE, user]


def load(repo, name):
    """One machine's settings, checked."""
    if not NAME_RE.fullmatch(name or ""):
        raise Refusal("invalid machine name %r" % name)
    names = []
    for file in files(repo):
        if not file.is_file():
            continue
        parser = configparser.ConfigParser(interpolation=None, inline_comment_prefixes=("#",))
        try:
            parser.read_string(file.read_text(), source=str(file))
        except configparser.Error as error:
            raise Refusal("%s: %s" % (file, error))
        if parser.has_section(name):
            return machine(name, parser[name], "[%s] in %s" % (name, file))
        names += parser.sections()
    if name == LOCAL:
        return machine(name, {}, "the built-in local machine")
    raise Refusal("no machine [%s] in %s; `--on local` runs on this host. Machines described: %s"
                  % (name, " or ".join(str(file) for file in files(repo)), ", ".join(sorted(set(names))) or "none"))


def machine(name, section, where):
    kind = section.get("kind", "ssh" if name == LOCAL else "").strip()
    if kind not in KEYS:
        raise Refusal("%s: kind must be slurm or ssh" % where)
    unknown = sorted(set(section) - KEYS[kind])
    if unknown:
        raise Refusal("%s: %s %s not a %s setting" % (where, ", ".join(unknown), "is" if len(unknown) == 1 else "are",
                                                      kind))
    host = section.get("host", LOCAL if name == LOCAL else name).strip()
    workdir = section.get("workdir", DEFAULT_WORKDIR).strip().rstrip("/")
    workdir = workdir[2:] if workdir.startswith("~/") else workdir
    if not HOST_RE.fullmatch(host):
        raise Refusal("%s: host must be an ssh alias, a host name or local" % where)
    if not WORKDIR_RE.fullmatch(workdir):
        raise Refusal("%s: workdir must be a path of letters, digits and ._+@%%=,/- (relative paths are under "
                      "the machine's home directory)" % where)
    if host == LOCAL and not workdir.startswith("/"):
        workdir = str(Path.home() / workdir)
    result = {"name": name, "kind": kind, "host": host, "workdir": workdir,
              "setup": [line.strip() for line in section.get("setup", "").splitlines() if line.strip()]}
    if kind == "slurm":
        for key in ("sbatch", "cpu_sbatch", "gpu_sbatch"):
            result[key] = shlex.split(section.get(key, ""))
        result["gpu_type"] = section.get("gpu_type", "").strip() or None
        if result["gpu_type"] and not GPU_TYPE_RE.fullmatch(result["gpu_type"]):
            raise Refusal("%s: gpu_type must be a Slurm GPU type such as h100" % where)
        return result
    result["slots"], result["gpus"] = number(section, "slots", where, 1), number(section, "gpus", where, 0)
    value = str(section.get("systemd", "no")).strip().lower()
    if value not in ("yes", "no", "true", "false", "1", "0", "on", "off"):
        raise Refusal("%s: systemd must be yes or no" % where)
    result["systemd"] = value in ("yes", "true", "1", "on")
    return result


def number(section, key, where, least):
    if key not in section:
        return None
    try:
        value = int(section[key])
    except ValueError:
        value = least - 1
    if value < least:
        raise Refusal("%s: %s must be a whole number, at least %d" % (where, key, least))
    return value


def identity(machine, commit, recipe):
    """Synthetic sha256 digests in place of container images: the machine's environment (its
    host and setup lines), and the revision built in it. Returns them with the build directory's
    name; sections for the same host and setup share builds."""
    host = machine["host"] if machine["host"] != LOCAL else "local:" + socket.gethostname()
    environment = "sha256:" + core.sha256_bytes(json.dumps({"schema": core.SCHEMA, "host": host,
                                                            "setup": machine["setup"]},
                                                           sort_keys=True, separators=(",", ":")).encode())
    key = core.build_key(commit, environment, recipe)
    return environment, "sha256:" + key, key[:16]


def code_files():
    here = Path(__file__).resolve().parent
    return {name: (here / name).read_bytes() for name in REMOTE_FILES}


def code_hash(files_code):
    return core.sha256_bytes(b"".join(core.sha256_bytes(files_code[name]).encode() for name in REMOTE_FILES))[:16]


def job_script(job, setup, code):
    """The Slurm batch script: one array task per command, run in workdir. The worker replaces the
    shell (exec), so --signal's SIGTERM reaches it before the time limit and it uploads what it has."""
    submission, seconds = job["submission"], job["seconds"]
    lines = ["#!/bin/bash -l",
             "# Written by cloudsweep: one array task per command of submission %s." % submission,
             "#SBATCH --job-name=%s-run" % submission,
             "#SBATCH --array=0-%d" % (job["count"] - 1),
             "#SBATCH --ntasks=1",
             "#SBATCH --cpus-per-task=%d" % job["vcpus"],
             "#SBATCH --mem=%dM" % job["memory_mib"],
             "#SBATCH --time=%d" % -(-seconds // 60),
             "#SBATCH --signal=B:TERM@%d" % min(SIGNAL_SECONDS, seconds // 2),
             "#SBATCH --output=jobs/%s/logs/%%a.txt" % submission]
    if job["gpus"]:
        lines.append("#SBATCH --gpus-per-node=%s%d" % (job["gpu_type"] + ":" if job.get("gpu_type") else "", job["gpus"]))
    # Setup lines run in the home directory, as in every other step; the job runs in workdir.
    lines += ["set -e", "cd"] + setup + ['cd "$SLURM_SUBMIT_DIR"',
                                         "exec python3 code/%s/remote.py child . %s" % (code, submission)]
    return "\n".join(lines) + "\n"


def at(machine, path):
    """A path on the machine as rsync names it."""
    return path if machine["host"] == LOCAL else "%s:%s" % (machine["host"], path)


def ssh(machine, script):
    """Run script in a bash login shell on the machine (so module and similar setup commands work
    there), in its home directory; its stderr streams here. Returns the process, stdout captured."""
    argv = ["bash", "-lc", script] if machine["host"] == LOCAL else ["ssh", machine["host"],
                                                                    "bash -lc " + shlex.quote(script)]
    try:
        done = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, universal_newlines=True,
                              cwd=str(Path.home()))
    except FileNotFoundError:
        raise Refusal("%s is not installed or not on PATH" % argv[0])
    if done.returncode == 255 and machine["host"] != LOCAL:
        raise Refusal("ssh %s failed (connection or sign-in). A machine that needs a password and Duo needs an "
                      "open ControlMaster connection first: run `ssh %s` with ControlMaster and ControlPersist "
                      "set for it in ~/.ssh/config" % (machine["host"], machine["host"]))
    return done


def rsync(*args):
    try:
        done = subprocess.run(["rsync"] + list(args), stdin=subprocess.DEVNULL, cwd=str(Path.home()))
    except FileNotFoundError:
        raise Refusal("rsync is not installed or not on PATH")
    if done.returncode:
        raise Refusal("rsync failed (exit %d); see the messages above" % done.returncode)


def write_code(tree, files_code):
    directory = tree / "code" / code_hash(files_code)
    directory.mkdir(parents=True)
    for name, data in files_code.items():
        (directory / name).write_bytes(data)


def call(machine, action, *args):
    """Run remote.py ACTION on the machine and return its result, first uploading this version of
    the remote code if workdir lacks it."""
    files_code = code_files()
    program = "%s/code/%s/remote.py" % (machine["workdir"], code_hash(files_code))
    script = "\n".join(["[ -f %s ] || exit %d" % (program, MISSING_CODE), "set -e"] + machine["setup"] +
                       ["exec python3 %s %s %s %s" % (program, action, machine["workdir"],
                                                      " ".join(shlex.quote(arg) for arg in args))])
    done = ssh(machine, script)
    if done.returncode == MISSING_CODE:
        with tempfile.TemporaryDirectory(prefix="cloudsweep-") as scratch:
            write_code(Path(scratch), files_code)
            if ssh(machine, "mkdir -p -m 700 %s" % machine["workdir"]).returncode:
                raise Refusal("could not create %s on %s" % (machine["workdir"], machine["name"]))
            rsync("-a", scratch + "/code/", at(machine, machine["workdir"] + "/code/"))
        done = ssh(machine, script)
    found = [line[len(RESULT):] for line in done.stdout.splitlines() if line.startswith(RESULT)]
    result = json.loads(found[-1]) if found else None
    if done.returncode or not isinstance(result, dict) or "error" in result:
        detail = result["error"] if isinstance(result, dict) and "error" in result else \
            "exit %d; see the messages above" % done.returncode
        raise Refusal("%s on %s failed: %s" % (action, machine["name"], detail))
    return result


def submit(args, prepare):
    """cloudsweep submit --on MACHINE: stage, upload, then build if needed and start the work there."""
    repo = project.repository(args.repo)
    path, receipt = None, None
    try:
        machine = load(repo, args.on)
        if args.isolated:
            raise Refusal("--isolated reserves a whole AWS instance; on a machine, request [run] vcpus and "
                          "memory_mib (slurm) or set slots (ssh)")
        commit, recipe, files_in, commands, hours, vcpus, memory_mib, gpus = prepare(args, repo)
        if machine["kind"] == "ssh" and gpus and machine["gpus"] == 0:
            raise Refusal("these commands need GPUs; [%s] sets gpus = 0" % machine["name"])
        environment, digest, build = identity(machine, commit, recipe)
        submission = core.new_submission_id()
        inputs = {name: core.sha256_file(file) for name, file in sorted(files_in.items())}
        manifest = core.submission_manifest(
            submission, commit, digest, environment, recipe, inputs, commands, vcpus, memory_mib, hours, args.label,
            gpus=gpus, machine={"name": machine["name"], "kind": machine["kind"], "host": machine["host"]})
        files_code = code_files()
        job = {"schema": core.SCHEMA, "submission": submission, "machine": machine["name"], "kind": machine["kind"],
               "commit": commit, "build": build, "image_digest": digest, "count": len(commands),
               "seconds": core.timeout_seconds(hours), "vcpus": vcpus, "memory_mib": memory_mib, "gpus": gpus,
               "setup": machine["setup"]}
        workdir, script = machine["workdir"], None
        plan = {"submission": submission, "machine": machine["name"], "kind": machine["kind"],
                "host": machine["host"], "workdir": workdir, "commit": commit, "children": len(commands),
                "vcpus": vcpus, "memory_mib": memory_mib, "gpus": gpus, "hours": str(hours), "build": build,
                "image_digest": digest}
        if machine["kind"] == "slurm":
            job.update(sbatch=machine["sbatch"] + machine["gpu_sbatch" if gpus else "cpu_sbatch"],
                       gpu_type=machine["gpu_type"])
            script = job_script(job, machine["setup"], code_hash(files_code))
            plan["sbatch"] = ["sbatch"] + job["sbatch"] + ["jobs/%s/job.sh" % submission]
        else:
            job.update(slots=machine["slots"], machine_gpus=machine["gpus"], systemd=machine["systemd"])
            plan["slots"] = ("one per %d GPU(s)" % gpus if gpus else machine["slots"] or
                             "one per %d CPU(s) of the machine" % vcpus)
        if args.dry_run:
            print(json.dumps(dict(plan, dry_run=True), indent=2, sort_keys=True))
            if script:
                print(script, end="")
            return 0
        # The build is reused when there; otherwise the submission that takes builds/KEY.lock
        # uploads the checkout and builds it, and a concurrent one waits for that build.
        probe = ssh(machine, "set -e; umask 077; mkdir -p {w}/builds {w}/jobs {w}/store/inputs {w}/store/runs; "
                             "if [ -f {w}/builds/{b}/build.json ]; then echo built; "
                             "elif mkdir {w}/builds/{b}.lock 2>/dev/null; then echo {s} > {w}/builds/{b}.lock/owner; "
                             "rm -rf {w}/builds/{b}; echo new; else echo busy; fi".format(w=workdir, b=build,
                                                                                         s=submission))
        state = (probe.stdout.split() or [""])[-1]
        if probe.returncode or state not in ("built", "new", "busy"):
            raise Refusal("could not prepare %s on %s (exit %d)" % (workdir, machine["name"], probe.returncode))
        try:
            path, receipt = stage(machine, repo, manifest, commands, job, script, files_in, inputs, files_code,
                                  state, dict(plan, label=args.label))
        except BaseException:
            release(machine, build, submission)
            raise
        try:
            result = call(machine, "submit", submission)
        except BaseException:
            release(machine, build, submission)
            raise
        receipt.update(result=result, submission_state="cancelled" if result.get("cancelled") else "submitted")
        settings.write_receipt(path.parent, receipt, replace=True)
    except (Refusal, ValueError, OSError, subprocess.CalledProcessError) as error:
        if path is not None:
            receipt["error"] = str(error)
            try:
                settings.write_receipt(path.parent, receipt, replace=True)
            except (ValueError, OSError):
                pass
            print("receipt: %s; check `cloudsweep status %s` before trying again" % (path, submission))
        return settings.fail(error)
    if result.get("cancelled"):
        print("%s was cancelled before it started" % submission)
        return 0
    if machine["kind"] == "slurm":
        started = "Slurm job %s" % result["slurm_job"]
    else:
        started = "a pool of %d slot(s)%s, process %s on %s" % (
            result["slots"], " of %d GPU(s)" % gpus if gpus else "", result["pid"], result["host"])
    print("submitted %s: %d command(s) at %s on %s (%s)" % (submission, len(commands), commit[:12],
                                                           machine["name"], started))
    if result.get("warning"):
        print("warning: " + result["warning"])
    print("receipt: %s" % path)
    print("next: cloudsweep status %s    |    cloudsweep fetch %s --wait" % (submission, submission))
    return 0


def release(machine, build, submission):
    """Release the build lock if this submission holds it and no build took it up (a build that
    starts marks the lock and releases it itself); best effort."""
    lock = "%s/builds/%s.lock" % (machine["workdir"], build)
    try:
        ssh(machine, '[ "$(cat {l}/owner 2>/dev/null)" = {s} ] && [ ! -e {l}/building ] && rm -rf {l}; true'
            .format(l=lock, s=submission))
    except Refusal:
        pass


def stage(machine, repo, manifest, commands, job, script, files_in, inputs, files_code, state, plan):
    """Upload the remote code, the submission's records, its job, its inputs (by content digest, as
    on AWS) and, when this submission builds, the checkout; then write the receipt."""
    submission, workdir, build = job["submission"], machine["workdir"], job["build"]
    with tempfile.TemporaryDirectory(prefix="cloudsweep-") as scratch:
        tree = Path(scratch) / "tree"
        tree.mkdir(mode=0o700)  # rsync gives workdir this mode
        write_code(tree, files_code)
        records = tree / "store" / "submissions" / submission
        records.mkdir(parents=True)
        (records / "commands.jsonl").write_bytes(core.commands_bytes(commands))
        (records / "submission.json").write_bytes(core.pretty(manifest))
        jobs = tree / "jobs" / submission
        jobs.mkdir(parents=True)
        (jobs / "job.json").write_bytes(core.pretty(job))
        if script:
            (jobs / "job.sh").write_text(script)
        if state == "new":
            (tree / "builds" / build).mkdir(parents=True)
            engine.checkout(repo, job["commit"], tree / "builds" / build / "src")
            (tree / "builds" / build / core.RECIPE).write_bytes(core.pretty(manifest["recipe"]))
        rsync("-a", str(tree) + "/", at(machine, workdir + "/"))
        if inputs:  # ones already there are not sent again
            links = Path(scratch) / "inputs"
            links.mkdir()
            for name, file in files_in.items():
                if not (links / inputs[name]).exists():
                    (links / inputs[name]).symlink_to(file.resolve())
            rsync("-rL", "--ignore-existing", str(links) + "/", at(machine, workdir + "/store/inputs/"))
    receipt = dict(plan, schema=core.SCHEMA, created=manifest["created"], inputs=inputs,
                   manifest_sha256=core.sha256_bytes(core.pretty(manifest)), submission_state="submitting")
    return settings.write_receipt(settings.state_dir(repo) / "receipts", receipt), receipt


def receipts(repo, name):
    """This checkout's receipts for submissions sent to machine name, oldest first."""
    directory, found = settings.state_dir(repo) / "receipts", []
    for file in sorted(directory.glob("*.json")) if directory.is_dir() else []:
        try:
            receipt = json.loads(file.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(receipt, dict) and receipt.get("machine") == name:
            found.append(receipt["submission"])
    return found


def machine_of(repo, submission):
    """The machine a submission went to, from its receipt; None for AWS (or no receipt)."""
    try:
        receipt = json.loads((settings.state_dir(repo) / "receipts" / (submission + ".json")).read_text())
        return receipt.get("machine") if isinstance(receipt, dict) else None
    except (OSError, ValueError):
        return None


def describe(submission, row):
    children = " children %s" % json.dumps(row["children"], sort_keys=True) if row.get("children") else ""
    job = ""
    if row.get("job"):
        job = " (Slurm job %s)" % row["job"] if row.get("kind") == "slurm" else " (pool process %s)" % row["job"]
    return "  %s %s%s%s%s" % (submission, row.get("state", "UNKNOWN"), children, job,
                              " " + row["error"] if row.get("error") else "")


def status(args, groups):
    """Print each machine's submissions (groups: machine name -> submissions, empty for all of
    this checkout's); one JSON object for all of them with --json."""
    repo, reports = project.repository(), {}
    for name, wanted in groups.items():
        machine = load(repo, name)
        wanted = wanted or receipts(repo, name)
        report = call(machine, "status", *wanted) if wanted else {}
        reports.update(report)
        if args.json:
            continue
        print("%s (%s, %s)" % (name, machine["kind"], at(machine, machine["workdir"])))
        for submission in wanted:
            print(describe(submission, report.get(submission, {})))
        if not wanted:
            print("  no submissions recorded in this checkout")
    if args.json:
        print(json.dumps(reports, indent=2, sort_keys=True))
    return 0


def cancel(args, name):
    repo = project.repository()
    machine = load(repo, name)
    wanted = [args.submission] if args.submission else receipts(repo, name)
    report = call(machine, "cancel", *wanted) if wanted else {}
    for submission in wanted:
        if args.submission or report.get(submission) != "not running":
            print("  %s: %s" % (submission, report.get(submission)))
    deadline = time.monotonic() + 60 * args.wait
    while args.wait and wanted:
        rows = call(machine, "status", *wanted)
        busy = [s for s in wanted if rows.get(s, {}).get("state") in ACTIVE]
        if not busy:
            break
        if time.monotonic() > deadline:
            raise Refusal("still running after --wait: %s" % ", ".join(busy))
        time.sleep(30)
    return 1 if any(str(value).startswith("error") for value in report.values()) else 0


class Fetched(core.LocalStore):
    """The submission as fetch reads it: the machine's listing; its records (submission.json,
    commands.jsonl and every attempt's manifest.json), copied fresh into a scratch directory; and
    the output files rsync has placed in DEST/SUBMISSION/. fetch copies the records into place,
    never over different local content, and checks each output file in place against its manifest."""

    def __init__(self, listing, records, destination, submission):
        super().__init__(Path(destination) / submission)
        self.listing, self.records, self.submission = listing, Path(records), submission

    def path(self, key):
        for prefix in ("submissions/%s/" % self.submission, "runs/%s/" % self.submission):
            if key.startswith(prefix):
                relative = core.input_name(key[len(prefix):])
                record = prefix[0] == "s" or re.fullmatch(r"\d+/[^/]+/manifest\.json", relative)
                return (self.records / self.submission if record else self.root) / relative
        raise ValueError("%s is outside submission %s" % (key, self.submission))

    def list(self, prefix):
        return [(key, size) for key, size in self.listing if key.startswith(prefix)]


def wait_for(machine, submission, minutes):
    """Poll the submission until its job leaves the queue or its pool ends, at most minutes long."""
    deadline = time.monotonic() + 60 * minutes
    while True:
        row = call(machine, "status", submission).get(submission, {})
        print(describe(submission, row).strip(), flush=True)
        if row.get("state") not in ACTIVE:
            return row
        if time.monotonic() > deadline:
            raise Refusal("still %s after --wait %d minutes" % (row.get("state"), minutes))
        time.sleep(60)


def fetch_results(args, name):
    """Copy the submission from the machine's store with rsync, then verify it with fetch's checks."""
    repo = project.repository()
    machine = load(repo, name)
    submission = args.submission
    if args.wait is not None:
        wait_for(machine, submission, args.wait)
    listing = call(machine, "list", submission)["files"]
    destination = Path(args.destination).absolute()
    fetch.no_symlinks(destination / submission)
    destination.mkdir(parents=True, exist_ok=True)
    runs = at(machine, "%s/store/runs/%s" % (machine["workdir"], submission))
    attempts = "/%s/*/*/manifest.json" % submission
    with tempfile.TemporaryDirectory(prefix="cloudsweep-fetch-") as records:
        # The records, fresh each time. Directories named SUBMISSION land in the targets, and
        # --ignore-missing-args covers a submission no command has started yet.
        rsync("-rt", "--ignore-missing-args", "--include=/%s/" % submission, "--include=/%s/*/" % submission,
              "--include=/%s/*/*/" % submission, "--include=" + attempts,
              "--include=/%s/submission.json" % submission, "--include=/%s/commands.jsonl" % submission,
              "--exclude=*", at(machine, "%s/store/submissions/%s" % (machine["workdir"], submission)), runs,
              records + "/")
        # The outputs, where fetch places them; a local file is never replaced.
        rsync("-rt", "--ignore-existing", "--ignore-missing-args", "--exclude=" + attempts, runs,
              str(destination) + "/")
        summary = fetch.fetch(Fetched(listing, records, destination, submission), submission, destination)
    fetch.print_summary(summary, destination)
    return 0 if summary["complete"] else 1


def doctor(name):
    """cloudsweep doctor --on MACHINE: reach the machine and report what cloudsweep needs there."""
    repo = project.repository()
    machine = load(repo, name)
    print("%-10s ok   %s: %s via %s, workdir %s" % ("machine", name, machine["kind"],
                                                     "this host" if machine["host"] == LOCAL else "ssh " + machine["host"],
                                                     machine["workdir"]), flush=True)
    result = call(machine, "check", machine["kind"], "1" if machine["kind"] == "ssh" and machine["gpus"] else "0")
    rows = [("reached", True, result["host"])] + [tuple(row) for row in result["rows"]]
    for label, ok, detail in rows:
        print("%-10s %s  %s" % (label, "ok " if ok else "-- ", detail))
    return 0 if all(ok for _, ok, _ in rows) else 1
