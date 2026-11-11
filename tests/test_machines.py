"""Sweeps on machines from machines.ini and on this host, end to end: fake ssh runs each remote
command here (so real rsync works through it), and fake sbatch runs an array's tasks in order,
as Slurm would."""

import calendar
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

from helpers import PACKAGE, git, make_repo

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
if os.environ.get("FAKE_HOLD"):  # queued: the test says what squeue and sacct show
    print("4242")
    sys.exit(0)
for index in range(last + 1):
    env = dict(os.environ, SLURM_ARRAY_TASK_ID=str(index), SLURM_ARRAY_JOB_ID="4242", SLURM_SUBMIT_DIR=os.getcwd())
    with open(output.replace("%a", str(index)), "ab") as out:
        subprocess.run(["/bin/bash", script], env=env, stdout=out, stderr=subprocess.STDOUT)
print("4242")
"""
# squeue and sacct print FAKE_SLURM/queue.txt and accounting.txt, where {t:EPOCH} becomes a time as
# Slurm prints it: in the zone TZ names, and in ISO 8601 only with SLURM_TIME_FORMAT=standard.
FAKE_REPORTER = """#!{python}
import os, re, sys, time
name = os.path.join(os.environ["FAKE_SLURM"], "%s")
with open(os.environ["FAKE_LOG"], "a") as log:
    log.write("%s\\n")
def when(match):
    moment = time.localtime(int(match.group(1)))
    if os.environ.get("SLURM_TIME_FORMAT") == "standard":
        return time.strftime("%%Y-%%m-%%dT%%H:%%M:%%S", moment)
    return time.strftime("%%a %%H:%%M", moment)
if os.path.exists(name):
    sys.stdout.write(re.sub(r"\\{{t:(\\d+)\\}}", when, open(name).read()))
"""
FAKE_SCONTROL = """#!{python}
import os, sys
here = os.environ["FAKE_SLURM"]
if sys.argv[1:] == ["-o", "show", "partition"]:
    sys.stdout.write(open(os.path.join(here, "partitions.txt")).read())
elif sys.argv[1:] == ["show", "config"]:
    sys.stdout.write("AccountingStorageTRES   = cpu,mem\\n")
    if os.path.exists(os.path.join(here, "flags.txt")):
        sys.stdout.write("PriorityFlags           = %s\\n" % open(os.path.join(here, "flags.txt")).read().strip())
"""
PARTITIONS = """PartitionName=cpu AllowGroups=ALL Default=YES QoS=N/A TRES=cpu=848,mem=7893156M,node=7,billing=848
PartitionName=gpu AllowGroups=ALL Default=NO TRESBillingWeights=CPU=13.888888,Mem=9.01726G,GRES/gpu=1000
PartitionName=gpu-short AllowGroups=ALL Default=NO TRESBillingWeights=CPU=27.777776,Mem=18.03452G,GRES/gpu=2000
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
        for name, text in (("ssh", FAKE_SSH), ("sbatch", FAKE_SBATCH), ("scontrol", FAKE_SCONTROL),
                           ("squeue", FAKE_REPORTER % ("queue.txt", "squeue")),
                           ("sacct", FAKE_REPORTER % ("accounting.txt", "sacct")),
                           ("scancel", "#!/bin/sh\necho scancel \"$@\" >> \"$FAKE_LOG\"\n")):
            (fakes / name).write_text(text.format(python=sys.executable))
            (fakes / name).chmod(0o755)
        self.slurm = self.root / "slurm"
        self.slurm.mkdir()
        (self.slurm / "partitions.txt").write_text(PARTITIONS)
        (self.slurm / "flags.txt").write_text("MAX_TRES")
        self.home = self.root / "home"
        self.home.mkdir()
        (self.home / "cs-setup.sh").write_text("export FROM_SETUP=yes\n")  # setup paths are relative to home
        self.log = self.root / "fake.log"
        environment = {key: value for key, value in os.environ.items()
                       if key not in ("CUDA_VISIBLE_DEVICES", "XDG_CONFIG_HOME")}
        environment.update(PATH="%s:%s" % (fakes, os.environ["PATH"]), HOME=str(self.home), FAKE_LOG=str(self.log),
                           FAKE_SLURM=str(self.slurm))
        patcher = mock.patch.dict(os.environ, environment, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def cli(self, *argv):
        code, out, err = self.cli_streams(*argv)
        return code, out + err

    def cli_streams(self, *argv):
        out, old = io.StringIO(), os.getcwd()
        os.chdir(str(self.repo))
        try:
            with redirect_stdout(out), mock.patch("sys.stderr", io.StringIO()) as err:
                code = cli.main(list(argv))
        finally:
            os.chdir(old)
        return code, out.getvalue(), err.getvalue()

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
        self.assertNotIn("--partition=cpu", out.split('"sbatch": [')[2].split("]")[0])  # the batch line
        # A dry run only reads the cluster (its partitions' billing weights): nothing is sent or started.
        self.assertNotIn("sbatch", self.log.read_text())
        self.assertFalse((self.root / "cluster").exists())
        self.assertIn("billing on gpu: 2000 unit(s) per task, the largest term (MAX_TRES)", out)

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

    def test_a_machines_file_given_once_serves_the_submission_s_later_commands(self):
        given = self.root / "elsewhere.ini"
        given.write_text("[cluster]\nkind = ssh\nhost = fakehost\nworkdir = %s/given-work\nslots = 1\n" % self.root)
        code, out = self.cli("submit", "--on", "cluster", "--machines", str(given), "--commands", str(self.sweep),
                             "--input", "trace.bin")
        self.assertEqual(code, 0, out)
        submission = re.search(r"submitted (\S+):", out).group(1)
        receipt = self.receipt(submission)
        self.assertEqual(receipt["machines_file"], str(given))
        self.assertEqual((receipt["section"]["kind"], receipt["section"]["workdir"]),
                         ("ssh", "%s/given-work" % self.root))
        self.wait_for_pool(self.root / "given-work", submission)
        other, _ = self.submit("cluster")  # the project's [cluster], a Slurm machine of the same name
        given.unlink()  # later commands need neither the flag nor the file
        code, out = self.cli("status", submission, other)
        self.assertEqual(code, 0, out)
        self.assertEqual(out.count("cluster ("), 2)  # one group per endpoint, not per name
        self.assertIn("%s FINISHED" % submission, out)
        self.assertIn("%s FINISHED" % other, out)
        self.assertEqual(self.cli("fetch", submission, str(self.root / "results"))[0], 0)
        self.assertIn("--machines %s: no such file" % given, self.cli("status", "--machines", str(given))[1])
        self.assertIn("--machines is for machines", self.cli("submit", "--on", "aws", "--machines", str(given),
                                                              "--commands", str(self.sweep))[1])

    def test_this_host_s_submissions_are_listed_and_cancelled_with_their_receipts_settings(self):
        given = self.root / "other-local.ini"
        given.write_text("[local]\nworkdir = %s/other-local\nslots = 1\n" % self.root)
        self.sweep.write_text('["sh", "-c", "sleep 60"]\n')
        code, out = self.cli("submit", "--on", "local", "--machines", str(given), "--commands", str(self.sweep))
        self.assertEqual(code, 0, out)
        submission = re.search(r"submitted (\S+):", out).group(1)
        started = self.root / "other-local/store/runs" / submission / "0" / "1" / "started.json"
        for _ in range(80):
            if started.is_file():
                break
            time.sleep(0.25)
        self.assertIn("%s RUNNING" % submission, self.cli("status")[1])  # no flag: the receipt's workdir
        code, out = self.cli("cancel", "--all")
        self.assertEqual(code, 0, out)
        self.assertIn("%s: stop requested" % submission, out)
        self.assertTrue(self.wait_for_pool(self.root / "other-local", submission)["stopped"])
        self.assertIn("%s CANCELLED" % submission, self.cli("status")[1])

    def test_cancel_stops_every_group_before_waiting_for_any(self):
        first, second, third = (dict(machines.load(self.repo, "box"), workdir=str(self.root / name), name=name)
                                for name in ("one", "two", "three"))
        asked, clock = [], [0.0]
        repository = mock.patch.object(machines.project, "repository", return_value=self.repo)  # any cwd
        repository.start()
        self.addCleanup(repository.stop)

        def call(machine, action, *submissions, timeout=None):
            asked.append((machine["name"], action) + submissions)
            if machine["name"] == "two":
                raise project.Refusal("cancel on two failed: no answer")
            return {s: "stop requested" for s in submissions}

        def call_status(machine, *submissions):
            clock[0] += 40  # each poll takes a while; "one" is never seen to stop
            return {s: {"state": "RUNNING" if machine["name"] == "one" else "CANCELLED"} for s in submissions}
        args = mock.Mock(submission=None, wait=1)
        with mock.patch.object(machines, "call", call), mock.patch.object(machines, "call_status", call_status), \
                mock.patch.object(machines.time, "monotonic", lambda: clock[0]), \
                mock.patch.object(machines.time, "sleep"), redirect_stdout(io.StringIO()):
            with self.assertRaises(project.Refusal) as refused:
                machines.cancel(args, [(first, ["a"]), (second, ["b"]), (third, ["c"])])
        self.assertEqual([entry[0] for entry in asked], ["one", "two", "three"])  # all sent before the wait
        self.assertIn("cancel on two failed", str(refused.exception))
        self.assertIn("after --wait: a", str(refused.exception))

    def test_variables_resolve_per_machine_and_the_receipt_keeps_them(self):
        with open(str(self.repo / ".cloudsweep/machines.ini"), "a") as stream:
            stream.write("\n[here]\nkind = ssh\nhost = fakehost\nworkdir = %s/here\nslots = 1\n"
                         "vars = data = /data/a, gpu=H100,\n  odd = x{y}\n" % self.root)
            stream.write("\n[there]\nkind = ssh\nhost = fakehost\nworkdir = %s/there\nslots = 1\n"
                         "vars = data=/data/b\n" % self.root)
        self.sweep.write_text('["sh", "-c", "echo {var:data} {machine} {{var:data}} > {out}/v.txt"]\n')
        first, _ = self.submit("here")
        second, _ = self.submit("there")
        here, there = self.receipt(first), self.receipt(second)
        self.assertEqual((here["vars"], there["vars"]), ({"data": "/data/a"}, {"data": "/data/b"}))  # those used
        self.assertEqual(here["template_sha256"], there["template_sha256"])
        self.assertEqual(here["commands"], [["sh", "-c", "echo /data/a here {{var:data}} > {out}/v.txt"]])
        self.wait_for_pool(self.root / "there", second)
        self.assertEqual(self.cli("fetch", second, str(self.root / "results"))[0], 0)
        self.assertEqual((self.root / "results" / second / "0/1/out/v.txt").read_text(), "/data/b there {var:data}\n")
        self.sweep.write_text('["sh", "-c", "echo {var:odd} > {out}/v.txt"]\n')
        third, _ = self.submit("here")  # a value's braces are text, not placeholders
        self.wait_for_pool(self.root / "here", third)
        self.assertEqual(self.cli("fetch", third, str(self.root / "results"))[0], 0)
        self.assertEqual((self.root / "results" / third / "0/1/out/v.txt").read_text(), "x{y}\n")
        code, out = self.cli("submit", "--on", "there", "--commands", str(self.sweep))
        self.assertEqual(code, 1)
        self.assertIn("command line 1 uses {var:odd}, which [there] does not define (its vars: data)", out)
        (self.repo / ".cloudsweep/machines.ini").write_text("[bad]\nkind = ssh\nvars = 1x=2\n")
        with self.assertRaisesRegex(project.Refusal, "vars must be NAME=VALUE"):
            machines.load(self.repo, "bad")

    def test_build_cpus_caps_what_the_build_sees(self):
        (self.repo / "cloudsweep.ini").write_text(INI.replace("cp sim.sh build/sim", "cp sim.sh build/sim && nproc > nproc.txt"))
        git(self.repo, "commit", "-qam", "count the build's CPUs")
        with open(str(self.repo / ".cloudsweep/machines.ini"), "a") as stream:
            stream.write("\n[capped]\nkind = ssh\nhost = fakehost\nworkdir = %s/capped\nbuild_cpus = 1\n" % self.root)
        self.sweep.write_text('["sh", "-c", "cp nproc.txt {out}/"]\n')
        seen = {}
        for name, workdir in (("capped", "capped"), ("cluster", "cluster")):
            submission, _ = self.submit(name)
            if name == "capped":
                self.wait_for_pool(self.root / workdir, submission)
            self.assertEqual(self.cli("fetch", submission, str(self.root / "results"))[0], 0)
            seen[name] = ((self.root / "results" / submission / "0/1/out/nproc.txt").read_text().strip(),
                          self.receipt(submission)["build_cpus"])
        cpus = len(os.sched_getaffinity(0))
        self.assertEqual(seen["capped"], ("1", 1))
        self.assertEqual(seen["cluster"], (str(min(8, cpus)), 8))  # Slurm's login nodes: 8 by default
        self.assertIsNone(machines.load(self.repo, "box")["build_cpus"])  # ssh machines: no cap by default

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
        # The receipt keeps the settings the submission was made with; --on uses the repaired file.
        self.assertIn("status on broken failed", self.cli("status", submission)[1])
        self.assertIn("NOT STARTED", self.cli("status", "--on", "broken", submission)[1])
        self.assertIn("it will not start", self.cli("cancel", "--on", "broken", submission)[1])
        self.assertIn("%s CANCELLED" % submission, self.cli("status", "--on", "broken", submission)[1])
        code, out = self.cli("fetch", "--on", "broken", submission, str(self.root / "results"))
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


def utc(*when):
    return calendar.timegm(when + (0, 0, 0))


class SlurmTests(MachineFixture):
    """What status, fetch and a dry run read from Slurm: the queue, the accounting and the
    partitions' billing weights, with fake squeue, sacct and scontrol output."""

    def held(self, *extra):
        os.environ["FAKE_HOLD"] = "1"  # sbatch queues the job and runs nothing
        submission, _ = self.submit("cluster", *extra)
        return submission

    def test_status_shows_why_tasks_wait_and_the_estimated_start_in_utc(self):
        submission = self.held()
        (self.slurm / "queue.txt").write_text("0|PENDING|Priority|{t:%d}||node12\n1|PENDING|None|N/A||(null)\n"
                                              "2|PENDING||N/A||\n" % utc(2026, 11, 11, 2, 43, 0))
        # As a cluster's login shell may leave them: another zone, and times as Tue 20:43.
        os.environ.update(TZ="America/Chicago", SLURM_TIME_FORMAT="relative")
        code, out = self.cli("status", submission)
        self.assertEqual(code, 0, out)
        self.assertIn('%s PENDING children {"waiting": 3} (Slurm job 4242: 3 pending (Priority), estimated start '
                      '2026-11-11T02:43:00Z on node12)\n' % submission, out)
        row = json.loads(self.cli_streams("status", "--json", submission)[1])[submission]
        self.assertEqual(row["queue"], [
            {"task": 0, "state": "PENDING", "reason": "Priority", "start": "2026-11-11T02:43:00Z", "nodes": "node12"},
            {"task": 1, "state": "PENDING", "reason": None, "start": None, "nodes": None},
            {"task": 2, "state": "PENDING", "reason": None, "start": None, "nodes": None}])
        self.assertNotIn("accounting", row)  # nothing has run, so sacct is not asked
        self.assertNotIn("sacct", self.log.read_text())
        (self.slurm / "queue.txt").write_text("0|PENDING|JobHeldUser|N/A||(null)\n")
        self.assertIn("(Slurm job 4242: 1 pending (JobHeldUser), no start estimate)", self.cli("status", submission)[1])

    def test_a_running_task_shows_its_node_its_start_and_what_it_has_used(self):
        submission = self.held()
        start = utc(2026, 11, 11, 9, 12, 27)
        (self.slurm / "queue.txt").write_text("0|RUNNING|None|{t:%d}|node02|(null)\n1|PENDING|Resources|N/A||\n"
                                              "2|PENDING|Resources|N/A||\n" % start)
        (self.slurm / "accounting.txt").write_text(
            "4242_0|RUNNING|{t:%d}|Unknown|120|node02|gpu|billing=8,cpu=8,gres/gpu:h100=1,gres/gpu=1,node=1|0:0\n"
            "4242_[1-2]|PENDING|Unknown|Unknown|0|None assigned|gpu||0:0\n" % start)
        code, out = self.cli("status", submission)
        self.assertIn('RUNNING children {"running": 1, "waiting": 2} (Slurm job 4242: 1 running on node02 since '
                      '2026-11-11T09:12:27Z; 2 pending (Resources), no start estimate)', out)
        self.assertIn("Slurm accounting: 1 RUNNING; elapsed 0:02:00 (2026-11-11T09:12:27Z to now on node02); "
                      "0.033 GPU-hours; billing 8 unit(s) per task, 0.267 unit-hours", out)
        self.assertFalse((self.root / "cluster/jobs" / submission / "accounting.json").exists())  # not final

    def test_accounting_of_finished_failed_and_cancelled_tasks_is_read_once(self):
        submission = self.held()
        (self.slurm / "accounting.txt").write_text(
            "4242_0|COMPLETED|{t:%d}|{t:%d}|1868|node64|gpu-short|billing=2000,cpu=8,gres/gpu=1,mem=96G,node=1|0:0\n"
            "4242_1|FAILED|{t:%d}|{t:%d}|193|node02|gpu|billing=8,cpu=8,gres/gpu:h100=1,gres/gpu=1,node=1|1:0\n"
            "4242_[2]|CANCELLED by 1234|None|{t:%d}|0|None assigned|gpu||0:0\n" % (
                utc(2026, 11, 11, 16, 5, 40), utc(2026, 11, 11, 16, 36, 48), utc(2026, 11, 11, 9, 12, 27),
                utc(2026, 11, 11, 9, 15, 40), utc(2026, 11, 11, 9, 15, 41)))
        os.environ["TZ"] = "Asia/Kolkata"
        code, out = self.cli("status", submission)
        self.assertIn("%s FINISHED" % submission, out)
        self.assertIn("Slurm accounting: 1 CANCELLED, 1 COMPLETED, 1 FAILED; elapsed 0:34:21 in all; 0.573 GPU-hours; "
                      "billing 1038.207 unit-hours", out)
        accounting = json.loads(self.cli_streams("status", "--json", submission)[1])[submission]["accounting"]
        self.assertEqual(accounting["tasks"][0], {
            "task": 0, "state": "COMPLETED", "start": "2026-11-11T16:05:40Z", "end": "2026-11-11T16:36:48Z",
            "elapsed_s": 1868, "nodes": "node64", "partition": "gpu-short", "gpus": 1, "billing": 2000,
            "exit_code": "0:0"})
        self.assertEqual(accounting["tasks"][2], {
            "task": 2, "state": "CANCELLED", "start": None, "end": "2026-11-11T09:15:41Z", "elapsed_s": 0,
            "nodes": None, "partition": "gpu", "gpus": 0, "billing": None, "exit_code": "0:0"})
        self.assertEqual(accounting["totals"], {"states": {"CANCELLED": 1, "COMPLETED": 1, "FAILED": 1},
                                                "elapsed_s": 2061, "gpu_hours": round(2061 / 3600, 6),
                                                "billing_unit_hours": round((2000 * 1868 + 8 * 193) / 3600, 6)})
        (self.slurm / "accounting.txt").unlink()  # final: kept on the machine, not asked for again
        self.assertEqual(json.loads(self.cli_streams("status", "--json", submission)[1])[submission]["accounting"],
                         accounting)
        self.assertEqual(self.log.read_text().count("sacct\n"), 1)
        destination = self.root / "results"
        code, out = self.cli("fetch", submission, str(destination))
        self.assertEqual(code, 1, out)  # no command ran here: incomplete, and accounted for
        self.assertIn("Slurm accounting: 1 CANCELLED, 1 COMPLETED, 1 FAILED", out)
        self.assertTrue(out.strip().splitlines()[-1].startswith("INCOMPLETE: "))
        kept = json.loads((destination / submission / "fetch.json").read_text())
        self.assertEqual((kept["accounting"]["job"], kept["accounting"]["totals"]), ("4242", accounting["totals"]))

    def test_every_execution_of_a_requeued_task_is_accounted_for(self):
        submission = self.held()
        (self.slurm / "accounting.txt").write_text(
            "4242_0|REQUEUED|{t:%d}|{t:%d}|6|node04|cpu|billing=2,cpu=2,node=1|0:0\n"
            "4242_0|COMPLETED|{t:%d}|{t:%d}|60|node04|cpu|billing=2,cpu=2,node=1|0:0\n"
            "4242_1|COMPLETED|{t:%d}|{t:%d}|30|node05|cpu|billing=2,cpu=2,node=1|0:0\n"
            "4242_2|REQUEUED|{t:%d}|{t:%d}|5|node05|cpu|billing=2,cpu=2,node=1|0:0\n"
            "4242_2|PENDING|Unknown|Unknown|0|None assigned|cpu||0:0\n" % (
                utc(2026, 11, 11, 22, 9, 27), utc(2026, 11, 11, 22, 9, 33), utc(2026, 11, 11, 22, 11, 40),
                utc(2026, 11, 11, 22, 12, 40), utc(2026, 11, 11, 22, 9, 27), utc(2026, 11, 11, 22, 9, 57),
                utc(2026, 11, 11, 22, 9, 27), utc(2026, 11, 11, 22, 9, 32)))
        (self.slurm / "queue.txt").write_text("2|PENDING|BeginTime|{t:%d}||\n" % utc(2026, 11, 11, 22, 11, 35))
        accounting = json.loads(self.cli_streams("status", "--json", submission)[1])[submission]["accounting"]
        self.assertEqual([(task["task"], task["state"], task["elapsed_s"]) for task in accounting["tasks"]],
                         [(0, "REQUEUED", 6), (0, "COMPLETED", 60), (1, "COMPLETED", 30), (2, "REQUEUED", 5)])
        self.assertEqual(accounting["totals"]["elapsed_s"], 101)
        self.assertFalse((self.root / "cluster/jobs" / submission / "accounting.json").exists())  # 2 is queued
        (self.slurm / "queue.txt").unlink()
        with open(str(self.slurm / "accounting.txt"), "a") as stream:
            stream.write("4242_2|COMPLETED|{t:%d}|{t:%d}|40|node05|cpu|billing=2,cpu=2,node=1|0:0\n" % (
                utc(2026, 11, 11, 22, 11, 40), utc(2026, 11, 11, 22, 12, 20)))
        text = (self.slurm / "accounting.txt").read_text().replace(
            "4242_2|PENDING|Unknown|Unknown|0|None assigned|cpu||0:0\n", "")
        (self.slurm / "accounting.txt").write_text(text)
        code, out = self.cli("status", submission)
        self.assertIn("Slurm accounting: 3 COMPLETED, 2 REQUEUED; elapsed 0:02:21 in all", out)
        self.assertTrue((self.root / "cluster/jobs" / submission / "accounting.json").exists())  # final

    def test_status_works_when_sacct_does_not(self):
        submission = self.held()
        sacct = self.root / "bin" / "sacct"
        sacct.write_text("#!/bin/sh\necho 'sacct: error: Slurm accounting storage is disabled' >&2\nexit 1\n")
        code, out = self.cli("status", submission)
        self.assertEqual(code, 0, out)
        self.assertIn("%s FINISHED" % submission, out)
        self.assertIn("Slurm accounting unavailable: sacct failed: sacct: error: Slurm accounting storage is disabled",
                      out)
        self.assertFalse((self.root / "cluster/jobs" / submission / "accounting.json").exists())
        sacct.unlink()
        self.assertIn("Slurm accounting unavailable: sacct: ", self.cli("status", submission)[1])

    def test_billing_is_unknown_when_the_cluster_does_not_say_its_priority_flags(self):
        (self.slurm / "flags.txt").unlink()
        code, out, err = self.cli_streams("submit", "--on", "cluster", "--dry-run", "--json", "--gpus", "1",
                                          "--commands", str(self.sweep), "--input", "trace.bin")
        self.assertEqual(code, 0, err)
        self.assertIn("gave no PriorityFlags", json.loads(out)["billing"][0]["error"])

    def test_a_dry_run_shows_the_partition_s_billing_and_warns_above_a_gpu_s_share(self):
        code, out, err = self.cli_streams("submit", "--on", "cluster", "--dry-run", "--json", "--gpus", "1",
                                          "--memory-mib", str(128 * 1024), "--commands", str(self.sweep),
                                          "--input", "trace.bin")
        self.assertEqual(code, 0, err)
        plan = json.loads(out)
        self.assertEqual(len(plan["billing"]), 1)
        billing = plan["billing"][0]
        self.assertEqual((billing["partition"], billing["default"], billing["units"], billing["per_gpu"],
                          billing["as_gpus"]), ("gpu", False, 1154.21, 1000, 1.154))
        self.assertEqual(billing["terms"], {"cpu": 13.89, "mem": 1154.21, "gres/gpu": 1000})
        self.assertIn("warning: on gpu each task bills 1154.21 units, worth 1.154 GPU(s): its 128 GiB of memory exceed "
                      "1 GPU(s)' share (110.9 GiB)", err)
        code, out, err = self.cli_streams("submit", "--on", "cluster", "--dry-run", "--json", "--commands",
                                          str(self.sweep), "--input", "trace.bin")
        billing = json.loads(out)["billing"][0]  # a CPU job, on the partition its section names
        self.assertEqual((billing["partition"], billing["units"], billing["weights"]), ("cpu", 1, None))
        self.assertIn("billing on cpu: 1 unit(s) per task (no TRESBillingWeights: Slurm bills the allocated CPUs)", err)
        self.assertNotIn("sbatch", self.log.read_text())

    def test_a_partition_per_submission_wins_and_is_billed_and_recorded(self):
        code, out, err = self.cli_streams("submit", "--on", "cluster", "--dry-run", "--json", "--gpus", "1",
                                          "--partition", "gpu-short", "--commands", str(self.sweep), "--input",
                                          "trace.bin")
        self.assertEqual(code, 0, err)
        plan = json.loads(out)
        self.assertEqual(plan["sbatch"][-3:-1], ["--partition=gpu", "--partition=gpu-short"])  # the last wins
        self.assertEqual((plan["partition"], plan["billing"][0]["partition"], plan["billing"][0]["units"]),
                         ("gpu-short", "gpu-short", 2000))
        submission, _ = self.submit("cluster", "--partition", "gpu-short")
        sent = [line for line in self.log.read_text().splitlines() if line.startswith("sbatch")][-1]
        self.assertTrue(sent.endswith('"--partition=cpu", "--partition=gpu-short"]'), sent)  # a CPU job here
        self.assertEqual(self.receipt(submission)["partition"], "gpu-short")
        self.assertIn("[box] is ssh", self.cli("submit", "--on", "box", "--partition", "x", "--commands",
                                              str(self.sweep), "--input", "trace.bin")[1])
        self.assertIn("--partition is for machines", self.cli("submit", "--on", "aws", "--partition", "x",
                                                               "--commands", str(self.sweep))[1])

    def test_submit_json_gives_the_dry_run_fields_and_the_receipt(self):
        code, out, _ = self.cli_streams("submit", "--on", "cluster", "--dry-run", "--json", "--commands",
                                        str(self.sweep), "--input", "trace.bin")
        plan = json.loads(out)  # one document, the batch script in it
        self.assertTrue(plan["dry_run"])
        self.assertIn("#SBATCH --array=0-2", plan["script"])
        code, out, err = self.cli_streams("submit", "--on", "cluster", "--json", "--commands", str(self.sweep),
                                          "--input", "trace.bin")
        self.assertEqual(code, 0, err)
        sent = json.loads(out)
        self.assertLessEqual(set(plan), set(sent))
        self.assertFalse(sent["dry_run"])
        self.assertEqual((sent["submission_state"], sent["result"]["slurm_job"]), ("submitted", "4242"))
        self.assertEqual(json.loads(Path(sent["receipt"]).read_text()),
                         {key: value for key, value in sent.items() if key not in ("receipt", "dry_run", "script")})
        code, out, err = self.cli_streams("submit", "--on", "nowhere", "--json", "--commands", str(self.sweep))
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out), {"error": json.loads(out)["error"], "receipt": None, "submission": None,
                                           "submission_state": None})
        self.assertIn("no machine [nowhere]", json.loads(out)["error"])
        code, out, err = self.cli_streams("submit", "--on", "aws", "--json", "--commands", str(self.sweep))
        self.assertEqual((code, out), (1, ""))
        self.assertIn("--json is for machines", err)

    def test_no_build_runs_the_checkout_as_committed_and_refuses_bin(self):
        code, out = self.cli("submit", "--on", "box", "--no-build", "--commands", str(self.sweep), "--input",
                             "trace.bin")
        self.assertEqual(code, 1, out)  # the sweep's commands run {bin}
        self.assertIn("--no-build: command 0 uses {bin}", out)
        self.assertIn("--no-build is for machines", self.cli("submit", "--on", "aws", "--no-build", "--commands",
                                                             str(self.sweep))[1])
        self.sweep.write_text('["sh", "-c", "ls build > {out}/built.txt 2>&1; echo {{bin}} {index} > {out}/i.txt"]\n')
        submission, out = self.submit("box", "--no-build")
        self.assertTrue(self.receipt(submission)["no_build"])
        self.wait_for_pool(self.root / "box", submission)
        destination = self.root / "results"
        self.assertEqual(self.cli("fetch", submission, str(destination))[0], 0)
        self.assertIn("No such file", (destination / submission / "0/1/out/built.txt").read_text())
        self.assertEqual((destination / submission / "0/1/out/i.txt").read_text(), "{bin} 0\n")
        record = json.loads((self.root / "box/builds" / self.receipt(submission)["build"] / "build.json").read_text())
        self.assertEqual(record["recipe"], {"command": "", "binary": None})
        self.assertNotIn("mkdir -p build", (self.root / "box/builds" / self.receipt(submission)["build"] /
                                            "build.log").read_text())
        built, _ = self.submit("box")  # the same commit, built: another build directory
        self.assertNotEqual(self.receipt(built)["build"], self.receipt(submission)["build"])
        self.assertFalse(self.receipt(built)["no_build"])


class BillingTests(unittest.TestCase):
    """The units of a request under a partition's TRESBillingWeights, as Slurm computes them."""
    BATCH = "CPU=13.888888,Mem=9.01726G,GRES/gpu=1000"

    def request(self, cpus=8, gib=96, gpus=1, gpu_type=None):
        return {"cpus": cpus, "memory_mib": gib * 1024, "gpus": gpus, "gpu_type": gpu_type}

    def test_the_largest_term_is_billed_with_max_tres(self):
        found = machines.billing_units(self.BATCH, "MAX_TRES", self.request())
        self.assertEqual((found["units"], found["terms"]), (1000, {"cpu": 111.11, "mem": 865.66, "gres/gpu": 1000}))
        self.assertEqual(machines.gpu_share(dict(found, partition="p"), self.request()),
                         {"per_gpu": 1000, "as_gpus": 1})
        twice = machines.billing_units("CPU=27.777776,Mem=18.03452G,GRES/gpu=2000", "MAX_TRES", self.request())
        self.assertEqual(twice["units"], 2000)  # DeltaAI's interactive partition: one GPU bills 2000

    def test_memory_or_cpus_above_the_gpus_share_are_warned_about(self):
        found = machines.billing_units(self.BATCH, "MAX_TRES", self.request(gib=128))
        share = machines.gpu_share(dict(found, partition="ghx4"), self.request(gib=128))
        self.assertEqual((found["units"], share["as_gpus"]), (1154.21, 1.154))
        self.assertEqual(share["warning"], "on ghx4 each task bills 1154.21 units, worth 1.154 GPU(s): its 128 GiB "
                                           "of memory exceed 1 GPU(s)' share (110.9 GiB)")
        found = machines.billing_units(self.BATCH, "MAX_TRES", self.request(cpus=96))
        self.assertIn("96 CPUs exceed 1 GPU(s)' share (72 CPUs)",
                      machines.gpu_share(dict(found, partition="p"), self.request(cpus=96))["warning"])

    def test_the_sum_is_billed_without_max_tres_and_cpus_without_weights(self):
        found = machines.billing_units(self.BATCH, "", self.request())
        self.assertEqual((found["units"], found["rule"]), (1976.77, "the sum"))
        found = machines.billing_units(None, "", self.request())
        self.assertEqual(found["units"], 8)
        self.assertIn("Slurm bills the allocated CPUs", found["notes"][0])
        typed = machines.billing_units("CPU=1,GRES/gpu:h100=500,License/x=3", "", self.request(gpu_type="H100"))
        self.assertEqual(typed["terms"], {"cpu": 8, "gres/gpu:h100": 500})
        self.assertEqual(typed["notes"], ["the weight License/x=3 is not counted"])

    def test_gpus_that_weigh_nothing_bill_without_a_gpu_s_worth(self):
        found = machines.billing_units("CPU=1,GRES/gpu=0", "MAX_TRES", self.request())
        self.assertEqual((found["units"], machines.gpu_share(dict(found, partition="p"), self.request())), (8, {}))

    def test_rules_cloudsweep_does_not_know_are_not_guessed(self):
        self.assertIn("does not apply", machines.billing_units(self.BATCH, "MAX_TRES_GRES", self.request())["error"])
        self.assertIn("cannot read", machines.billing_units("CPU=1K", "", self.request())["error"])

    def test_the_request_includes_the_sections_sbatch_options(self):
        job = {"vcpus": 1, "memory_mib": 3500, "gpus": 1, "gpu_type": "h100",
               "sbatch": ["--account=a", "-p", "gpu-short", "--mem=128G", "-c4", "--exclusive"]}
        partitions, request, uncounted = machines.requested(job)
        self.assertEqual((partitions, request, uncounted),
                         (["gpu-short"], {"cpus": 4, "memory_mib": 131072, "gpus": 1, "gpu_type": "h100"},
                          ["--exclusive"]))
        self.assertEqual(machines.requested(dict(job, sbatch=["--partition=a,b", "--mem", "0"]))[::2],
                         (["a", "b"], ["--mem"]))
        self.assertIsNone(machines.requested(dict(job, sbatch=[]))[0])


FAKE_APPTAINER = """#!{python}
import os, re, shutil, sys
args = sys.argv[1:]
if args[0] == "build":  # a SIF is a copy of the archive here
    shutil.copyfile(args[-1].split("://", 1)[1], args[-2])
    sys.exit(0)
if args[1:] == ["--help"]:
    print(os.environ.get("FAKE_APPTAINER_HELP", "      --no-mount strings   disable one or more 'mount xxx' options"))
    sys.exit(0)
index, binds, env, clean, fixed = 1, {{}}, {{}}, False, {{}}
while args[index].startswith("--"):
    flag = args[index]
    index += 1
    if flag in ("--bind", "--home", "--env"):
        key, value = args[index].split("=" if flag == "--env" else ":", 1)
        (env.__setitem__(key, value) if flag == "--env" else binds.__setitem__(value.split(":")[0], key))
        index += 1
    elif flag in ("--no-mount", "--pwd"):
        fixed[flag] = args[index]
        index += 1
    clean = clean or flag == "--cleanenv"
if "--home" in " ".join(args) and fixed != {{"--no-mount": "cwd", "--pwd": "/work"}}:
    sys.exit("the container would also see the directory it was started from")
if "/store" in binds:
    sys.exit("the whole store is bound; a command sees only its own submission's")
with open(os.environ["FAKE_LOG"], "a") as log:  # submission ids as SUB
    log.write("apptainer exec %s\\n" % re.sub(r"20[0-9]{{6}}T[0-9]{{6}}Z-[0-9a-f]{{6}}", "SUB", " ".join(sorted(binds))))
command = args[index + 1:]
base = {{key: os.environ[key] for key in ("PATH", "FAKE_LOG", "FAKE_WORKER")}} if clean else dict(os.environ)
base.update(env)
if "/work" in binds:  # the container's paths, as host paths
    base["CLOUDSWEEP_WORK"] = binds["/work"]
if base.get("CLOUDSWEEP_STORE") == "file:///store":  # the store the narrowed binds come from
    base["CLOUDSWEEP_STORE"] = "file://" + binds["/store/inputs"][:-len("/inputs")]
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
        self.assertIn("apptainer exec /store/inputs /store/runs/SUB /store/submissions/SUB /work\n", log)  # commands
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
        self.assertIn("apptainer exec /opt/cloudsweep /store/inputs /store/runs/SUB /store/submissions/SUB /work\n",
                      self.log.read_text())  # cloudsweep's worker, bound in
        self.assertRegex(self.cli("doctor", "--on", "given")[1], r"image +ok +%s" % re.escape(str(self.root / "given.sif")))
        (self.root / "given.sif").unlink()
        self.assertRegex(self.cli("doctor", "--on", "given")[1], r"image +-- +given has no image")
        self.assertFalse((self.root / "given/images").exists() and any((self.root / "given/images").iterdir()))

    def test_a_dry_run_names_the_machine_s_image_and_the_build_a_submission_uses(self):
        self.given_image()
        code, out, err = self.cli_streams("submit", "--on", "given", "--dry-run", "--json", "--commands",
                                          str(self.sweep), "--input", "trace.bin")
        self.assertEqual(code, 0, err)
        plan = json.loads(out)
        self.assertEqual(plan["container"], "apptainer, the machine's image %s" % (self.root / "given.sif"))
        submission, _ = self.submit("given")
        self.assertEqual((self.receipt(submission)["build"], self.receipt(submission)["image_digest"]),
                         (plan["build"], plan["image_digest"]))
        (self.root / "given.sif").unlink()  # the submission would refuse, so the dry run does
        code, out = self.cli("submit", "--on", "given", "--dry-run", "--commands", str(self.sweep), "--input",
                             "trace.bin")
        self.assertEqual(code, 1, out)
        self.assertIn("given has no image", out)

    def test_every_container_starts_in_work_without_the_directory_it_started_from(self):
        self.given_image()
        submission, _ = self.submit("given")  # the fake apptainer refuses a start without both flags
        self.wait_for_pool(self.root / "given", submission)
        self.assertEqual(self.cli("fetch", submission, str(self.root / "results"))[0], 0)
        store, own = self.root / "w/store", "20261111T000000Z-abcdef"
        site = mock.Mock(job={"container": {"sif": "~/x.sif"}, "gpus": 0}, store=store, submission=own)
        with mock.patch.object(remote, "apptainer", return_value="apptainer"):
            argv = remote.in_container(site, "/w/jobs/scratch", {}, store=True)
        self.assertEqual(argv[:9], ["apptainer", "exec", "--cleanenv", "--home", "/w/jobs/scratch:/work",
                                    "--no-mount", "cwd", "--pwd", "/work"])
        binds = [argv[i + 1] for i, arg in enumerate(argv) if arg == "--bind"][1:]  # after /opt/cloudsweep
        self.assertEqual(binds, ["%s/inputs:/store/inputs:ro" % store,
                                 "%s/submissions/%s:/store/submissions/%s:ro" % (store, own, own),
                                 "%s/runs/%s:/store/runs/%s" % (store, own, own)])

    def test_a_runtime_that_cannot_leave_the_start_directory_unmounted_is_refused(self):
        self.given_image()
        os.environ["FAKE_APPTAINER_HELP"] = "Usage: apptainer exec [exec options...] <container> <command>"
        _, out = self.submit("given", expect=1)
        self.assertIn("has no --no-mount option", out)
        self.assertRegex(self.cli("doctor", "--on", "given")[1], r"apptainer +-- +\S+ lacks --no-mount")

    def test_binds_are_checked_on_the_machine_recorded_and_given_to_commands_only(self):
        (self.home / "models").mkdir()
        (self.root / "venv").mkdir()
        self.given_image()
        with open(str(self.repo / ".cloudsweep/machines.ini"), "a") as stream:
            stream.write("binds = ~/models:/models:ro, %s\n" % (self.root / "venv"))
        submission, _ = self.submit("given")
        expected = ["%s:/models:ro" % (self.home / "models"), "%s:%s:rw" % (self.root / "venv", self.root / "venv")]
        self.assertEqual(self.receipt(submission)["binds"], expected)
        self.wait_for_pool(self.root / "given", submission)
        self.assertEqual(self.cli("fetch", submission, str(self.root / "results"))[0], 0)
        log = self.log.read_text()
        self.assertIn("apptainer exec /models /opt/cloudsweep /store/inputs /store/runs/SUB /store/submissions/SUB "
                      "%s /work\n" % (self.root / "venv"), log)  # each command
        self.assertIn("apptainer exec /opt/cloudsweep /work\n", log)  # the build gets no binds
        self.assertRegex(self.cli("doctor", "--on", "given")[1], r"binds +ok +%s" % re.escape(str(self.home)))
        (self.home / "models").rmdir()
        code, out = self.cli("submit", "--on", "given", "--dry-run", "--commands", str(self.sweep), "--input", "trace.bin")
        self.assertEqual(code, 1, out)
        self.assertIn("binds: ~/models does not exist on given", out)
        file = self.repo / ".cloudsweep/machines.ini"
        for text, message in (("binds = /data:/work/src\n", "would cover /work"),
                              ("binds = /opt\n", "would cover"),
                              ("binds = /data:relative\n", "binds must be"),
                              ("binds = /data:/data:rx\n", "binds must be")):
            file.write_text("[m]\nkind = ssh\ncontainer = ~/x.sif\n" + text)
            with self.assertRaisesRegex(project.Refusal, message):
                machines.load(self.repo, "m")
        file.write_text("[m]\nkind = ssh\nbinds = /data\n")
        with self.assertRaisesRegex(project.Refusal, "binds need a container"):
            machines.load(self.repo, "m")

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

    def test_slurm_fields_are_read_as_slurm_prints_them(self):
        self.assertEqual([remote.slurm_value(text) for text in ("(null)", "None", "N/A", "", "None assigned", "node64")],
                         [None, None, None, None, None, "node64"])
        self.assertEqual(remote.slurm_time("2026-11-11T09:12:27"), "2026-11-11T09:12:27Z")
        self.assertIsNone(remote.slurm_time("Tue 20:43"))  # not UTC in ISO 8601: never guessed
        self.assertEqual(remote.task_indices("4242_7"), [7])
        self.assertEqual(remote.task_indices("4242_[0-3,5%2]"), [0, 1, 2, 3, 5])
        self.assertEqual(remote.allocated("billing=2000,cpu=8,gres/gpu=1,mem=96G,node=1"), (2000, 1))
        self.assertEqual(remote.allocated("billing=8,cpu=8,gres/gpu:h100=1,gres/gpu=1,node=1"), (8, 1))
        self.assertEqual(remote.allocated("billing=2,cpu=2,gres/gpu:a=1,gres/gpu:b=2,node=1"), (2, 3))
        self.assertEqual(remote.allocated(""), (None, 0))

    def test_a_reused_process_id_is_not_taken_for_the_pool(self):
        site = mock.Mock(submission="20261106T000000Z-aaaaaa")
        self.assertFalse(remote.pool_alive(site, {"pid": os.getpid(), "pgid": os.getpid()}))
        self.assertFalse(remote.pool_alive(site, {"pid": os.getpid(), "finished": "now"}))


if __name__ == "__main__":
    unittest.main()
