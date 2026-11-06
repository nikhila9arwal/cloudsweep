"""Run sweeps on your own machines: Slurm clusters, machines you reach over ssh, and this host.

Describe machines once in ~/.config/cloudsweep/machines.ini, or for one project in its untracked
.cloudsweep/machines.ini, which takes precedence (docs/machines.md):

  [deltaai]
  kind = slurm                  # one sbatch --array job per submission
  sbatch = --account=bbxx-dtai-gh --partition=ghx4

  [pembroke2]
  kind = ssh                    # a pool on the machine itself, one command per slot

Without --on, or with `--on local`, a sweep runs in a pool on this host, which needs no section;
`--on aws` runs it on the project's AWS Batch deployment instead. cloudsweep reaches other machines
with the system ssh and rsync under their ssh alias (host, by default the section name), so ssh's
own configuration applies, including a ControlMaster connection to a machine that needs a
password and Duo. Each command runs the worker in the machine's own environment, against a
file:// store under workdir; fetch copies a submission back with rsync and verifies it as it
does from AWS.
"""

import configparser
import hashlib
import json
import os
import platform
import re
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from . import core, engine, fetch, project, settings
from .project import Refusal

FILE = "machines.ini"
COMMON = {"kind", "host", "workdir", "setup", "container", "use"}
KEYS = {"slurm": COMMON | {"sbatch", "cpu_sbatch", "gpu_sbatch", "gpu_type"},
        "ssh": COMMON | {"slots", "gpus", "systemd"}}
GPU_TYPE_RE = re.compile(r"[A-Za-z0-9_.-]+")
SIF_RE = re.compile(r"(~/|/)[A-Za-z0-9._+@%=,/-]+")  # an image already on the machine; unquoted in scripts
LOCAL = "local"  # as a host: this machine, without ssh; as a machine name: built in, and the default
AWS = "aws"  # the machine name of the project's AWS Batch deployment, which this module does not run
NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
HOST_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._@-]*")
WORKDIR_RE = re.compile(r"(?!.*(^|/)\.\.(/|$))[A-Za-z0-9._+@%=,/][A-Za-z0-9._+@%=,/-]*")  # unquoted in scripts
DEFAULT_WORKDIR = "cloudsweep"  # relative paths are relative to the machine's home directory
REMOTE_FILES = ("remote.py", "worker.py", "core.py")
RESULT = "CLOUDSWEEP-RESULT "
MISSING_CODE = 97  # remote exit status: this version of cloudsweep's code is not in workdir yet
ACTIVE = ("PENDING", "RUNNING")
PLATFORM_OF = {"x86_64": "linux/amd64", "amd64": "linux/amd64", "aarch64": "linux/arm64", "arm64": "linux/arm64"}
QEMU = {"linux/amd64": "qemu-x86_64", "linux/arm64": "qemu-aarch64"}  # binfmt_misc entries for emulated builds
SIGNAL_SECONDS = 120  # Slurm sends SIGTERM this long before the time limit, so the worker can upload


def files(repo):
    """machines.ini files, first match wins: the project's, then the user's."""
    user = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "cloudsweep" / FILE
    return [settings.state_dir(repo) / FILE, user]


def sections(repo):
    """(name, section, where) for every machine described, the project's file first."""
    for file in files(repo):
        if not file.is_file():
            continue
        parser = configparser.ConfigParser(interpolation=None, inline_comment_prefixes=("#",))
        try:
            parser.read_string(file.read_text(), source=str(file))
        except configparser.Error as error:
            raise Refusal("%s: %s" % (file, error))
        for name in parser.sections():
            yield name, parser[name], "[%s] in %s" % (name, file)


def load(repo, name):
    """One machine's settings, checked."""
    if not NAME_RE.fullmatch(name or ""):
        raise Refusal("invalid machine name %r" % name)
    names = []
    for found, section, where in sections(repo):
        if found == name:
            return machine(name, section, where)
        names.append(found)
    if name == LOCAL:
        return machine(name, {}, "the built-in local machine")
    raise Refusal("no machine [%s] in %s; `--on local` runs on this host and `--on aws` on AWS. Machines "
                  "described: %s" % (name, " or ".join(str(file) for file in files(repo)),
                                     ", ".join(sorted(set(names))) or "none"))


def show(args):
    """cloudsweep machines: every machine this project can run on, and what each is for."""
    repo, rows, seen = project.repository(), [], set()
    for name, section, where in sections(repo):
        if name in seen:
            continue
        seen.add(name)
        try:
            rows.append(machine(name, section, where))
        except (Refusal, ValueError) as error:  # a malformed section is reported, the others still listed
            rows.append({"name": name, "kind": "?", "host": "?", "container": None, "use": str(error)})
    if LOCAL not in seen:
        rows.append(machine(LOCAL, {}, "the built-in local machine"))
    if settings.settings_path(repo).is_file():
        on = settings.read_values(settings.settings_path(repo)).get(settings.ENABLED) == "1"
        rows.append({"name": AWS, "kind": "aws", "host": "AWS Batch", "container": "its image",
                     "use": "the project's AWS Batch deployment: CPU only, and it costs money (docs/admin.md)" if on
                     else "the project's AWS deployment, with cloud use off (%s=0)" % settings.ENABLED})
    for row in rows:
        row["default"] = row["name"] == LOCAL
    if args.json:
        print(json.dumps(rows, indent=2, sort_keys=True))
        return 0
    for row in rows:
        reach = "this host" if row["host"] == LOCAL else row["host"]
        print("%-16s %-5s %-26s %s" % (row["name"] + (" (default)" if row["default"] else ""), row["kind"],
                                       reach + (", in a container" if row["container"] else ""), row["use"]))
    return 0


def machine(name, section, where):
    if name == AWS:
        raise Refusal("%s: aws names the project's AWS deployment (--on aws); give this machine another name"
                      % where)
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
    container = section.get("container", "").strip() or None
    if container and container.lower() in ("apptainer", "singularity"):
        container = "apptainer"
    elif container and not (SIF_RE.fullmatch(container) and container.endswith(".sif")):
        raise Refusal("%s: container must be apptainer (an image built from [image]), the path of a .sif image on "
                      "the machine, or left out to run in the machine's own environment" % where)
    result = {"name": name, "kind": kind, "host": host, "workdir": workdir, "container": container,
              "use": " ".join(section.get("use", "").split()),
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


def identity(machine, commit, recipe, image=None):
    """Synthetic sha256 digests in place of AWS's registry digests: the machine's environment (its
    host, setup lines and container image, if any), and the revision built in it. Returns them with
    the build directory's name; sections for the same host and setup share builds."""
    host = machine["host"] if machine["host"] != LOCAL else "local:" + socket.gethostname()
    environment = "sha256:" + core.sha256_bytes(json.dumps({"schema": core.SCHEMA, "host": host,
                                                            "setup": machine["setup"], "image": image},
                                                           sort_keys=True, separators=(",", ":")).encode())
    key = core.build_key(commit, environment, recipe)
    return environment, "sha256:" + key, key[:16]


def builds_here(target):
    """Whether this host can build images for target: a container engine and the same
    architecture, or emulation for the other (QEMU's binfmt entry on Linux; Docker Desktop and
    podman machine provide it elsewhere)."""
    try:
        working = engine.engine("info", stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    except (Refusal, OSError):
        return False
    if working.returncode:
        return False
    if platform.system() != "Linux" or target == PLATFORM_OF.get(platform.machine()):
        return True
    try:
        return Path("/proc/sys/fs/binfmt_misc", QEMU[target]).read_text().startswith("enabled")
    except OSError:
        return False


def prepare_image(repo, target):
    """The machine's container image, from the working tree's [image] settings as for AWS: the
    toolchain Dockerfile with cloudsweep's worker layer. Built and checked here when this host can
    build for the machine's architecture; otherwise only identified, for the machine to build."""
    from . import image as images  # image imports submit, which imports this module
    config = project.load(repo)
    spec, name = dict(project.image(config, repo), platform=target), project.name(config)
    if builds_here(target):
        tag = "localhost/%s-base:%s" % (name, target.split("/")[1])
        print("cloudsweep: building the container image for %s here (reused until the toolchain changes)"
              % target, file=sys.stderr, flush=True)
        image_id = engine.build_base(spec, name, tag)
        images.checks(spec, tag)
        key = image_id.split(":", 1)[1][:16]
        exact = "localhost/cloudsweep-image:%s" % key  # a concurrent build may move tag; this names the image
        engine.engine("tag", image_id, exact)
        return {"key": key, "tag": exact, "spec": spec}
    digest = hashlib.sha256(json.dumps([target, engine.WORKER_LAYER, spec["check"]]).encode())
    for path in [spec["dockerfile"]] + context_files(spec["context"]) + \
            [engine.HERE / file for file in engine.WORKER_FILES]:
        digest.update(b"%s\0%o\0" % (str(path).encode(), Path(path).stat().st_mode) + Path(path).read_bytes())
    return {"key": digest.hexdigest()[:16], "tag": None, "spec": spec}


def context_files(context):
    """The Docker build context's files, without the directories Git and cloudsweep keep there."""
    return [path for path in sorted(Path(context).rglob("*")) if path.is_file() and
            not {".git", ".cloudsweep"} & set(path.relative_to(context).parts)]


def existing_image(machine):
    """An image already on the machine (container = PATH.sif), named by its path, size and time, so
    a replaced file makes a new environment (and new builds) rather than reusing old ones."""
    done = ssh(machine, "stat -L -c %%s:%%.9Y %s" % machine["container"])  # size, mtime to the nanosecond
    stamp = (done.stdout.split() or [""])[-1]
    if done.returncode or ":" not in stamp:
        raise Refusal("%s has no image %s" % (machine["name"], machine["container"]))
    key = core.sha256_bytes(("%s\0%s" % (machine["container"], stamp)).encode())[:16]
    return {"key": key, "sif": machine["container"], "stamp": stamp}


def stage_image(tree, image):
    """What the machine needs to have the image: a SIF converted here when this host has apptainer,
    else a docker archive it converts; or, when this host could not build it, the recipe."""
    directory = tree / "images" / image["key"]
    directory.mkdir(parents=True)
    if image["tag"] is None:
        shutil.copytree(str(image["spec"]["context"]), str(directory / "context"), symlinks=True,
                        ignore=shutil.ignore_patterns(".git", ".cloudsweep"))
        shutil.copy2(str(image["spec"]["dockerfile"]), str(directory / "Dockerfile"))
        (directory / "worker").mkdir()
        for file in engine.WORKER_FILES:
            shutil.copy2(str(engine.HERE / file), str(directory / "worker" / file))
        (directory / "worker" / "Dockerfile").write_text(engine.WORKER_LAYER.format(
            toolchain="localhost/cloudsweep-%s-toolchain:latest" % image["key"], work=core.WORK))
        return
    archive = directory / "base.tar"
    engine.engine("save", *(["--format", "docker-archive"] if engine.engine_name() == "podman" else []),
                  "-o", str(archive), image["tag"], stdout=subprocess.DEVNULL)
    runtime = shutil.which("apptainer") or shutil.which("singularity")
    if runtime:
        print("cloudsweep: converting the image to a SIF here", file=sys.stderr, flush=True)
        subprocess.run([runtime, "build", str(directory / "base.sif"), "docker-archive://" + str(archive)],
                       check=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL)  # errors still show
        archive.unlink()


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
        image = None
        if machine["container"] and machine["container"] != "apptainer" and not args.dry_run:
            image = existing_image(machine)
        elif machine["container"] and not args.dry_run:
            arch = ssh(machine, "uname -m").stdout.split()
            target = PLATFORM_OF.get(arch[-1] if arch else "")
            if not target:
                raise Refusal("cannot tell %s's architecture (uname -m printed %r)" % (machine["name"], arch))
            image = prepare_image(repo, target)
        environment, digest, build = identity(machine, commit, recipe, image and image["key"])
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
        if image and image.get("sif"):
            job["container"] = {"key": image["key"], "sif": image["sif"], "stamp": image["stamp"]}
        elif image:
            job["container"] = {"key": image["key"], "check": None if image["tag"] else image["spec"]["check"]}
        workdir, script = machine["workdir"], None
        plan = {"submission": submission, "machine": machine["name"], "kind": machine["kind"],
                "host": machine["host"], "workdir": workdir, "commit": commit, "children": len(commands),
                "vcpus": vcpus, "memory_mib": memory_mib, "gpus": gpus, "hours": str(hours), "build": build,
                "image_digest": digest}
        if machine["container"] and not image:
            plan["container"] = "apptainer; the image is built when you submit"
        if image and image.get("sif"):
            plan["container"] = "apptainer, the machine's image %s" % image["sif"]
        elif image:
            plan["container"] = "apptainer, image %s built %s" % (image["key"], "here" if image["tag"] else
                                                                  "on the machine (this host cannot build for it)")
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
        # Each of the build and the image is reused when there; otherwise the submission that takes
        # its lock uploads what makes it, and a concurrent one waits for it.
        locks = ["builds/" + build] + (["images/" + image["key"]] if image and not image.get("sif") else [])
        probe = ssh(machine, "set -e; umask 077; mkdir -p {w}/builds {w}/images {w}/jobs {w}/store/inputs "
                             "{w}/store/runs\n".format(w=workdir) + "\n".join(
            "if [ -f {w}/{d}/{f} ]; then echo built; elif mkdir {w}/{d}.lock 2>/dev/null; then echo {s} > "
            "{w}/{d}.lock/owner; rm -rf {w}/{d}; echo new; else echo busy; fi".format(
                w=workdir, d=lock, f="build.json" if lock.startswith("builds") else "base.sif", s=submission)
            for lock in locks))
        states = probe.stdout.split()[-len(locks):]
        if probe.returncode or len(states) != len(locks) or not set(states) <= {"built", "new", "busy"}:
            raise Refusal("could not prepare %s on %s (exit %d)" % (workdir, machine["name"], probe.returncode))
        try:
            path, receipt = stage(machine, repo, manifest, commands, job, script, files_in, inputs, files_code,
                                  states[0], dict(plan, label=args.label), image if states[1:] == ["new"] else None)
            result = call(machine, "submit", submission)
        except BaseException:
            for lock in locks:
                release(machine, lock, submission)
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


def release(machine, made, submission):
    """Release the lock on made (builds/KEY or images/KEY) if this submission holds it and nothing
    took it up (making starts by marking the lock, and releases it itself); best effort."""
    lock = "%s/%s.lock" % (machine["workdir"], made)
    try:
        ssh(machine, '[ "$(cat {l}/owner 2>/dev/null)" = {s} ] && [ ! -e {l}/building ] && rm -rf {l}; true'
            .format(l=lock, s=submission))
    except Refusal:
        pass


def stage(machine, repo, manifest, commands, job, script, files_in, inputs, files_code, state, plan, image=None):
    """Upload the remote code, the submission's records, its job, its inputs (by content digest, as
    on AWS), the checkout when this submission builds it and the image when it makes that; then
    write the receipt."""
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
        if image:
            stage_image(tree, image)
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
    """The machine a submission went to, from its receipt (AWS receipts name none, and may be kept
    where the deployment's RECEIPTS says); local without one."""
    places = [settings.state_dir(repo) / "receipts"]
    try:
        places.append(Path(settings.read_values(settings.settings_path(repo))["RECEIPTS"]))
    except (OSError, ValueError, KeyError):
        pass
    for place in places:
        try:
            receipt = json.loads((place / (submission + ".json")).read_text())
        except (OSError, ValueError):
            continue
        if isinstance(receipt, dict):
            return receipt.get("machine") or AWS
    return LOCAL


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


def doctor(repo, name, row):
    """cloudsweep doctor's machine checks: reach the machine and report what cloudsweep needs there."""
    machine = load(repo, name)
    row("machine", True, "%s: %s via %s, workdir %s" % (name, machine["kind"], "this host" if machine["host"] == LOCAL
                                                        else "ssh " + machine["host"], machine["workdir"]))
    result = call(machine, "check", machine["kind"], "1" if machine["kind"] == "ssh" and machine["gpus"] else "0",
                  "1" if machine["container"] else "0")
    row("reached", True, result["host"])
    for label, ok, detail in result["rows"]:
        row(label, ok, detail)
    if machine["container"] and machine["container"] != "apptainer":
        try:
            existing_image(machine)
            row("image", True, machine["container"])
        except Refusal as error:
            row("image", False, str(error))
