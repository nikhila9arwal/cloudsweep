"""The worker's build and run modes and fetch's verification, in-process with a local store
standing in for the bucket (no container, no AWS)."""

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from helpers import PACKAGE, core, git, make_repo

sys.path.insert(0, str(PACKAGE))
import worker  # noqa: E402
from cloudsweep import fetch  # noqa: E402

SIM = """#!/bin/sh
# usage: sim OUT INPUT CODE
echo "input: $(cat "$2")" > "$1/result.txt"
mkdir -p "$1/sub" && echo nested > "$1/sub/n.txt"
echo "to stdout"
exit "$3"
"""
RECIPE = {"command": "mkdir -p build && cp sim.sh build/sim", "binary": "build/sim"}
DIGEST = "sha256:" + "1" * 64


class Fixture:
    """A built work directory (as inside a revision image) and a store with one submission."""

    def __init__(self, root, commands, inputs=None, recipe=RECIPE):
        self.root = Path(root)
        self.commit = make_repo(self.root / "repo", {"sim.sh": SIM, "README": "x\n"})
        self.work = self.root / "work"
        self.work.mkdir()
        git(self.root, "clone", "--quiet", str(self.root / "repo"), str(self.work / "src"))
        (self.work / core.RECIPE).write_text(json.dumps(recipe))
        assert worker.build_mode(str(self.work)) == 0
        self.store = core.LocalStore(self.root / "store")
        self.submission = core.new_submission_id()
        staged = {}
        for name, text in (inputs or {}).items():
            path = self.root / "in" / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
            staged[name] = core.sha256_file(path)
            self.store.put_file(path, "inputs/" + staged[name])
        manifest = core.submission_manifest(self.submission, self.commit, DIGEST, "sha256:" + "2" * 64, recipe,
                                            staged, commands, 1, 512, 1)
        self.store.put_bytes(core.commands_bytes(commands), "submissions/%s/commands.jsonl" % self.submission)
        self.store.put_bytes(core.pretty(manifest), "submissions/%s/submission.json" % self.submission)

    def run(self, child=0, attempt="1", digest=DIGEST):
        """One child in a fresh copy of the built work directory, as each Batch job is a fresh container."""
        self.runs = getattr(self, "runs", 0) + 1
        work = self.root / ("container-%d" % self.runs)
        shutil.copytree(str(self.work), str(work), symlinks=True)
        env = {"IMAGE_DIGEST": digest, "AWS_BATCH_JOB_ARRAY_INDEX": str(child), "AWS_BATCH_JOB_ATTEMPT": attempt}
        return worker.run_mode(self.store, self.submission, env, str(work))

    def fetch(self):
        return fetch.fetch(self.store, self.submission, self.root / "results")


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.scratch)

    def test_build_records_commit_binary_and_tracked_state(self):
        fixture = Fixture(self.scratch, [["{bin}", "{out}", "README", "0"]])
        record = json.loads((fixture.work / "build.json").read_text())
        self.assertEqual(record["commit"], fixture.commit)
        self.assertEqual(record["binary"]["sha256"], core.sha256_file(fixture.work / "src/build/sim"))
        self.assertEqual(record["tracked_changes"], "")

    def test_a_sweep_runs_each_child_and_fetch_verifies_everything(self):
        commands = [["{bin}", "{out}", "{input:data/in.txt}", "0"], ["{bin}", "{out}", "README", "0"]]
        fixture = Fixture(self.scratch, commands, {"data/in.txt": "hello"})
        self.assertEqual(fixture.run(0), 0)
        fixture.run(1)
        summary = fixture.fetch()
        self.assertTrue(summary["complete"], summary)
        out = Path(self.scratch) / "results" / fixture.submission / "0" / "1"
        self.assertEqual((out / "out/result.txt").read_text(), "input: hello\n")
        self.assertEqual((out / "out/sub/n.txt").read_text(), "nested\n")
        self.assertEqual((out / "stdout.txt").read_text(), "to stdout\n")
        manifest = json.loads((out / "manifest.json").read_text())
        self.assertEqual((manifest["status"], manifest["commit"]), ("succeeded", fixture.commit))

    def test_failures_are_recorded_and_fetch_reports_them(self):
        fixture = Fixture(self.scratch, [["{bin}", "{out}", "README", "3"]])
        self.assertEqual(fixture.run(), 3)
        summary = fixture.fetch()
        self.assertFalse(summary["complete"])
        self.assertEqual(summary["children"][0]["verdict"], "failed")

    def test_a_retry_gets_its_own_attempt_directory(self):
        fixture = Fixture(self.scratch, [["{bin}", "{out}", "README", "0"]])
        fixture.run(attempt="1")
        fixture.run(attempt="1")  # the same Batch attempt number, e.g. a rerun after an upload race
        attempts = [a["attempt"] for a in fixture.fetch()["children"][0]["attempts"]]
        self.assertEqual(attempts, ["1", "1-2"])

    def test_wrong_image_or_tampered_binary_never_runs_the_command(self):
        fixture = Fixture(self.scratch, [["{bin}", "{out}", "README", "0"]])
        self.assertNotEqual(fixture.run(digest="sha256:" + "9" * 64), 0)
        (fixture.work / "src/build/sim").write_text("#!/bin/sh\nexit 0\n")
        self.assertNotEqual(fixture.run(), 0)
        errors = [a.get("error") or "" for a in fixture.fetch()["children"][0]["attempts"]]
        self.assertIn("not the submission's image", errors[0])
        self.assertIn("binary differs", errors[1])

    def test_inputs_cannot_replace_files_of_the_revision(self):
        fixture = Fixture(self.scratch, [["{bin}", "{out}", "{input:README}", "0"]], {"README": "evil"})
        self.assertNotEqual(fixture.run(), 0)
        self.assertIn("would replace", fixture.fetch()["children"][0]["attempts"][0]["error"])

    def test_projects_without_a_binary_run_any_command(self):
        recipe = {"command": "", "binary": None}
        fixture = Fixture(self.scratch, [["sh", "sim.sh", "{out}", "README", "0"]], recipe=recipe)
        self.assertEqual(fixture.run(), 0)
        self.assertTrue(fixture.fetch()["complete"])

    def test_fetch_detects_a_changed_output(self):
        fixture = Fixture(self.scratch, [["{bin}", "{out}", "README", "0"]])
        fixture.run()
        key = "runs/%s/0/1/out/result.txt" % fixture.submission
        fixture.store.put_bytes(b"changed\n", key)
        summary = fixture.fetch()
        self.assertEqual(summary["children"][0]["verdict"], "verification failed")


class CheckTests(unittest.TestCase):
    def test_check_reports_python_and_git(self):
        self.assertEqual(worker.check_mode(), 0)


if __name__ == "__main__":
    unittest.main()
