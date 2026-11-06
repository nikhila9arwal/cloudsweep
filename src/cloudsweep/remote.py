#!/usr/bin/env python3
"""remote.py submit|child|pool|status|cancel|list|check WORKDIR [SUBMISSION ...]

cloudsweep copies this file, worker.py and core.py to WORKDIR/code/ on a machine and runs it
there (over the system ssh, or directly on this host), in the machine's own environment after
its setup lines. It needs python3 >= 3.6 and git. WORKDIR may be relative to the home directory.

WORKDIR holds:
  store/            the file:// store: submissions, inputs and every attempt's results
  builds/KEY/       a revision built on this machine (KEY names the commit, recipe and setup)
  images/KEY/       base.sif, when the machine runs commands in a container (container = apptainer)
  jobs/SUBMISSION/  job.json and job.sh (written by cloudsweep), state.json, logs/INDEX.txt
  code/HASH/        these three files

  submit  build the revision once, then start the work: one sbatch --array job (slurm) or a
          detached pool (ssh); prints its result as one CLOUDSWEEP-RESULT line
  child   run one command through the worker in a fresh copy of the built checkout; the
          index is SLURM_ARRAY_TASK_ID unless given
  pool    run every command, one per slot (ssh machines)
  status  report each submission's job and its commands
  cancel  stop each submission's job, or keep it from starting
  list    the store objects of a submission, for fetch
  check   what this machine provides (cloudsweep doctor --on)
"""

import sys

# Checked before anything newer is imported; this file parses on older Pythons too.
if sys.version_info < (3, 6):
    sys.exit("cloudsweep: python3 here is %d.%d after the machine's setup lines; cloudsweep needs 3.6 or "
             "newer: add a setup line in machines.ini that provides one" % sys.version_info[:2])

import json  # noqa: E402
import os  # noqa: E402
import platform  # noqa: E402
import shlex  # noqa: E402
import shutil  # noqa: E402
import signal  # noqa: E402
import socket  # noqa: E402
import subprocess  # noqa: E402
import tempfile  # noqa: E402
import time  # noqa: E402
from pathlib import Path  # noqa: E402

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import core  # noqa: E402
import worker  # noqa: E402

RESULT = "CLOUDSWEEP-RESULT "
SLURM_RUNNING = ("RUNNING", "COMPLETING")
NO_ATTEMPT = object()


def emit(value):
    print(RESULT + json.dumps(value, sort_keys=True), flush=True)


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=str(path.parent), prefix=".part-")
    with os.fdopen(handle, "wb") as stream:
        stream.write(core.pretty(value))
    os.replace(temporary, str(path))


def read_json(path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def run(argv, **kwargs):
    return subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          universal_newlines=True, **kwargs)


class Site:
    """One submission's places in WORKDIR. Only submit and the pool write state.json; cancel leaves
    its own marker, so neither can overwrite the other's record."""

    def __init__(self, workdir, submission):
        core.require(core.SUBMISSION_RE.fullmatch(submission), "invalid submission id %r" % submission)
        self.root, self.submission = Path(workdir).resolve(), submission
        self.store = self.root / "store"
        self.dir = self.root / "jobs" / submission
        self.logs = self.dir / "logs"
        self.job = read_json(self.dir / "job.json")
        core.require(isinstance(self.job, dict) and self.job.get("submission") == submission,
                     "no job record %s" % (self.dir / "job.json"))
        self.build = self.root / "builds" / self.job["build"]

    def state(self):
        return read_json(self.dir / "state.json") or {}

    def update_state(self, **changes):
        state = self.state()
        state.update(changes)
        write_json(self.dir / "state.json", state)

    def cancelled(self):
        return (self.dir / "cancelled").exists()


def lock_owner(lock):
    try:
        return (lock / "owner").read_text().strip()
    except OSError:
        return None


def my_turn(site, lock, made, what):
    """Whether this submission makes `what` (it took lock when cloudsweep prepared workdir and
    uploaded what it needs); a concurrent one waits, then finds it made. Marks the lock once
    making starts, so cloudsweep never releases a lock a maker holds."""
    if not made.exists() and lock_owner(lock) != site.submission:
        print("cloudsweep: submission %s is making %s; waiting (remove %s if nothing is)" % (
            lock_owner(lock), what, lock), file=sys.stderr, flush=True)
        deadline = time.monotonic() + 3 * 3600
        while lock.exists() and time.monotonic() < deadline:
            time.sleep(5)
    if made.exists():
        if lock_owner(lock) == site.submission:  # cloudsweep sent it already made (a SIF converted there)
            shutil.rmtree(str(lock), ignore_errors=True)
        return False
    core.require(lock_owner(lock) == site.submission, "the other submission's %s did not finish; submit again" % what)
    (lock / "building").touch()
    return True


def apptainer():
    found = shutil.which("apptainer") or shutil.which("singularity")
    core.require(found, "container = apptainer, but this machine has neither apptainer nor singularity")
    return found


def image_path(site):
    return site.root / "images" / site.job["container"]["key"] / "base.sif"


def in_container(site, work, env, store=False):
    """The image's worker under apptainer exec, as Batch runs it: only these variables, work as
    /work and the home directory (apptainer sets HOME itself), the store at /store, and the host's
    GPUs (--nv) for GPU commands."""
    argv = [apptainer(), "exec", "--cleanenv", "--home", "%s:/work" % work]
    if store:
        argv += ["--bind", "%s:/store" % site.store]
    if site.job["gpus"]:
        argv.append("--nv")
    for name, value in sorted(env.items()):
        argv += ["--env", "%s=%s" % (name, value)]
    return argv + [str(image_path(site)), "python3", "/opt/cloudsweep/worker.py"]


def stream(argv, log, **kwargs):
    """Run argv with its output in log and on stderr (which ssh carries to the submitting host)."""
    with open(str(log), "ab") as out:
        process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, **kwargs)
        for line in process.stdout:
            out.write(line)
            sys.stderr.buffer.write(line)
            sys.stderr.flush()
        return process.wait()


def ensure_image(site):
    """The SIF commands run in, made once per image: converted from the docker archive cloudsweep
    built and sent, or, when the submitting host could not build for this machine, built here
    from the Dockerfile it sent. Returns whether this submission made it."""
    sif = image_path(site)
    lock = sif.parent.parent / (sif.parent.name + ".lock")
    if not my_turn(site, lock, sif, "the container image"):
        return False
    log, part = sif.parent / "image.log", sif.parent / "base.sif.part"
    try:
        archive = sif.parent / "base.tar"
        if not archive.exists():
            archive = build_image(site, sif.parent, log)
        print("cloudsweep: converting the image to %s (once per image)" % sif, file=sys.stderr, flush=True)
        code = stream([apptainer(), "build", "--force", str(part), "docker-archive://" + str(archive)], log)
        core.require(code == 0, "apptainer build failed (exit %d); its log is %s" % (code, log))
        check = site.job["container"].get("check")
        if check:  # an image built on the submitting host was checked there
            code = stream([apptainer(), "exec", str(part), "/bin/sh", "-c", check], log)
            core.require(code == 0, "the toolchain check failed in the image: %s" % check)
        os.replace(str(part), str(sif))
        archive.unlink()
    finally:
        shutil.rmtree(str(lock), ignore_errors=True)
    return True


def build_image(site, directory, log):
    """The last resort: build the image on this machine, with its podman or docker, from the
    toolchain Dockerfile and worker layer cloudsweep sent; returns a docker archive of it."""
    engine = shutil.which("podman") or shutil.which("docker")
    core.require(engine, "the submitting host could not build the image for this machine, and this machine has "
                 "neither podman nor docker to build it")
    base = [engine]
    if os.path.basename(engine) == "podman":  # storage on local disk, as cloudsweep uses it
        root = Path(tempfile.gettempdir()) / ("cloudsweep-podman-%d" % os.getuid())
        base += ["--root", str(root / "storage"), "--runroot", str(root / "run"),
                 "--storage-opt", "overlay.ignore_chown_errors=true"]
    spec, archive = site.job["container"], directory / "base.tar"
    toolchain, tag = "localhost/cloudsweep-%s-toolchain:latest" % spec["key"], "localhost/cloudsweep-%s:latest" % spec["key"]
    print("cloudsweep: building the container image here, since the submitting host could not",
          file=sys.stderr, flush=True)
    for argv in (base + ["build", "-t", toolchain, "-f", str(directory / "Dockerfile"), str(directory / "context")],
                 base + ["build", "-t", tag, str(directory / "worker")],
                 base + ["save"] + (["--format", "docker-archive"] if os.path.basename(engine) == "podman" else [])
                 + ["-o", str(archive), tag]):
        code = stream(argv, log)
        core.require(code == 0, "%s failed (exit %d); its log is %s" % (" ".join(argv[:2]), code, log))
    run(base + ["rmi", tag, toolchain])
    return archive


def build(site):
    """Build the revision once in this machine's environment (or its container image); later
    submissions reuse it. A revision is built where it runs, since programs may depend on where
    they were built, so it is never built aside and moved."""
    record, lock = site.build / "build.json", site.root / "builds" / (site.job["build"] + ".lock")
    if not my_turn(site, lock, record, "this revision's build"):
        return False
    log = site.build / "build.log"
    print("cloudsweep: building %s here (once per commit and setup); log %s" % (site.job["commit"][:12], log),
          file=sys.stderr, flush=True)
    try:
        if site.job.get("container"):
            argv = in_container(site, site.build, {"GIT_OPTIONAL_LOCKS": "0"}) + ["build", "--keep-modes"]
        else:
            argv = [sys.executable, "-c", "import sys; sys.path.insert(0, %r); import worker; "
                    "sys.exit(worker.build_mode(%r, writable=False))" % (str(HERE), str(site.build))]
        with open(str(log), "wb") as stream_:
            process = subprocess.Popen(argv, stdin=subprocess.DEVNULL,
                                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, cwd=str(site.build),
                                       env=dict(os.environ, GIT_OPTIONAL_LOCKS="0"))
            for line in process.stdout:
                stream_.write(line)
                sys.stderr.buffer.write(line)
                sys.stderr.flush()
            process.wait()
    finally:
        shutil.rmtree(str(lock), ignore_errors=True)
    core.require(process.returncode == 0, "the build failed (exit %d); its log is %s" % (process.returncode, log))
    return True


def slurm_job(site, state):
    """The array's job id: recorded at submission, else found by name (a lost submit response)."""
    if state.get("slurm_job"):
        return state["slurm_job"]
    done = run(["squeue", "-h", "-n", site.submission + "-run", "-o", "%F"])
    core.require(done.returncode == 0, "squeue failed: %s" % done.stderr.strip())
    found = done.stdout.split()
    return found[0] if found else None


def start_array(site):
    """One Slurm array job, one task per command; its script was written by cloudsweep and runs in
    WORKDIR (Slurm starts jobs in the directory sbatch ran in)."""
    site.logs.mkdir(parents=True, exist_ok=True)
    done = run(["sbatch", "--parsable"] + site.job["sbatch"] + [str(site.dir / "job.sh")], cwd=str(site.root))
    core.require(done.returncode == 0, "sbatch failed: %s" % (done.stderr.strip() or "exit %d" % done.returncode))
    job = done.stdout.strip().split(";", 1)[0]
    core.require(job.isdigit(), "sbatch printed %r, not a job id" % done.stdout.strip())
    site.update_state(slurm_job=job, submitted=core.now_iso())
    return {"slurm_job": job}


def visible_gpus(count):
    """GPU indices the pool may hand out: CUDA_VISIBLE_DEVICES when set, else 0..count-1, with
    count from nvidia-smi when machines.ini does not give it."""
    if "CUDA_VISIBLE_DEVICES" in os.environ:  # set, even empty: only these (empty hides every GPU)
        listed = [device.strip() for device in os.environ["CUDA_VISIBLE_DEVICES"].split(",") if device.strip()]
        return listed if count is None else listed[:count]
    if count is None:
        try:
            found = run(["nvidia-smi", "-L"]).stdout
        except OSError:
            found = ""
        count = sum(1 for line in found.splitlines() if line.startswith("GPU "))
    return [str(index) for index in range(count)]


def pool_slots(job):
    """One slot per [run] gpus GPUs; for CPU commands, the configured slots, else as many as the
    machine's CPUs hold commands of [run] vcpus each."""
    if not job["gpus"]:
        count = job["slots"] or max(1, len(os.sched_getaffinity(0)) // max(1, job.get("vcpus", 1)))
        return [{} for _ in range(count)]
    devices, size = visible_gpus(job["machine_gpus"]), job["gpus"]
    core.require(len(devices) >= size, "each command needs %d GPU(s); this machine has %d" % (size, len(devices)))
    return [{"CUDA_VISIBLE_DEVICES": ",".join(devices[k * size:(k + 1) * size])} for k in range(len(devices) // size)]


def start_pool(site):
    """Start the pool detached from this session; it records its own process in state.json."""
    slots = pool_slots(site.job)
    site.logs.mkdir(parents=True, exist_ok=True)
    write_json(site.dir / "pool.json", {"slots": slots})
    log, script = site.dir / "pool.log", site.dir / "pool.sh"
    # Setup lines run in the home directory, as in every other remote step; a script file keeps
    # systemd-run from expanding their $ signs.
    script.write_text("\n".join(["exec >> %s 2>&1 < /dev/null" % shlex.quote(str(log)), "cd"] + site.job["setup"] +
                                 ["exec %s %s pool %s %s" % (shlex.quote(sys.executable),
                                                             shlex.quote(str(HERE / "remote.py")),
                                                             shlex.quote(str(site.root)), site.submission)]) + "\n")
    command = ["/bin/bash", "-l", str(script)]
    result = {"slots": len(slots), "gpus_per_slot": site.job["gpus"]}
    if site.job["systemd"]:
        # This machine stops a user's processes at logout; a user unit outlives the session.
        done = run(["systemd-run", "--user", "--unit", "cloudsweep-" + site.submission, "--collect", "--quiet",
                    "--"] + command)
        core.require(done.returncode == 0, "systemd-run failed: %s" % (done.stderr or done.stdout).strip())
        if run(["loginctl", "show-user", os.environ.get("USER", ""), "-p", "Linger"]).stdout.strip() != "Linger=yes":
            result["warning"] = ("lingering is off for this user, so systemd stops the pool when your last session "
                                 "on this machine ends; `loginctl enable-linger` keeps it running")
    else:
        subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         cwd=str(site.root), start_new_session=True)
    for _ in range(30):
        state = site.state()
        if "pid" in state:
            result.update(pid=state["pid"], host=state["host"])
            return result
        time.sleep(0.5)
    tail = log.read_text(errors="replace")[-2000:] if log.is_file() else ""
    raise ValueError("the pool did not start within 15 s; %s ends:\n%s" % (log, tail))


def stop(site):
    """Stop the submission's work, or mark it so that a submit still building does not start it;
    finished work is left as it is. Returns what was done."""
    state = site.state()
    if site.job["kind"] == "slurm":
        job = slurm_job(site, state)
        if not job:
            (site.dir / "cancelled").touch()  # a submit still building sees it and does not start
            return "stop recorded; it will not start"
        if not slurm_tasks(job):
            return "not running"
        done = run(["scancel", job])
        if done.returncode and not slurm_tasks(job):  # it finished meanwhile
            return "not running"
        core.require(done.returncode == 0, "scancel failed: %s" % (done.stderr or done.stdout).strip())
        (site.dir / "cancelled").touch()
        return "stop requested (scancel %s)" % job
    if state and not pool_alive(site, state):
        return "not running"
    (site.dir / "cancelled").touch()
    if not state:
        return "stop recorded; it will not start"
    os.killpg(state["pgid"], signal.SIGTERM)  # the pool and the commands it started
    return "stop requested (pool process %d)" % state["pid"]


def submit(site):
    """Build if needed, then start the work, unless the submission was cancelled meanwhile."""
    result = {}
    if site.job.get("container"):
        apptainer()  # before taking any lock up
        result["image"] = ensure_image(site)
    result["built"] = build(site)
    if site.cancelled():
        result["cancelled"] = True
    else:
        result.update(start_array(site) if site.job["kind"] == "slurm" else start_pool(site))
        if site.cancelled():  # cancelled while it started
            result["cancelled"] = stop(site)
    emit(result)
    return 0


def on_stop(signum, frame):
    sys.exit(128 + signum)


def child(site, index=None):
    """One command, as one AWS Batch child runs in its own container: the worker gets a fresh copy
    of the built checkout, since it places inputs there and the command may write there too."""
    for signum in (signal.SIGTERM, signal.SIGINT):  # until the worker takes over, stop with cleanup
        signal.signal(signum, on_stop)
    if index is None:
        index = int(os.environ["SLURM_ARRAY_TASK_ID"])
    core.require(0 <= index < site.job["count"], "index %d is outside the submission" % index)
    if site.job["kind"] == "slurm":
        label = "slurm:%s_%d" % (os.environ.get("SLURM_ARRAY_JOB_ID", "?"), index)
        attempt = 1 + int(os.environ.get("SLURM_RESTART_COUNT") or 0)
    else:
        label, attempt = "pool:%s" % site.job["machine"], 1
    scratch = Path(tempfile.mkdtemp(prefix="cloudsweep-%s-%d-" % (site.submission, index)))
    try:
        if (site.build / "build.json").is_file():  # otherwise the worker records the missing build
            shutil.copytree(str(site.build / "src"), str(scratch / "src"), symlinks=True)
            for name in ("build.json", core.RECIPE):
                shutil.copy2(str(site.build / name), str(scratch / name))
        # The variables `cloudsweep try` sets for a local run; started.json records them.
        env = dict(IMAGE_DIGEST=site.job["image_digest"], AWS_BATCH_JOB_ARRAY_INDEX=str(index),
                   AWS_BATCH_JOB_ATTEMPT=str(attempt), AWS_BATCH_JOB_ID=label)
        if not site.job.get("container"):
            os.environ.update(env, CLOUDSWEEP_WORK=str(scratch), CLOUDSWEEP_STORE="file://" + str(site.store))
            return worker.main(["run", site.submission])
        env.update(CLOUDSWEEP_STORE="file:///store")
        env.update({name: os.environ[name] for name in ("CLOUDSWEEP_TIME_LIMIT", "CUDA_VISIBLE_DEVICES")
                    if name in os.environ})
        started, stops = [], []

        def forward(signum, frame):  # apptainer passes it to the worker, which uploads what it has
            stops.append(signum)
            for process in started:
                process.send_signal(signal.SIGTERM)
        for signum in (signal.SIGTERM, signal.SIGINT):
            signal.signal(signum, forward)
        started.append(subprocess.Popen(in_container(site, scratch, env, store=True) + ["run", site.submission],
                                        stdin=subprocess.DEVNULL))
        if stops:  # it came while the container started
            started[0].send_signal(signal.SIGTERM)
        return started[0].wait()
    finally:
        shutil.rmtree(str(scratch), ignore_errors=True)


def pool(site):
    """Run every command, at most one per slot at a time. SIGTERM stops new starts and passes on to
    the running commands, whose workers then upload what they have. running.json names the
    commands running now."""
    slots = json.loads((site.dir / "pool.json").read_text())["slots"]
    site.update_state(pid=os.getpid(), pgid=os.getpgid(0), host=socket.gethostname(), started=core.now_iso())
    pending, running, codes, stopped = list(range(site.job["count"])), {}, {}, []

    def stop_all(signum, frame):
        stopped.append(signum)
        for _, process in running.values():
            try:
                process.send_signal(signal.SIGTERM)
            except OSError:
                pass
    for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(signum, stop_all)
    write_json(site.dir / "running.json", [])
    while (pending and not stopped) or running:
        changed = False
        for slot in range(len(slots)):
            if slot in running or not pending or stopped:
                continue
            index = pending.pop(0)
            env = dict(os.environ, CLOUDSWEEP_TIME_LIMIT=str(site.job["seconds"]))
            env.update(slots[slot])
            with open(str(site.logs / ("%d.txt" % index)), "ab") as log:
                process = subprocess.Popen([sys.executable, str(HERE / "remote.py"), "child", str(site.root),
                                            site.submission, str(index)], env=env, stdin=subprocess.DEVNULL,
                                           stdout=log, stderr=subprocess.STDOUT, cwd=str(site.root))
            running[slot] = (index, process)
            if stopped:  # the stop arrived while this command was starting
                process.send_signal(signal.SIGTERM)
            changed = True
        for slot, (index, process) in list(running.items()):
            if process.poll() is not None:
                codes[str(process.returncode)] = codes.get(str(process.returncode), 0) + 1
                del running[slot]
                changed = True
        if changed:
            write_json(site.dir / "running.json", sorted(index for index, _ in running.values()))
        time.sleep(1)
    site.update_state(finished=core.now_iso(), stopped=bool(stopped), exit_codes=codes, never_started=len(pending))
    return 0


def latest_statuses(site):
    """index -> the latest attempt's manifest status, or None while that attempt has no manifest."""
    found, base = {}, site.store / "runs" / site.submission
    if not base.is_dir():
        return found
    for directory in base.iterdir():
        attempts = [path for path in directory.iterdir() if core.ATTEMPT_RE.fullmatch(path.name)] \
            if directory.name.isdigit() else []
        if attempts:
            last = max(attempts, key=lambda path: core.attempt_order(path.name))
            manifest = last / "manifest.json"
            found[int(directory.name)] = ((read_json(manifest) or {}).get("status") or "invalid manifest"
                                          if manifest.exists() else None)
    return found


def slurm_tasks(job):
    """index -> Slurm state of the array's tasks still known to the queue."""
    done = run(["squeue", "-h", "-r", "-j", job, "-o", "%K|%T"])
    # Older Slurm rejects a job id that has left the queue instead of listing nothing.
    core.require(done.returncode == 0 or "Invalid job id" in done.stderr, "squeue failed: %s" % done.stderr.strip())
    tasks = {}
    for line in done.stdout.splitlines():
        index, _, state = line.strip().partition("|")
        if index.isdigit():
            tasks[int(index)] = state
    return tasks


def pool_alive(site, state):
    """Whether this submission's pool still runs: an unfinished record whose process is that pool
    (a process id can be reused once the pool has ended)."""
    if "finished" in state or "pid" not in state:
        return False
    try:
        with open("/proc/%d/cmdline" % state["pid"], "rb") as stream:
            argv = stream.read().split(b"\0")
    except (OSError, TypeError):
        return False
    return b"pool" in argv and site.submission.encode() in argv


def child_state(status, queued, pool_active):
    """What one command is doing. status is its latest attempt's manifest status (None while it has
    none, NO_ATTEMPT before it starts); queued is its Slurm state, or RUNNING for an ssh pool's
    running command; pool_active says whether an ssh pool still runs."""
    if queued in SLURM_RUNNING:
        return "running"
    if queued is not None or (status is NO_ATTEMPT and pool_active):
        return "waiting"
    if status is None:
        return "lost (started, no completion manifest)"
    return "not run" if status is NO_ATTEMPT else status


def site_status(site):
    state, statuses = site.state(), latest_statuses(site)
    report = {"kind": site.job["kind"], "commands": site.job["count"]}
    pool_active, queued = False, {}
    if site.job["kind"] == "slurm":
        job = slurm_job(site, state)
        queued = slurm_tasks(job) if job else {}
        active = bool(queued)
        report.update(job=job, state=("RUNNING" if any(v in SLURM_RUNNING for v in queued.values()) else
                                      "PENDING" if active else "FINISHED"))
        started = bool(job)
    else:
        if state.get("host") and state["host"] != socket.gethostname():
            report.update(state="UNKNOWN (the pool runs on %s, not here)" % state["host"])
            return report
        active = pool_active = pool_alive(site, state)
        if active:
            queued = {index: "RUNNING" for index in read_json(site.dir / "running.json") or []}
        report.update(job=state.get("pid"), state=("RUNNING" if active else "FINISHED" if "finished" in state
                                                   else "STOPPED (the pool ended early; see pool.log)"))
        started = bool(state)
    if not started and not statuses:
        report["state"] = "NOT STARTED (its submit did not finish; see the receipt)"
    unfinished = len(statuses) < site.job["count"] or any(status != "succeeded" for status in statuses.values())
    if site.cancelled() and not active and unfinished:  # a stop that came too late changed nothing
        report["state"] = "CANCELLED"
    counts = {}
    for index in range(site.job["count"]):
        name = child_state(statuses.get(index, NO_ATTEMPT), queued.get(index), pool_active)
        counts[name] = counts.get(name, 0) + 1
    report["children"] = counts
    return report


def status(root, submissions):
    report = {}
    for submission in submissions:
        try:
            report[submission] = site_status(Site(root, submission))
        except (OSError, ValueError, KeyError) as error:
            report[submission] = {"state": "UNKNOWN", "error": str(error)}
    emit(report)
    return 0


def cancel(root, submissions):
    report = {}
    for submission in submissions:
        try:
            report[submission] = stop(Site(root, submission))
        except (OSError, ValueError, KeyError) as error:
            report[submission] = "error: %s" % error
    emit(report)
    return 0


def listing(root, submission):
    """The submission's objects in the store, as an S3 listing would give them to fetch."""
    core.require(core.SUBMISSION_RE.fullmatch(submission), "invalid submission id %r" % submission)
    store, found = Path(root).resolve() / "store", []
    for prefix in ("submissions/%s" % submission, "runs/%s" % submission):
        base = store / prefix
        for path in sorted(base.rglob("*")) if base.is_dir() else []:
            if path.is_file() and not path.name.startswith(".part-"):
                found.append([path.relative_to(store).as_posix(), path.stat().st_size])
    emit({"files": found})
    return 0


def check(root, kind, gpus_wanted, container):
    """What this machine provides, for cloudsweep doctor --on."""
    rows = [["python3", True, platform.python_version()]]
    if container:
        found = shutil.which("apptainer") or shutil.which("singularity")
        rows.append(["apptainer", bool(found), found or "missing (container = apptainer)"])
    for tool in ("git", "rsync") + (("sbatch", "squeue", "scancel") if kind == "slurm" else ()):
        rows.append([tool, bool(shutil.which(tool)), shutil.which(tool) or "missing"])
    rows.append(["cpus", True, "%d usable here, %s" % (len(os.sched_getaffinity(0)), platform.machine())])
    if kind == "ssh" or gpus_wanted:
        count = len(visible_gpus(None))
        rows.append(["gpus", count > 0 or not gpus_wanted, "%d visible" % count if kind == "ssh"
                     else "%d on this login node (compute nodes get theirs from Slurm)" % count])
    workdir = Path(root).resolve()
    try:
        workdir.mkdir(mode=0o700, parents=True, exist_ok=True)
        free = shutil.disk_usage(str(workdir)).free
        rows.append(["workdir", os.access(str(workdir), os.W_OK), "%s (%.1f GiB free)" % (workdir, free / 2 ** 30)])
    except OSError as error:
        rows.append(["workdir", False, str(error)])
    emit({"rows": rows, "host": socket.gethostname()})
    return 0


def main(argv):
    actions = ("submit", "child", "pool", "status", "cancel", "list", "check")
    if len(argv) < 2 or argv[0] not in actions:
        print(__doc__, file=sys.stderr)
        return 2
    action, root, rest = argv[0], argv[1], argv[2:]
    try:
        if action == "status":
            return status(root, rest)
        if action == "cancel":
            return cancel(root, rest)
        if action == "list":
            return listing(root, rest[0])
        if action == "check":
            return check(root, rest[0], rest[1] == "1", rest[2] == "1")
        site = Site(root, rest[0])
        if action == "child":
            return child(site, int(rest[1]) if len(rest) > 1 else None)
        if action == "pool":
            return pool(site)
        # A dropped connection must still release the build lock (build's finally).
        signal.signal(signal.SIGHUP, on_stop)
        return submit(site)
    except (OSError, ValueError, KeyError, IndexError, subprocess.SubprocessError) as error:
        if action in ("submit", "list", "check"):
            emit({"error": str(error)})
        print("cloudsweep: %s" % error, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
