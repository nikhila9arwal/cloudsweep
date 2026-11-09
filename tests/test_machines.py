"""Sweeps on machines from machines.ini and on this host, end to end: fake ssh runs each remote
command here (so real rsync works through it), and fake sbatch runs an array's tasks in order,
as Slurm would."""

import io
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from helpers import PACKAGE, make_repo

from cloudsweep import cli, core, machines, project

sys.path.insert(0, str(PACKAGE))
import remote  # noqa: E402

INI = """[project]
name = demo
[image]
dockerfile = cloud/Dockerfile
[build]
command = mkdir -p build && cp sim.sh build/sim
binary = build/sim
[run]
hours = 0.1
"""
SIM = """#!/bin/sh
# usage: sim OUT INDEX INPUT
echo "$2 $(cat "$3")" > "$1/result.txt"
echo "${CUDA_VISIBLE_DEVICES:-none}" > "$1/gpus.txt"
echo "${FROM_SETUP:-no}" > "$1/setup.txt"
"""
MACHINES = """[cluster]
kind = slurm
host = fakehost
workdir = {root}/cluster
sbatch = --account=acct
cpu_sbatch = --partition=cpu
gpu_sbatch = --partition=gpu
gpu_type = h100
setup = source cs-setup.sh

[box]
kind = ssh
host = fakehost
workdir = {root}/box
slots = 2

[gpubox]
kind = ssh
host = fakehost
workdir = {root}/gpubox
gpus = 2

[broken]
kind = ssh
host = fakehost
workdir = {root}/broken
setup = false
"""
FAKE_SSH = """#!{python}
import os, sys
args = sys.argv[1:]
while args[0].startswith("-"):
    if args.pop(0) in ("-l", "-p", "-o"):
        args.pop(0)
host = args.pop(0)
with open(os.environ["FAKE_LOG"], "a") as log:
    log.write("ssh %s\\n" % host)
os.chdir(os.environ["HOME"])
os.execv("/bin/sh", ["/bin/sh", "-c", " ".join(args)])
"""
FAKE_SBATCH = """#!{python}
import json, os, re, subprocess, sys
flags, script = sys.argv[1:-1], sys.argv[-1]
text = open(script).read()
last = int(re.search(r"#SBATCH --array=0-(\\d+)", text).group(1))
output = re.search(r"#SBATCH --output=(\\S+)", text).group(1)
with open(os.environ["FAKE_LOG"], "a") as log:
    log.write("sbatch %s\\n" % json.dumps(flags))
for index in range(last + 1):
    env = dict(os.environ, SLURM_ARRAY_TASK_ID=str(index), SLURM_ARRAY_JOB_ID="4242", SLURM_SUBMIT_DIR=os.getcwd())
    with open(output.replace("%a", str(index)), "ab") as out:
        subprocess.run(["/bin/bash", script], env=env, stdout=out, stderr=subprocess.STDOUT)
print("4242")
"""


class MachineFixture(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, str(self.root), True)
        self.repo = self.root / "p"
        self.commit = make_repo(self.repo, {"cloudsweep.ini": INI, "sim.sh": SIM})
        (self.repo / "trace.bin").write_text("untracked input")
        (self.repo / ".cloudsweep").mkdir()
        (self.repo / ".cloudsweep/machines.ini").write_text(MACHINES.format(root=self.root))
        self.sweep = self.root / "sweep.jsonl"
        self.sweep.write_text("".join('["{bin}", "{out}", "%d", "{input:trace.bin}"]\n' % i for i in range(3)))
        fakes = self.root / "bin"
        fakes.mkdir()
        for name, text in (("ssh", FAKE_SSH), ("sbatch", FAKE_SBATCH),
                           ("squeue", "#!/bin/sh\n"), ("scancel", "#!/bin/sh\necho scancel \"$@\" >> \"$FAKE_LOG\"\n")):
            (fakes / name).write_text(text.format(python=sys.executable))
            (fakes / name).chmod(0o755)
        self.home = self.root / "home"
        self.home.mkdir()
        (self.home / "cs-setup.sh").write_text("export FROM_SETUP=yes\n")  # setup paths are relative to home
        self.log = self.root / "fake.log"
        environment = {key: value for key, value in os.environ.items()
                       if key not in ("CUDA_VISIBLE_DEVICES", "XDG_CONFIG_HOME")}
        environment.update(PATH="%s:%s" % (fakes, os.environ["PATH"]), HOME=str(self.home), FAKE_LOG=str(self.log))
        patcher = mock.patch.dict(os.environ, environment, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def cli(self, *argv):
        out, old = io.StringIO(), os.getcwd()
        os.chdir(str(self.repo))
        try:
            with redirect_stdout(out), mock.patch("sys.stderr", io.StringIO()) as err:
                code = cli.main(list(argv))
        finally:
            os.chdir(old)
        return code, out.getvalue() + err.getvalue()

    def submit(self, machine, *extra, expect=0):
        code, out = self.cli("submit", "--on", machine, "--commands", str(self.sweep), "--input", "trace.bin", *extra)
        self.assertEqual(code, expect, out)
        found = re.search(r"submitted (\S+):", out) or re.search(r"receipt: \S+/(\S+)\.json", out)
        return found.group(1), out

    def wait_for_pool(self, workdir, submission):
        state = Path(workdir) / "jobs" / submission / "state.json"
        for _ in range(120):
            if state.is_file() and "finished" in json.loads(state.read_text()):
                return json.loads(state.read_text())
            time.sleep(0.25)
        self.fail("the pool did not finish: %s" % (Path(workdir) / "jobs" / submission / "pool.log").read_text())

    def receipt(self, submission):
        return json.loads((self.repo / ".cloudsweep/receipts" / (submission + ".json")).read_text())


class MachineTests(MachineFixture):
    def test_slurm_sweep_is_one_array_job_and_fetches_verified_results(self):
        submission, out = self.submit("cluster")
        self.assertIn("(Slurm job 4242)", out)
        self.assertIn('sbatch ["--parsable", "--account=acct", "--partition=cpu"]', self.log.read_text())
        script = (self.root / "cluster/jobs" / submission / "job.sh").read_text()
        for line in ("#SBATCH --array=0-2", "#SBATCH --time=6", "#SBATCH --signal=B:TERM@120",
                     "#SBATCH --cpus-per-task=1", "#SBATCH --mem=3500M", "#SBATCH --output=jobs/%s/logs/%%a.txt"
                     % submission, 'set -e\ncd\nsource cs-setup.sh\ncd "$SLURM_SUBMIT_DIR"\nexec python3 code/'):
            self.assertIn(line, script)
        self.assertNotIn("--gpus-per-node", script)
        code, out = self.cli("status", "--on", "cluster")
        self.assertIn("%s FINISHED" % submission, out)
        self.assertIn('"succeeded": 3', out)
        destination = self.root / "results"
        code, out = self.cli("fetch", submission, str(destination))  # the receipt names the machine
        self.assertEqual(code, 0, out)
        self.assertIn("complete", out)
        result = destination / submission / "2" / "1" / "out" / "result.txt"
        self.assertEqual(result.read_text(), "2 untracked input\n")
        self.assertEqual((result.parent / "setup.txt").read_text(), "yes\n")
        manifest = json.loads((destination / submission / "submission.json").read_text())
        self.assertEqual(manifest["machine"], {"name": "cluster", "kind": "slurm", "host": "fakehost"})
        self.assertTrue(core.DIGEST_RE.fullmatch(manifest["image_digest"]))
        # A file of your own in the results is not the machine's business; fetching again verifies.
        (result.parent / "notes.txt").write_text("mine")
        code, out = self.cli("fetch", submission, str(destination))
        self.assertEqual(code, 0, out)
        # A local manifest that no longer matches the machine's is reported, never trusted.
        manifest_path = destination / submission / "1" / "1" / "manifest.json"
        changed = json.loads(manifest_path.read_text())
        changed["exit_code"] = 3
        manifest_path.write_text(json.dumps(changed))
        code, out = self.cli("fetch", submission, str(destination))
        self.assertEqual(code, 1)
        self.assertIn("child 1: verification failed [1 invalid completion manifest]", out)
        self.assertIn("exists with different content; not overwritten", out)
        # The next submission of this commit reuses the build, also from another section's name.
        again, _ = self.submit("cluster")
        self.assertFalse(self.receipt(again)["result"]["built"])

    def test_gpu_commands_use_the_gpu_partition(self):
        code, out = self.cli("submit", "--on", "cluster", "--dry-run", "--gpus", "2", "--commands", str(self.sweep),
                             "--input", "trace.bin")
        self.assertEqual(code, 0, out)
        self.assertIn("#SBATCH --gpus-per-node=h100:2", out)
        self.assertIn('"--partition=gpu"', out)
        self.assertNotIn("--partition=cpu", out)
        self.assertFalse(self.log.exists())  # a dry run contacts nothing

    def test_ssh_pool_runs_commands_in_slots(self):
        submission, out = self.submit("box")
        self.assertIn("a pool of 2 slot(s)", out)
        state = self.wait_for_pool(self.root / "box", submission)
        self.assertEqual((state["exit_codes"], state["stopped"]), ({"0": 3}, False))
        code, out = self.cli("status", submission)
        self.assertIn("%s FINISHED" % submission, out)
        self.assertIn("not running", self.cli("cancel", submission)[1])  # finished work is left as it is
        self.assertIn("%s FINISHED" % submission, self.cli("status", submission)[1])
        (self.root / "box/jobs" / submission / "cancelled").touch()  # a stop that came too late
        self.assertIn("%s FINISHED" % submission, self.cli("status", submission)[1])
        destination = self.root / "results"
        code, out = self.cli("fetch", "--on", "box", submission, str(destination))
        self.assertEqual(code, 0, out)
        self.assertEqual((destination / submission / "1/1/out/gpus.txt").read_text(), "none\n")

    def test_this_host_needs_no_settings_and_is_the_default(self):
        (self.repo / ".cloudsweep/machines.ini").unlink()
        code, out = self.cli("submit", "--commands", str(self.sweep), "--input", "trace.bin")  # no --on
        self.assertEqual(code, 0, out)
        self.assertIn(" on local (", out)
        submission = re.search(r"submitted (\S+):", out).group(1)
        self.wait_for_pool(self.home / "cloudsweep", submission)  # the default workdir, ~/cloudsweep
        self.assertIn("%s FINISHED" % submission, self.cli("status")[1])  # this checkout's, on local
        self.assertEqual(self.cli("cancel", "--all")[0], 0)
        self.assertEqual(self.cli("fetch", submission, str(self.root / "results"))[0], 0)
        self.assertFalse(self.log.exists())  # no ssh
        other = "20261106T000000Z-aaaaaa"
        self.assertEqual(machines.machine_of(self.repo, other), "local")  # no receipt here
        kept = self.root / "aws-receipts"  # where a deployment's RECEIPTS may keep them
        kept.mkdir()
        (self.repo / ".cloudsweep/deploy.env").write_text("RECEIPTS=%s\n" % kept)
        (kept / (other + ".json")).write_text(json.dumps({"submission": other}))
        self.assertEqual(machines.machine_of(self.repo, other), "aws")  # AWS receipts name no machine
        code, out = self.cli("status", other, submission)
        self.assertEqual(code, 1)
        self.assertIn("ran on AWS and the others on machines", out)

    def test_machines_can_be_described_once_per_user(self):
        user = self.root / "config" / "cloudsweep"
        user.mkdir(parents=True)
        (user / "machines.ini").write_text("[shared]\nkind = ssh\nhost = fakehost\nworkdir = shared-work\n")
        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.root / "config")}):
            machine = machines.load(self.repo, "shared")
            self.assertEqual((machine["workdir"], machine["slots"]), ("shared-work", None))
            submission, _ = self.submit("shared")
            self.wait_for_pool(self.home / "shared-work", submission)  # relative to the machine's home
            self.assertEqual(self.cli("fetch", submission, str(self.root / "results"))[0], 0)

    def test_gpu_commands_get_one_slot_per_gpu(self):
        submission, out = self.submit("gpubox", "--gpus", "1")
        self.assertIn("a pool of 2 slot(s) of 1 GPU(s)", out)
        self.wait_for_pool(self.root / "gpubox", submission)
        destination = self.root / "results"
        self.assertEqual(self.cli("fetch", submission, str(destination))[0], 0)
        seen = {(destination / submission / str(i) / "1/out/gpus.txt").read_text().strip() for i in range(3)}
        self.assertEqual(seen, {"0", "1"})

    def test_cancel_stops_the_pool_and_its_commands_upload_what_they_have(self):
        self.sweep.write_text('["sh", "-c", "sleep 60"]\n' * 4)
        submission, _ = self.submit("box")
        started = self.root / "box/store/runs" / submission / "0" / "1" / "started.json"
        for _ in range(80):
            if started.is_file():
                break
            time.sleep(0.25)
        self.assertIn('"running": 2', self.cli("status", submission)[1])
        code, out = self.cli("cancel", submission)
        self.assertIn("stop requested", out)
        state = self.wait_for_pool(self.root / "box", submission)
        self.assertTrue(state["stopped"])
        self.assertEqual(state["never_started"], 2)
        self.assertIn("%s CANCELLED" % submission, self.cli("status", "--on", "box")[1])
        code, out = self.cli("fetch", submission, str(self.root / "results"))
        self.assertEqual(code, 1)
        self.assertIn("child 0: terminated", out)
        self.assertIn("child 3: no attempt yet", out)

    def test_a_submission_that_never_started_is_reported_cancelled_and_fetched(self):
        submission, out = self.submit("broken", expect=1)  # its setup line fails before anything starts
        self.assertIn("submit on broken failed", out)
        self.assertEqual(list((self.root / "broken/builds").glob("*.lock")), [])  # the build lock is released
        (self.repo / ".cloudsweep/machines.ini").write_text(MACHINES.format(root=self.root).replace("setup = false", ""))
        self.assertIn("NOT STARTED", self.cli("status", submission)[1])
        self.assertIn("it will not start", self.cli("cancel", submission)[1])
        self.assertIn("%s CANCELLED" % submission, self.cli("status", submission)[1])
        code, out = self.cli("fetch", submission, str(self.root / "results"))
        self.assertEqual(code, 1, out)
        self.assertIn("child 0: no attempt yet", out)

    def test_status_reports_submissions_on_several_machines(self):
        first, _ = self.submit("cluster")
        second, _ = self.submit("box")
        self.wait_for_pool(self.root / "box", second)
        code, out = self.cli("status", first, second)
        self.assertEqual(code, 0, out)
        self.assertIn("%s FINISHED" % first, out)
        self.assertIn("%s FINISHED" % second, out)
        code, out = self.cli("status", "--json", first, second)
        self.assertEqual(set(json.loads(out)), {first, second})

    def test_a_concurrent_build_of_the_same_revision_is_waited_for(self):
        first, _ = self.submit("box")
        self.wait_for_pool(self.root / "box", first)
        build = self.root / "box/builds" / self.receipt(first)["build"]
        record = (build / "build.json").read_bytes()
        (build / "build.json").unlink()  # as if another submission were still building it
        lock = Path(str(build) + ".lock")
        lock.mkdir()
        (lock / "owner").write_text("20261106T000000Z-aaaaaa\n")

        def finish():
            time.sleep(1.5)
            (build / "build.json").write_bytes(record)
            shutil.rmtree(str(lock))
        threading.Thread(target=finish).start()
        second, out = self.submit("box")  # waits for that build, then reuses it
        self.assertFalse(self.receipt(second)["result"]["built"])
        self.assertFalse(lock.exists())
        self.wait_for_pool(self.root / "box", second)
        self.assertEqual(self.cli("fetch", second, str(self.root / "results"))[0], 0)

    def test_a_lock_is_released_only_by_its_owner_before_its_build_starts(self):
        machine = machines.load(self.repo, "box")
        lock = self.root / "box/builds/abc.lock"
        lock.mkdir(parents=True)
        (lock / "owner").write_text("20261106T000000Z-bbbbbb\n")
        machines.release(machine, "builds/abc", "20261106T000000Z-aaaaaa")  # another submission's lock
        self.assertTrue(lock.exists())
        (lock / "building").touch()
        machines.release(machine, "builds/abc", "20261106T000000Z-bbbbbb")  # its build started: the build releases it
        self.assertTrue(lock.exists())
        (lock / "building").unlink()
        machines.release(machine, "builds/abc", "20261106T000000Z-bbbbbb")
        self.assertFalse(lock.exists())

    def test_machines_lists_every_machine_and_what_it_is_for(self):
        with open(str(self.repo / ".cloudsweep/machines.ini"), "a") as stream:
            stream.write("\n[noted]\nkind = ssh\nhost = fakehost\nuse = idle 48 cores; free\n")
        code, out = self.cli("machines")
        self.assertEqual(code, 0, out)
        self.assertIn("idle 48 cores; free", out)
        names = [row["name"] for row in json.loads(self.cli("machines", "--json")[1])]
        self.assertEqual(names, ["cluster", "box", "gpubox", "broken", "noted", "local"])
        self.assertIn("local (default)", out)
        with open(str(self.repo / ".cloudsweep/machines.ini"), "a") as stream:
            stream.write('\n[bad]\nkind = slurm\nsbatch = --partition="\n')  # one bad section hides no others
        (self.repo / ".cloudsweep/deploy.env").write_text("CLOUDSWEEP_ENABLED=0\n")
        rows = {row["name"]: row for row in json.loads(self.cli("machines", "--json")[1])}
        self.assertEqual(list(rows), ["cluster", "box", "gpubox", "broken", "noted", "bad", "local", "aws"])
        self.assertIn("No closing quotation", rows["bad"]["use"])
        self.assertIn("cloud use off", rows["aws"]["use"])
        (self.repo / ".cloudsweep/deploy.env").write_text("CLOUDSWEEP_ENABLED=1\n")
        self.assertIn("costs money", self.cli("machines")[1])

    def test_settings_are_checked(self):
        file = self.repo / ".cloudsweep/machines.ini"
        for text, message in (("[m]\nkind = pbs\n", "kind must be slurm or ssh"),
                              ("[m]\nkind = slurm\nworkdir = /w/../x\n", "workdir must be a path"),
                              ("[m]\nkind = slurm\nslots = 2\n", "slots is not a slurm setting"),
                              ("[m]\nkind = ssh\nslots = 0\n", "slots must be a whole number")):
            file.write_text(text)
            with self.assertRaisesRegex(project.Refusal, message):
                machines.load(self.repo, "m")
        with self.assertRaisesRegex(project.Refusal, r"no machine \[other\]"):
            machines.load(self.repo, "other")
        file.write_text("[m]\nkind = slurm\nworkdir = ~/sweeps\n")
        self.assertEqual(machines.load(self.repo, "m")["workdir"], "sweeps")
        file.write_text("[aws]\nkind = ssh\n")
        with self.assertRaisesRegex(project.Refusal, "aws names the project's AWS deployment"):
            machines.load(self.repo, "aws")

    def test_setup_lines_are_part_of_the_build_identity_and_section_names_are_not(self):
        machine = machines.load(self.repo, "cluster")
        recipe = {"command": "make", "binary": None}
        first = machines.identity(machine, self.commit, recipe)
        self.assertEqual(first, machines.identity(dict(machine, name="cluster-gpu"), self.commit, recipe))
        changed = machines.identity(dict(machine, setup=["module load gcc/13"]), self.commit, recipe)
        self.assertNotEqual(first, changed)
        self.assertTrue(all(core.DIGEST_RE.fullmatch(digest) for digest in first[:2] + changed[:2]))


FAKE_APPTAINER = """#!{python}
import os, shutil, sys
args = sys.argv[1:]
if args[0] == "build":  # a SIF is a copy of the archive here
    shutil.copyfile(args[-1].split("://", 1)[1], args[-2])
    sys.exit(0)
index, binds, env, clean = 1, {{}}, {{}}, False
while args[index].startswith("--"):
    flag = args[index]
    index += 1
    if flag in ("--bind", "--home", "--env"):
        key, value = args[index].split("=" if flag == "--env" else ":", 1)
        (env.__setitem__(key, value) if flag == "--env" else binds.__setitem__(value, key))
        index += 1
    clean = clean or flag == "--cleanenv"
with open(os.environ["FAKE_LOG"], "a") as log:
    log.write("apptainer exec %s\\n" % " ".join(sorted(binds)))
command = args[index + 1:]
base = {{key: os.environ[key] for key in ("PATH", "FAKE_LOG", "FAKE_WORKER")}} if clean else dict(os.environ)
base.update(env)
if "/work" in binds:  # the container's paths, as host paths
    base["CLOUDSWEEP_WORK"] = binds["/work"]
if base.get("CLOUDSWEEP_STORE") == "file:///store":
    base["CLOUDSWEEP_STORE"] = "file://" + binds["/store"]
if command[:2] == ["python3", "/opt/cloudsweep/worker.py"]:
    command = [sys.executable, os.environ["FAKE_WORKER"]] + command[2:]
os.execvpe(command[0], command, base)
"""
FAKE_PODMAN = """#!/bin/sh
prev=""
for arg; do [ "$prev" = "-o" ] && echo archive > "$arg"; prev="$arg"; done
echo "podman $*" | cut -c1-60 >> "$FAKE_LOG"
"""


class ContainerTests(MachineFixture):
    """container = apptainer: the image built here (mocked engine), converted here or on the machine,
    or built on the machine; the revision built and every command run through apptainer exec."""

    def setUp(self):
        super().setUp()
        fakes = self.root / "bin"
        for name, text in (("apptainer", FAKE_APPTAINER), ("podman", FAKE_PODMAN)):
            (fakes / name).write_text(text.format(python=sys.executable))
            (fakes / name).chmod(0o755)
        os.environ["FAKE_WORKER"] = str(PACKAGE / "worker.py")
        (self.repo / "cloud").mkdir()  # the working tree's [image] Dockerfile, as `image build` reads it
        (self.repo / "cloud/Dockerfile").write_text("FROM docker.io/library/debian:bookworm-slim\n")
        with open(str(self.repo / ".cloudsweep/machines.ini"), "a") as stream:
            stream.write("\n[ctr]\nkind = slurm\nhost = fakehost\nworkdir = %s/ctr\ncontainer = apptainer\n" % self.root)

    def built_here(self):
        def save(*args, **kwargs):  # engine.engine: tag does nothing here, save writes the archive
            if "-o" in args:
                Path(args[list(args).index("-o") + 1]).write_text("archive")
        return [mock.patch.object(machines, "builds_here", return_value=True),
                mock.patch.object(machines.engine, "build_base", return_value="sha256:" + "c" * 64),
                mock.patch.object(machines.engine, "engine_name", return_value="podman"),
                mock.patch.object(machines.engine, "engine", side_effect=save),
                mock.patch("cloudsweep.image.checks")]

    def run_sweep(self, patches):
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        submission, out = self.submit("ctr")
        destination = self.root / "results"
        code, out = self.cli("fetch", submission, str(destination))
        self.assertEqual(code, 0, out)
        self.assertEqual((destination / submission / "2/1/out/result.txt").read_text(), "2 untracked input\n")
        job = json.loads((self.root / "ctr/jobs" / submission / "job.json").read_text())
        log = self.log.read_text()
        self.assertIn("apptainer exec /work\n", log)          # the revision's build, in the image
        self.assertIn("apptainer exec /store /work\n", log)   # each command, in the image
        return job, self.root / "ctr/images" / job["container"]["key"], log

    def test_the_image_is_built_and_converted_here(self):
        job, images, log = self.run_sweep(self.built_here())
        self.assertEqual(job["container"]["key"], "c" * 16)
        code, out = self.cli("submit", "--on", "ctr", "--dry-run", "--commands", str(self.sweep), "--input",
                             "trace.bin")
        self.assertIn("the image is built when you submit", out)  # a dry run builds nothing
        self.assertTrue((images / "base.sif").is_file())
        self.assertFalse((images / "base.tar").exists())
        self.assertNotIn("podman", log)

    def test_the_machine_converts_when_this_host_has_no_apptainer(self):
        which = lambda name: None if name in ("apptainer", "singularity") else shutil.which(name)
        job, images, _ = self.run_sweep(self.built_here() + [mock.patch.object(machines.shutil, "which", which)])
        self.assertTrue((images / "base.sif").is_file())
        self.assertFalse((images / "base.tar").exists())

    def given_image(self):
        given = self.root / "given.sif"
        given.write_text("an image")
        with open(str(self.repo / ".cloudsweep/machines.ini"), "a") as stream:
            stream.write("\n[given]\nkind = ssh\nhost = fakehost\nworkdir = %s/given\ncontainer = %s\n"
                         % (self.root, given))

    def test_an_image_already_on_the_machine_is_used_as_it_is(self):
        self.given_image()
        submission, out = self.submit("given")
        self.wait_for_pool(self.root / "given", submission)
        self.assertEqual(self.cli("fetch", submission, str(self.root / "results"))[0], 0)
        self.assertIn("apptainer exec /opt/cloudsweep /store /work\n", self.log.read_text())  # cloudsweep's worker, bound in
        self.assertRegex(self.cli("doctor", "--on", "given")[1], r"image +ok +%s" % re.escape(str(self.root / "given.sif")))
        (self.root / "given.sif").unlink()
        self.assertRegex(self.cli("doctor", "--on", "given")[1], r"image +-- +given has no image")
        self.assertFalse((self.root / "given/images").exists() and any((self.root / "given/images").iterdir()))

    def test_an_image_replaced_after_its_submission_is_refused(self):
        self.given_image()
        found = machines.existing_image
        with mock.patch.object(machines, "existing_image", lambda machine: dict(found(machine), stamp="1:0")):
            _, out = self.submit("given", expect=1)  # as if the file had another size and time at submission
        self.assertIn("changed after this submission", out)
        submission, _ = self.submit("given")
        self.wait_for_pool(self.root / "given", submission)
        site = remote.Site(self.root / "given", submission)
        (self.root / "given.sif").write_text("another image")  # replaced after the build
        self.assertTrue(remote.image_changed(site))
        site.job["container"]["stamp"] = remote.image_stamp(self.root / "given.sif")
        record, lock = site.build / "build.json", site.root / "builds" / (site.job["build"] + ".lock")
        record.unlink()
        lock.mkdir()
        (lock / "owner").write_text(submission)  # as the submission that builds it again takes the lock
        rebuild = ["sh", "-c", "echo {} > build.json"]  # stands in for the worker's build, which writes the record
        with mock.patch.object(remote, "image_changed", return_value=True), \
                mock.patch.object(remote, "in_container", return_value=rebuild):  # replaced while it builds
            with self.assertRaisesRegex(ValueError, "changed during the build"):
                remote.build(site)
        self.assertFalse(record.exists())  # never reused under the key of the image it was submitted with

    def test_the_machine_builds_the_image_only_when_this_host_cannot(self):
        job, images, log = self.run_sweep([mock.patch.object(machines, "builds_here", return_value=False)])
        self.assertIn("podman", log)                 # built on the machine, with its podman
        self.assertTrue((images / "context").is_dir())
        self.assertTrue((images / "base.sif").is_file())


class EmulationTests(unittest.TestCase):
    def test_only_an_emulator_registered_with_the_f_flag_builds_here(self):
        binfmt = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, str(binfmt), True)
        entry, status = binfmt / "qemu-aarch64", binfmt / "status"
        status.write_text("enabled\n")
        for text, works in (("enabled\ninterpreter /usr/libexec/qemu-binfmt/aarch64-binfmt-P\nflags: PF\n", True),
                            ("enabled\ninterpreter /usr/bin/qemu-aarch64-static\nflags: POCF\n", True),
                            ("enabled\ninterpreter /usr/libexec/qemu-binfmt/aarch64-binfmt-P\nflags: P\n", False),
                            ("disabled\ninterpreter /usr/bin/qemu-aarch64-static\nflags: F\n", False)):
            entry.write_text(text)
            self.assertEqual(machines.emulates("linux/arm64", binfmt), works, text)
        status.write_text("disabled\n")  # binfmt_misc off as a whole, whatever its entries say
        self.assertFalse(machines.emulates("linux/arm64", binfmt))
        entry.unlink()
        self.assertFalse(machines.emulates("linux/arm64", binfmt))
        self.assertFalse(machines.emulates("linux/riscv64", binfmt))


class RemoteTests(unittest.TestCase):
    def test_gpus_are_split_into_disjoint_slots(self):
        job = {"gpus": 2, "machine_gpus": 4, "slots": None}
        environment = {key: value for key, value in os.environ.items() if key != "CUDA_VISIBLE_DEVICES"}
        with mock.patch.dict(os.environ, environment, clear=True):
            self.assertEqual(remote.pool_slots(job), [{"CUDA_VISIBLE_DEVICES": "0,1"}, {"CUDA_VISIBLE_DEVICES": "2,3"}])
        with mock.patch.dict(os.environ, {"CUDA_VISIBLE_DEVICES": ""}):  # set but empty: no GPU is visible
            with self.assertRaisesRegex(ValueError, "this machine has 0"):
                remote.pool_slots(job)
        with mock.patch.dict(os.environ, {"CUDA_VISIBLE_DEVICES": "4,6,7"}):
            self.assertEqual(remote.pool_slots(dict(job, machine_gpus=None)), [{"CUDA_VISIBLE_DEVICES": "4,6"}])
        with self.assertRaisesRegex(ValueError, "needs 2 GPU"):
            remote.pool_slots(dict(job, machine_gpus=1))
        self.assertEqual(remote.pool_slots({"gpus": 0, "slots": 3}), [{}, {}, {}])
        cpus = len(os.sched_getaffinity(0))
        self.assertEqual(len(remote.pool_slots({"gpus": 0, "slots": None, "vcpus": 1})), cpus)

    def test_the_checkout_copy_keeps_modes_and_links(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, str(root))
        (root / "src/bin").mkdir(parents=True)
        (root / "src/bin/run").write_text("#!/bin/sh\n")
        (root / "src/bin/run").chmod(0o755)
        (root / "src/data.txt").write_text("x")
        (root / "src/data.txt").chmod(0o444)
        (root / "src/link").symlink_to("bin/run")
        remote.copy_tree(root / "src", root / "copy")
        self.assertEqual((root / "copy/bin/run").stat().st_mode & 0o777, 0o755)
        self.assertEqual((root / "copy/data.txt").stat().st_mode & 0o777, 0o444)
        self.assertEqual(os.readlink(str(root / "copy/link")), "bin/run")

    def test_command_states(self):
        self.assertEqual(remote.child_state(None, "RUNNING", False), "running")
        self.assertEqual(remote.child_state(remote.NO_ATTEMPT, "PENDING", False), "waiting")
        self.assertEqual(remote.child_state(None, None, True), "lost (started, no completion manifest)")
        self.assertEqual(remote.child_state(remote.NO_ATTEMPT, None, True), "waiting")
        self.assertEqual(remote.child_state("failed", None, False), "failed")

    def test_a_reused_process_id_is_not_taken_for_the_pool(self):
        site = mock.Mock(submission="20261106T000000Z-aaaaaa")
        self.assertFalse(remote.pool_alive(site, {"pid": os.getpid(), "pgid": os.getpid()}))
        self.assertFalse(remote.pool_alive(site, {"pid": os.getpid(), "finished": "now"}))


if __name__ == "__main__":
    unittest.main()
