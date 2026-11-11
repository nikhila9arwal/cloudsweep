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
BINFMT = Path("/proc/sys/fs/binfmt_misc")
# An image file's size and modification time in nanoseconds, as remote.image_stamp gives them; read
# with the machine's Python, since stat's formats differ between coreutils implementations.
STAMP = "import os, sys; s = os.stat(os.path.expanduser(sys.argv[1])); print('%d:%d' % (s.st_size, s.st_mtime_ns))"
SIGNAL_SECONDS = 120  # Slurm sends SIGTERM this long before the time limit, so the worker can upload


def files(repo, extra=None):
    """machines.ini files, first match wins: the one given with --machines, the project's, then
    the user's."""
    user = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "cloudsweep" / FILE
    return ([Path(extra).absolute()] if extra else []) + [settings.state_dir(repo) / FILE, user]


def sections(repo, extra=None):
    """(name, section, file) for every machine described, in the order files() searches them."""
    if extra and not Path(extra).is_file():
        raise Refusal("--machines %s: no such file" % extra)
    for file in files(repo, extra):
        if not file.is_file():
            continue
        parser = configparser.ConfigParser(interpolation=None, inline_comment_prefixes=("#",))
        try:
            parser.read_string(file.read_text(), source=str(file))
        except configparser.Error as error:
            raise Refusal("%s: %s" % (file, error))
        for name in parser.sections():
            yield name, parser[name], file


def load(repo, name, extra=None):
    """One machine's settings, checked."""
    if not NAME_RE.fullmatch(name or ""):
        raise Refusal("invalid machine name %r" % name)
    names = []
    for found, section, file in sections(repo, extra):
        if found == name:
            return machine(name, section, "[%s] in %s" % (name, file), file)
        names.append(found)
    if name == LOCAL:
        return machine(name, {}, "the built-in local machine")
    raise Refusal("no machine [%s] in %s; `--on local` runs on this host and `--on aws` on AWS. Machines "
                  "described: %s" % (name, " or ".join(str(file) for file in files(repo, extra)),
                                     ", ".join(sorted(set(names))) or "none"))


def show(args):
    """cloudsweep machines: every machine this project can run on, and what each is for."""
    repo, rows, seen = project.repository(), [], set()
    for name, section, file in sections(repo, args.machines):
        if name in seen:
            continue
        seen.add(name)
        try:
            rows.append(machine(name, section, "[%s] in %s" % (name, file), file))
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


def machine(name, section, where, file=None):
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
              "file": str(file) if file else None, "use": " ".join(section.get("use", "").split()),
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


def add_file_argument(parser):
    parser.add_argument("--machines", type=Path, metavar="FILE",
                        help="a machines.ini searched before the project's and yours (a submission's receipt "
                             "records the settings it used, so later commands need no flag)")


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
    return emulates(target)


def emulates(target, binfmt=BINFMT):
    """Whether containers here can run target's programs: binfmt_misc is enabled, and so is QEMU's
    entry, with the F flag, so the kernel opens the emulator when the entry is registered. Without
    F (as with Ubuntu's qemu-user-binfmt) it looks for the emulator inside each container, and an
    ordinary image has none, so the build fails; qemu-user-static registers it with F."""
    try:
        if (Path(binfmt) / "status").read_text().split() != ["enabled"]:
            return False
        lines = (Path(binfmt) / QEMU[target]).read_text().splitlines()
    except (OSError, KeyError):
        return False
    flags = [line.split(":", 1)[1].strip() for line in lines if line.startswith("flags:")]
    return bool(lines) and lines[0] == "enabled" and bool(flags) and "F" in flags[0]


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
    done = ssh(machine, "\n".join(["set -e"] + machine["setup"] + ["python3 -c %s %s" % (
        shlex.quote(STAMP), machine["container"])]))
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


MEMORY_RE = re.compile(r"([0-9]+)([KMGT]?)", re.I)
UNITS = "KMGTP"
# sbatch options that change the request in ways the billing below does not count.
UNCOUNTED = ("--exclusive", "--mem-per-cpu", "--mem-per-gpu", "--cpus-per-gpu", "--gres", "--gpus", "-G",
             "--gpus-per-node", "--gpus-per-task", "--ntasks", "-n", "--nodes", "-N")


def requested(job):
    """What each array task asks Slurm for: the batch script's request with the sbatch options from
    machines.ini, which win, applied. Returns the partitions (None: the default one), the request
    and the options that change it in ways not counted."""
    partitions, uncounted = None, []
    request = {"cpus": job["vcpus"], "memory_mib": job["memory_mib"], "gpus": job["gpus"],
               "gpu_type": job.get("gpu_type") if job["gpus"] else None}
    options, index = list(job["sbatch"]), 0
    while index < len(options):
        option, value = options[index], None
        for flag, short in (("--partition", "-p"), ("--cpus-per-task", "-c"), ("--mem", None)):
            if option.startswith(flag + "="):
                value = option[len(flag) + 1:]
            elif option == flag or option == short:
                value = options[index + 1] if index + 1 < len(options) else ""
                index += 1
            elif short and option.startswith(short) and not option.startswith("--"):
                value = option[len(short):]
            if value is not None:
                break
        if value is None:
            if option.split("=")[0] in UNCOUNTED:
                uncounted.append(option)
        elif flag == "--partition":
            partitions = [name for name in value.split(",") if name]
        elif flag == "--cpus-per-task" and value.isdigit():
            request["cpus"] = int(value)
        elif flag == "--mem" and MEMORY_RE.fullmatch(value) and int(MEMORY_RE.fullmatch(value).group(1)):
            number, unit = MEMORY_RE.fullmatch(value).groups()
            request["memory_mib"] = int(number) * 1024 ** (UNITS.index((unit or "M").upper()) - 1)
        else:
            uncounted.append(option)
        index += 1
    return partitions, request, uncounted


def settings_line(text):
    """scontrol's one-line form (Key=Value ...) as a dict."""
    return dict(item.partition("=")[::2] for item in text.split() if "=" in item)


def billing_units(weights, flags, request):
    """The billing units of one task by Slurm's rule for the partition's TRESBillingWeights: each
    weight times the amount requested (memory weights per MiB, or per unit of their suffix), summed,
    or with PriorityFlags MAX_TRES the largest of them. Without weights Slurm bills the CPUs."""
    if not weights:
        return {"units": request["cpus"], "terms": {"cpu": request["cpus"]},
                "notes": ["the partition sets no TRESBillingWeights, so Slurm bills the allocated CPUs, which "
                          "can exceed those requested (a node that allocates whole cores)"]}
    amounts = {"cpu": request["cpus"], "mem": request["memory_mib"], "node": 1, "gres/gpu": request["gpus"]}
    if request["gpu_type"]:
        amounts["gres/gpu:" + request["gpu_type"].lower()] = request["gpus"]
    terms, notes = {}, []
    for item in weights.split(","):
        name, _, text = item.partition("=")
        name, found = name.strip().lower(), re.fullmatch(r"([0-9]*\.?[0-9]+)([KMGTP]?)", text.strip(), re.I)
        if not found or (found.group(2) and name != "mem"):
            return {"error": "cannot read the billing weight %s" % item}
        weight = float(found.group(1)) * 1024 ** (1 - UNITS.index((found.group(2) or "M").upper()))
        if name in amounts:
            terms[name] = weight * amounts[name]
        elif not name.startswith("gres/gpu:"):
            notes.append("the weight %s is not counted" % item)
    flag_set = {flag.strip() for flag in flags.split(",") if flag.strip()}
    if {flag for flag in flag_set if flag.startswith("MAX_TRES")} - {"MAX_TRES"}:
        return {"error": "PriorityFlags %s: a billing rule cloudsweep does not apply" % flags}
    largest = "MAX_TRES" in flag_set
    return {"units": round(max(terms.values()) if largest else sum(terms.values()), 2) if terms else 0,
            "terms": {name: round(value, 2) for name, value in terms.items()}, "notes": notes,
            "rule": "the largest term (MAX_TRES)" if largest else "the sum"}


def gpu_share(billing, request):
    """For a GPU request: the units per GPU, the request in GPUs' worth of billing, and a warning
    when its CPUs or memory bill more than its GPUs, that is, exceed the GPUs' share of them."""
    terms = billing["terms"]
    gpu_terms = [value for name, value in terms.items() if name.startswith("gres/gpu")]
    if not request["gpus"] or not gpu_terms or not billing["units"]:
        return {}
    per_gpu = (max(gpu_terms) if "MAX_TRES" in billing["rule"] else sum(gpu_terms)) / request["gpus"]
    if per_gpu <= 0:  # GPUs weigh nothing here: there is no GPU's worth to state
        return {}
    found, over = {"per_gpu": round(per_gpu, 2), "as_gpus": round(billing["units"] / per_gpu, 3)}, []
    gpus_units = per_gpu * request["gpus"]
    if terms.get("cpu", 0) > gpus_units:
        over.append("%d CPUs exceed %d GPU(s)' share (%d CPUs)" % (
            request["cpus"], request["gpus"], gpus_units / terms["cpu"] * request["cpus"]))
    if terms.get("mem", 0) > gpus_units:
        over.append("%s GiB of memory exceed %d GPU(s)' share (%s GiB)" % (
            amount(request["memory_mib"] / 1024), request["gpus"],
            ("%.1f" % (gpus_units / terms["mem"] * request["memory_mib"] / 1024)).rstrip("0").rstrip(".")))
    if over:
        found["warning"] = "on %s each task bills %s units, worth %s GPU(s): its %s" % (
            billing["partition"], amount(billing["units"]), amount(found["as_gpus"]), "; its ".join(over))
    return found


def slurm_billing(machine, job):
    """What each task bills on its partition(s), read from the cluster (scontrol, read only):
    the partition's TRESBillingWeights and PriorityFlags, the units of this request, and for a GPU
    request whether its CPUs or memory make it bill more than its GPUs. Never refuses: a value the
    cluster does not give is reported as such."""
    partitions, request, uncounted = requested(job)
    script = "\n".join(machine["setup"] + ["command -v scontrol > /dev/null || { echo CLOUDSWEEP-NO-SCONTROL; exit; }",
                                           "scontrol -o show partition", "echo CLOUDSWEEP-CONFIG",
                                           "scontrol show config | grep -i '^PriorityFlags'"])
    try:
        done = ssh(machine, script)
    except Refusal as error:
        return [{"partition": name, "error": str(error)} for name in partitions or [None]]
    if "CLOUDSWEEP-NO-SCONTROL" in done.stdout.split():
        return [{"partition": name, "error": "scontrol is not on PATH after the setup lines"}
                for name in partitions or [None]]
    shown, _, config = done.stdout.partition("CLOUDSWEEP-CONFIG")
    found = {}
    for line in shown.splitlines():
        if line.startswith("PartitionName="):
            fields = settings_line(line)
            found[fields["PartitionName"]] = fields
    if not config.strip().lower().startswith("priorityflags"):  # not read: its rule cannot be applied
        return [{"partition": name, "error": "scontrol show config gave no PriorityFlags (exit %d)" % done.returncode}
                for name in partitions or [None]]
    flags = (config.strip().partition("=")[2]).strip()
    default = partitions is None
    if default:
        partitions = [name for name, fields in found.items() if fields.get("Default") == "YES"][:1] or [None]
    rows = []
    for name in partitions:
        if name not in found:
            rows.append({"partition": name, "error": "scontrol shows no partition %s (exit %d)"
                         % (name or "marked Default", done.returncode)})
            continue
        weights = found[name].get("TRESBillingWeights")
        row = dict(billing_units(weights, flags, request), partition=name, default=default, weights=weights,
                   priority_flags=flags, request=request)
        if "error" not in row:
            row.update(gpu_share(row, request))
            if uncounted:
                row["notes"] = row["notes"] + ["not counted: sbatch %s" % " ".join(uncounted)]
        rows.append(row)
    return rows


def billing_text(row):
    """One line for people: the units of a task on its partition, and how they were found."""
    where = (row.get("partition") or "the default partition") + (" (the default)" if row.get("default") else "")
    if row.get("error"):
        return "billing on %s: unknown (%s)" % (where, row["error"])
    if not row.get("weights"):
        return "billing on %s: %s unit(s) per task (no TRESBillingWeights: Slurm bills the allocated CPUs)" % (
            where, amount(row["units"]))
    text = "billing on %s: %s unit(s) per task, %s of %s (TRESBillingWeights %s)" % (
        where, amount(row["units"]), row["rule"],
        ", ".join("%s %s" % (name, amount(value)) for name, value in sorted(row["terms"].items())), row["weights"])
    if row.get("per_gpu"):
        text += "; worth %s GPU(s) at %s units per GPU" % (amount(row["as_gpus"]), amount(row["per_gpu"]))
    return text


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
        machine = load(repo, args.on, args.machines)
        if args.isolated:
            raise Refusal("--isolated reserves a whole AWS instance; on a machine, request [run] vcpus and "
                          "memory_mib (slurm) or set slots (ssh)")
        commit, recipe, files_in, commands, hours, vcpus, memory_mib, gpus = prepare(args, repo)
        if machine["kind"] == "ssh" and gpus and machine["gpus"] == 0:
            raise Refusal("these commands need GPUs; [%s] sets gpus = 0" % machine["name"])
        image = None
        if machine["container"] and machine["container"] != "apptainer":
            image = existing_image(machine)  # read only, so a dry run resolves it too: the build depends on it
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
               "setup": machine["setup"], "no_build": bool(args.no_build)}
        if image and image.get("sif"):
            job["container"] = {"key": image["key"], "sif": image["sif"], "stamp": image["stamp"]}
        elif image:
            job["container"] = {"key": image["key"], "check": None if image["tag"] else image["spec"]["check"]}
        workdir, script = machine["workdir"], None
        plan = {"submission": submission, "machine": machine["name"], "kind": machine["kind"],
                "host": machine["host"], "workdir": workdir, "commit": commit, "children": len(commands),
                "vcpus": vcpus, "memory_mib": memory_mib, "gpus": gpus, "hours": str(hours), "build": build,
                "no_build": bool(args.no_build), "image_digest": digest, "machines_file": machine["file"],
                "section": {key: value for key, value in machine.items() if key != "file"}}
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
            plan["billing"] = slurm_billing(machine, job)
            for row in plan["billing"]:
                print(billing_text(row) + ("\nwarning: " + row["warning"] if row.get("warning") else ""),
                      file=sys.stderr, flush=True)
        else:
            job.update(slots=machine["slots"], machine_gpus=machine["gpus"], systemd=machine["systemd"])
            plan["slots"] = ("one per %d GPU(s)" % gpus if gpus else machine["slots"] or
                             "one per %d CPU(s) of the machine" % vcpus)
        if args.dry_run:
            if args.json:  # one JSON document, the batch script in it
                print(json.dumps(dict(plan, dry_run=True, script=script), indent=2, sort_keys=True))
                return 0
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
            if not args.json:
                print("receipt: %s; check `cloudsweep status %s` before trying again" % (path, submission))
        if args.json:  # stdout stays one JSON document; with a receipt, the work may have been sent
            print(json.dumps({"error": str(error), "receipt": str(path) if path else None,
                              "submission": receipt["submission"] if receipt else None,
                              "submission_state": receipt["submission_state"] if receipt else None},
                             indent=2, sort_keys=True))
        return settings.fail(error)
    if args.json:
        print(json.dumps(dict(receipt, receipt=str(path), dry_run=False, script=script), indent=2, sort_keys=True))
        return 0
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


def receipt_of(repo, submission):
    """A submission's receipt, from this checkout or where the deployment's RECEIPTS keeps AWS's;
    None without one."""
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
            return receipt
    return None


def machine_of(repo, submission):
    """The name of the machine a submission went to (AWS receipts name none); local without a receipt."""
    receipt = receipt_of(repo, submission)
    return LOCAL if receipt is None else receipt.get("machine") or AWS


def machine_for(repo, submission, extra=None):
    """The settings a submission was made with: the section its receipt recorded, so that later
    commands need neither --machines nor an unchanged file; for an older receipt, the section of
    that name now; AWS for an AWS receipt; local without a receipt."""
    receipt = receipt_of(repo, submission)
    if receipt is None:
        return load(repo, LOCAL, extra)
    if not receipt.get("machine"):
        return AWS
    if isinstance(receipt.get("section"), dict):
        return dict(receipt["section"], name=receipt["machine"])
    return load(repo, receipt["machine"], extra)


def endpoint(machine):
    """What one remote call reaches: submissions with equal endpoints can share it."""
    return machine["kind"], machine["host"], machine["workdir"], tuple(machine["setup"])


def groups(repo, submissions, extra=None):
    """[(machine settings or AWS, submissions)], one group per endpoint, in the order given."""
    found = []
    for submission in submissions:
        machine = machine_for(repo, submission, extra)
        for known, members in found:
            if known == machine == AWS or (known != AWS and machine != AWS and endpoint(known) == endpoint(machine)):
                members.append(submission)
                break
        else:
            found.append((machine, [submission]))
    return found


def nodes_text(nodes):
    return ", ".join(nodes[:4]) + (" and %d more" % (len(nodes) - 4) if len(nodes) > 4 else "")


def queue_text(queue):
    """The tasks in Slurm's queue, one phrase per state: where and since when they run, or why
    they wait and Slurm's estimated start (times in UTC)."""
    groups = {}
    for task in queue:
        groups.setdefault(task["state"], []).append(task)
    parts = []
    for state in sorted(groups, key=lambda name: (name != "RUNNING", name)):
        tasks = groups[state]
        nodes = sorted({task["nodes"] for task in tasks if task["nodes"]})
        starts = sorted(task["start"] for task in tasks if task["start"])
        text = "%d %s" % (len(tasks), state.lower())
        if state == "PENDING":
            reasons = sorted({task["reason"] for task in tasks if task["reason"]})
            text += " (%s)" % ", ".join(reasons) if reasons else ""
            text += ", estimated start %s" % starts[0] if starts else ", no start estimate"
            text += " on %s" % nodes_text(nodes) if nodes else ""
        else:
            text += " on %s" % nodes_text(nodes) if nodes else ""
            text += " since %s" % starts[0] if starts else ""
        parts.append(text)
    return "; ".join(parts)


def clock(seconds):
    return "%d:%02d:%02d" % (seconds // 3600, seconds // 60 % 60, seconds % 60)


def amount(value):
    """A number for people: 0, 0.073, 1037.8."""
    if not value:
        return "0"
    return "%.2g" % value if abs(value) < 0.001 else ("%.3f" % value).rstrip("0").rstrip(".")


def with_totals(accounting):
    """sacct's tasks with their sums: elapsed seconds; GPU-hours, the allocated GPUs times the
    elapsed time; and billing unit-hours, Slurm's billing units times the elapsed time."""
    tasks = accounting.get("tasks")
    if tasks is None:
        return accounting
    states, billed = {}, [task for task in tasks if task["billing"] is not None]
    for task in tasks:
        states[task["state"]] = states.get(task["state"], 0) + 1
    totals = {"states": states, "elapsed_s": sum(task["elapsed_s"] for task in tasks),
              "gpu_hours": round(sum(task["gpus"] * task["elapsed_s"] for task in tasks) / 3600, 6),
              "billing_unit_hours": round(sum(task["billing"] * task["elapsed_s"] for task in billed) / 3600, 6)
              if billed else None}
    return dict(accounting, totals=totals)


def accounting_text(accounting):
    """One line of Slurm's accounting: states, elapsed time, GPU-hours and billing."""
    if accounting.get("error"):
        return "Slurm accounting unavailable: %s" % accounting["error"]
    tasks, totals = accounting["tasks"], accounting["totals"]
    if not tasks:
        return "Slurm accounting: no record of the job yet"
    text = "Slurm accounting: %s; elapsed %s" % (", ".join("%d %s" % (count, state) for state, count in
                                                           sorted(totals["states"].items())),
                                                clock(totals["elapsed_s"]))
    ran = [task for task in tasks if task["start"]]
    if len(tasks) == 1 and ran:
        text += " (%s to %s%s)" % (ran[0]["start"], ran[0]["end"] or "now",
                                   " on " + ran[0]["nodes"] if ran[0]["nodes"] else "")
    elif len(tasks) > 1:
        text += " in all"
    text += "; %s GPU-hours" % amount(totals["gpu_hours"])
    units = sorted({task["billing"] for task in ran if task["billing"] is not None})
    if totals["billing_unit_hours"] is not None:
        text += "; billing %s%s unit-hours" % ("%s unit(s) per task, " % amount(units[0]) if len(units) == 1 else "",
                                              amount(totals["billing_unit_hours"]))
    return text


def describe(submission, row):
    children = " children %s" % json.dumps(row["children"], sort_keys=True) if row.get("children") else ""
    job = ""
    if row.get("job") and row.get("kind") == "slurm":
        job = " (Slurm job %s%s)" % (row["job"], ": " + queue_text(row["queue"]) if row.get("queue") else "")
    elif row.get("job"):
        job = " (pool process %s)" % row["job"]
    line = "  %s %s%s%s%s" % (submission, row.get("state", "UNKNOWN"), children, job,
                              " " + row["error"] if row.get("error") else "")
    return line + ("\n    " + accounting_text(row["accounting"]) if row.get("accounting") else "")


def call_status(machine, *submissions):
    """remote.py status, with the sums of each Slurm submission's accounting added."""
    report = call(machine, "status", *submissions)
    for row in report.values():
        if isinstance(row, dict) and isinstance(row.get("accounting"), dict):
            row["accounting"] = with_totals(row["accounting"])
    return report


def status(args, groups):
    """Print each group's submissions (groups: [(machine settings, submissions)], no submissions
    for all of this checkout's on that machine); one JSON object for all of them with --json."""
    repo, reports = project.repository(), {}
    for machine, wanted in groups:
        wanted = wanted or receipts(repo, machine["name"])
        report = call_status(machine, *wanted) if wanted else {}
        reports.update(report)
        if args.json:
            continue
        print("%s (%s, %s)" % (machine["name"], machine["kind"], at(machine, machine["workdir"])))
        for submission in wanted:
            print(describe(submission, report.get(submission, {})))
        if not wanted:
            print("  no submissions recorded in this checkout")
    if args.json:
        print(json.dumps(reports, indent=2, sort_keys=True))
    return 0


def cancel(args, groups):
    """Stop each group's submissions (groups: [(machine settings, submissions)], none for all of this
    checkout's on that machine), every group's before any wait; then with --wait poll them until
    they have stopped, for at most --wait minutes in all. A machine that refuses does not keep the
    others from being stopped; its refusal is raised at the end."""
    repo, sent, refusals, failed = project.repository(), [], [], False
    for machine, wanted in groups:
        wanted = list(wanted) or receipts(repo, machine["name"])
        try:
            report = call(machine, "cancel", *wanted) if wanted else {}
        except Refusal as error:
            refusals.append(str(error))
            continue
        for submission in wanted:
            if args.submission or report.get(submission) != "not running":
                print("  %s: %s" % (submission, report.get(submission)), flush=True)
        failed = failed or any(str(value).startswith("error") for value in report.values())
        sent.append((machine, wanted))
    deadline = time.monotonic() + 60 * args.wait
    while args.wait and sent:
        busy = []
        for machine, wanted in sent:
            found = call_status(machine, *wanted)
            left = [s for s in wanted if found.get(s, {}).get("state") in ACTIVE]
            if left:
                busy.append((machine, left))
        sent = busy
        if not sent:
            break
        if time.monotonic() > deadline:
            refusals.append("still running after --wait: %s" % ", ".join(s for _, left in sent for s in left))
            break
        time.sleep(30)
    if refusals:
        raise Refusal("; ".join(refusals))
    return 1 if failed else 0


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
        row = call_status(machine, submission).get(submission, {})
        print(describe(submission, row).strip(), flush=True)
        if row.get("state") not in ACTIVE:
            return row
        if time.monotonic() > deadline:
            raise Refusal("still %s after --wait %d minutes" % (row.get("state"), minutes))
        time.sleep(60)


def fetch_results(args, machine):
    """Copy the submission from the machine's store with rsync, then verify it with fetch's checks."""
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
    if machine["kind"] == "slurm":
        # What Slurm accounted for the job, beside the verified results (not part of their verification).
        row = call_status(machine, submission).get(submission, {})
        if row.get("accounting"):
            summary["accounting"] = dict(row["accounting"], job=row.get("job"))
            fetch.no_symlinks(destination / submission / "fetch.json")
            (destination / submission / "fetch.json").write_bytes(core.pretty(summary))
            print(accounting_text(summary["accounting"]))
    fetch.print_summary(summary, destination)
    return 0 if summary["complete"] else 1


def doctor(repo, name, row, extra=None):
    """cloudsweep doctor's machine checks: reach the machine and report what cloudsweep needs there."""
    machine = load(repo, name, extra)
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
