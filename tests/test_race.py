"""wait and race: the decisions on synthetic observations, and races end to end against the fake
ssh, Slurm and pools of test_machines."""

import json
import os
import re
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

from test_machines import MachineFixture

from cloudsweep import race

UNKNOWN = ({"started": None, "start": None, "ended": None, "end": None, "outcome": None, "stopped": None}, None)


def seen(started=False, start=None, ended=False, end=None, outcome=None, stopped=False, final=None):
    """An observation and a Slurm status row whose accounting is final when the copy has ended."""
    row = {"kind": "slurm", "accounting": {"final": ended if final is None else final,
                                           "totals": {"elapsed_s": 60, "gpu_hours": 0.5, "billing_unit_hours": 9}}}
    return ({"started": started, "start": start, "ended": ended, "end": end, "outcome": outcome,
             "stopped": stopped}, row)


def ran(start, end=None, outcome="failed"):
    if end is None:
        return seen(True, start)
    return seen(True, start, True, end, outcome, True)


QUEUED = seen()
NEVER = seen(False, None, True, None, "never started", True)


class DecisionTests(unittest.TestCase):
    def new(self, keep, copies="abc"):
        return {"id": "R1", "keep": keep, "copies": list(copies), "phase": "waiting", "runner": None, "kept": None,
                "roles": {copy: "waiting" for copy in copies}, "seen": {copy: {} for copy in copies},
                "accounting": {}, "events": []}

    def turn(self, state, **found):
        with mock.patch("sys.stderr"):
            race.step(state, found)
            return race.cleanup(state, found)

    def test_first_start_keeps_the_earliest_known_start_and_cancels_the_rest(self):
        state = self.new("first-start")
        retiring = self.turn(state, a=QUEUED, b=ran("2026-11-11T10:00:05Z"), c=UNKNOWN)
        self.assertEqual((state["kept"], retiring), ("b", ["a", "c"]))  # c could not be seen: not waited for
        kept = [e for e in state["events"] if e["event"] == "kept"][0]
        self.assertEqual(kept["unknown"], ["c"])
        self.turn(state, a=NEVER, b=ran("2026-11-11T10:00:05Z"), c=seen(True, "2026-11-11T09:59:00Z", stopped=True,
                                                                       final=False))
        self.assertEqual(state["roles"], {"a": "cancelled", "b": "kept", "c": "cancelled"})
        self.assertEqual(state["accounting"], {"a": {"elapsed_s": 0, "gpu_hours": 0}})  # c's not final yet
        self.assertEqual(state["phase"], "cleanup")
        self.turn(state, a=NEVER, b=ran("2026-11-11T10:00:05Z"),
                  c=ran("2026-11-11T09:59:00Z", "2026-11-11T10:01:00Z", "failed"))
        self.assertEqual(state["accounting"]["c"], {"elapsed_s": 60, "gpu_hours": 0.5, "billing_unit_hours": 9})
        self.assertEqual(state["phase"], "done")

    def test_starts_in_the_same_second_go_by_tie_order(self):
        state = self.new("first-start")
        self.turn(state, a=QUEUED, b=ran("2026-11-11T10:00:05Z"), c=ran("2026-11-11T10:00:05Z"))
        self.assertEqual(state["kept"], "b")

    def test_first_success_recovers_from_a_failing_runner(self):
        state = self.new("first-success")
        self.turn(state, a=ran("2026-11-11T10:00:00Z"), b=QUEUED, c=QUEUED)
        self.assertEqual((state["phase"], state["runner"]), ("running", "a"))
        # a fails at 10:00:05; b starts in that second, after the end: not an overlap
        self.turn(state, a=ran("2026-11-11T10:00:00Z", "2026-11-11T10:00:05Z"), b=ran("2026-11-11T10:00:05Z"), c=QUEUED)
        self.assertEqual((state["runner"], state["roles"]["a"]), ("b", "dropped"))
        self.turn(state, a=ran("2026-11-11T10:00:00Z", "2026-11-11T10:00:05Z"),
                  b=ran("2026-11-11T10:00:05Z", "2026-11-11T10:30:00Z", "succeeded"), c=QUEUED)
        self.assertEqual((state["kept"], state["roles"]["c"]), ("b", "retiring"))
        self.turn(state, a=ran("2026-11-11T10:00:00Z", "2026-11-11T10:00:05Z"),
                  b=ran("2026-11-11T10:00:05Z", "2026-11-11T10:30:00Z", "succeeded"), c=NEVER)
        self.assertEqual(state["phase"], "done")
        self.assertEqual(set(state["accounting"]), {"a", "c"})

    def test_a_copy_that_starts_while_the_runner_runs_is_retired_and_never_chosen(self):
        state = self.new("first-success")
        self.turn(state, a=ran("2026-11-11T10:00:00Z"), b=QUEUED, c=QUEUED)
        self.turn(state, a=UNKNOWN, b=ran("2026-11-11T10:01:00Z"), c=QUEUED)  # the runner cannot be seen
        self.assertEqual(state["roles"]["b"], "waiting")  # nothing is judged against it
        self.turn(state, a=ran("2026-11-11T10:00:00Z", "2026-11-11T10:02:00Z"), b=ran("2026-11-11T10:01:00Z"),
                  c=QUEUED)
        self.assertEqual((state["roles"]["b"], state["roles"]["a"], state["phase"]), ("retiring", "dropped", "waiting"))
        self.turn(state, a=ran("2026-11-11T10:00:00Z", "2026-11-11T10:02:00Z"), b=ran("2026-11-11T10:01:00Z"),
                  c=ran("2026-11-11T10:05:00Z"))
        self.assertEqual(state["runner"], "c")

    def test_a_start_after_the_runner_was_last_seen_running_waits_for_the_runner(self):
        def asked(at, found):
            return dict(found[0], at=at), found[1]
        state = self.new("first-success")
        self.turn(state, a=asked("2026-11-11T10:00:30Z", ran("2026-11-11T10:00:00Z")), b=QUEUED, c=QUEUED)
        # a, asked first, ran at 10:01:00; it may have ended before b, asked later, started at 10:01:30
        self.turn(state, a=asked("2026-11-11T10:01:00Z", ran("2026-11-11T10:00:00Z")),
                  b=asked("2026-11-11T10:01:40Z", ran("2026-11-11T10:01:30Z")), c=QUEUED)
        self.assertEqual(state["roles"]["b"], "waiting")
        self.turn(state, a=asked("2026-11-11T10:02:00Z", ran("2026-11-11T10:00:00Z", "2026-11-11T10:01:10Z")),
                  b=asked("2026-11-11T10:02:10Z", ran("2026-11-11T10:01:30Z")), c=QUEUED)
        self.assertEqual((state["roles"]["a"], state["runner"]), ("dropped", "b"))  # no overlap: b runs
        # c starts before b, the runner, was seen running: an overlap, shown without waiting
        self.turn(state, a=asked("2026-11-11T10:03:00Z", ran("2026-11-11T10:00:00Z", "2026-11-11T10:01:10Z")),
                  b=asked("2026-11-11T10:03:00Z", ran("2026-11-11T10:01:30Z")),
                  c=asked("2026-11-11T10:03:05Z", ran("2026-11-11T10:02:30Z")))
        self.assertEqual(state["roles"]["c"], "retiring")

    def test_a_copy_seen_late_that_overlapped_an_earlier_runner_is_retired(self):
        state = self.new("first-success")
        self.turn(state, a=ran("2026-11-11T10:00:00Z"), b=UNKNOWN, c=QUEUED)
        self.turn(state, a=ran("2026-11-11T10:00:00Z", "2026-11-11T10:02:00Z"), b=UNKNOWN, c=QUEUED)
        self.assertEqual((state["roles"]["a"], state["phase"]), ("dropped", "waiting"))
        self.turn(state, a=ran("2026-11-11T10:00:00Z", "2026-11-11T10:02:00Z"), b=ran("2026-11-11T10:01:00Z"),
                  c=ran("2026-11-11T10:05:00Z"))
        self.assertEqual((state["roles"]["b"], state["runner"]), ("retiring", "c"))

    def test_a_pool_s_elapsed_time_is_its_end_minus_its_start(self):
        state = self.new("first-start", "ab")
        pool = ({"started": True, "start": "2026-11-11T10:00:00Z", "ended": True, "end": "2026-11-11T10:01:00Z",
                 "outcome": "failed", "stopped": True}, {"kind": "ssh"})
        self.turn(state, a=ran("2026-11-11T09:59:00Z"), b=pool)
        self.assertEqual(state["accounting"]["b"], {"elapsed_s": 60})

    def test_a_race_whose_copies_never_start_is_lost(self):
        state = self.new("first-success", "ab")
        self.turn(state, a=NEVER, b=QUEUED)
        self.assertEqual(state["roles"], {"a": "dropped", "b": "waiting"})
        self.turn(state, a=NEVER, b=NEVER)
        self.assertEqual(state["phase"], "lost")

    def test_accounting_that_never_becomes_final_is_recorded_unavailable(self):
        state = self.new("first-start", "ab")
        late = seen(True, "2026-11-11T09:00:00Z", stopped=True, final=False)
        self.turn(state, a=ran("2026-11-11T09:00:00Z"), b=late)
        self.assertEqual(state["phase"], "cleanup")
        state["seen"]["b"]["stopped"] = "2026-11-11T09:00:00Z"  # stopped long ago
        self.turn(state, a=ran("2026-11-11T09:00:00Z"), b=late)
        self.assertEqual((state["accounting"]["b"], state["phase"]), ({"unavailable": True}, "done"))


FAKE_SCANCEL = """#!/bin/sh
echo scancel "$@" >> "$FAKE_LOG"
rm -f "$FAKE_SLURM/queue.txt"
echo "4242_[0-2]|CANCELLED by 1234|None|{t:%d}|0|None assigned|cpu||0:0" >> "$FAKE_SLURM/accounting.txt"
"""


class RaceFixture(MachineFixture):
    def setUp(self):
        super().setUp()
        (self.root / "bin/scancel").write_text(FAKE_SCANCEL % int(time.time()))
        patcher = mock.patch.object(race, "LEAST_POLL", 0)
        patcher.start()
        self.addCleanup(patcher.stop)

    def held_copy(self):
        os.environ["FAKE_HOLD"] = "1"
        copy, _ = self.submit("cluster")
        del os.environ["FAKE_HOLD"]
        (self.slurm / "queue.txt").write_text("0|PENDING|Priority|N/A||\n1|PENDING|Priority|N/A||\n"
                                              "2|PENDING|Priority|N/A||\n")
        return copy


class RaceTests(RaceFixture):
    def test_first_start_keeps_the_pool_and_cancels_the_queued_copy(self):
        queued = self.held_copy()
        pool, _ = self.submit("box")
        self.wait_for_pool(self.root / "box", pool)
        code, out, err = self.cli_streams("race", queued, pool, "--keep", "first-start", "--poll", "0", "--json")
        self.assertEqual(code, 0, err)
        result = json.loads(out)
        self.assertEqual((result["result"], result["kept"]), ("done", pool))
        self.assertIn("scancel 4242", self.log.read_text())
        record = json.loads((self.repo / ".cloudsweep/races" / (result["race"] + ".json")).read_text())
        self.assertEqual(record["roles"], {queued: "cancelled", pool: "kept"})
        self.assertEqual(record["accounting"][queued], {"elapsed_s": 0, "gpu_hours": 0})
        mirror = self.receipt(queued)["race"]
        self.assertEqual((mirror["id"], mirror["role"]), (result["race"], "cancelled"))
        self.assertEqual([e["event"] for e in mirror["events"]],
                         ["retiring", "cancel requested", "ended", "cancelled", "accounted"])
        self.assertTrue(all(re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", e["at"]) for e in mirror["events"]))
        line = self.cli("status", pool)[1]
        self.assertIn("race %s (first-start, done): kept; %s cancelled" % (result["race"], queued), line)
        code, out, err = self.cli_streams("race", pool, "--json")  # an ended race gives its outcome again
        self.assertEqual((code, json.loads(out)["kept"]), (0, pool))

    def test_copies_of_different_jobs_and_of_two_races_are_refused(self):
        first, _ = self.submit("box")
        self.sweep.write_text('["sh", "-c", "true"]\n')
        other, _ = self.submit("box")
        code, out, err = self.cli_streams("race", first, other, "--keep", "first-start", "--json")
        self.assertEqual(code, 1)
        self.assertIn("differ in template_sha256", json.loads(out)["error"])
        self.assertIn("give --keep", self.cli("race", first, other)[1])

    def test_a_race_another_process_controls_is_refused(self):
        queued = self.held_copy()
        pool, _ = self.submit("box")
        with mock.patch.object(race, "poll", return_value={queued: QUEUED, pool: QUEUED}), \
                mock.patch.object(race, "sleep_until", side_effect=KeyboardInterrupt):
            code, out, err = self.cli_streams("race", queued, pool, "--keep", "first-success", "--json")
        self.assertEqual((code, json.loads(out)["result"]), (1, "interrupted"))
        name = json.loads(out)["race"]
        holder = subprocess.Popen([sys.executable, "-c", "import fcntl, sys, time; s = open(sys.argv[1], 'a+'); "
                                   "fcntl.lockf(s, fcntl.LOCK_EX); print('held', flush=True); time.sleep(30)",
                                   str(self.repo / ".cloudsweep/races" / (name + ".lock"))], stdout=subprocess.PIPE)
        self.addCleanup(holder.kill)
        holder.stdout.readline()
        code, out, err = self.cli_streams("race", queued, "--json")
        self.assertEqual(code, 1)
        self.assertIn("another race process controls this race", json.loads(out)["error"])

    def test_a_waiting_race_takes_new_copies_and_a_new_policy(self):
        queued = self.held_copy()
        pool, _ = self.submit("box")
        later, _ = self.submit("box")
        unseen = {queued: QUEUED, pool: QUEUED, later: QUEUED}
        with mock.patch.object(race, "poll", return_value=unseen), \
                mock.patch.object(race, "sleep_until", side_effect=KeyboardInterrupt):
            code, out, _ = self.cli_streams("race", queued, pool, "--keep", "first-success", "--json")
            name = json.loads(out)["race"]
            code, out, err = self.cli_streams("race", pool, later, "--keep", "first-start", "--json")
            self.assertIn("give every copy still in it", json.loads(out)["error"])
            code, out, err = self.cli_streams("race", pool, queued, later, "--keep", "first-start", "--json")
        record = json.loads((self.repo / ".cloudsweep/races" / (name + ".json")).read_text())
        self.assertEqual((record["keep"], record["copies"]), ("first-start", [pool, queued, later]))
        started = dict(unseen, **{queued: ran("2026-11-11T10:00:00Z")})
        with mock.patch.object(race, "poll", return_value=started):
            code, out, err = self.cli_streams("race", pool, queued, later, "--keep", "first-success", "--json")
        self.assertIn("can change only while every copy is queued", json.loads(out)["error"])


class RecoveryTests(RaceFixture):
    """A race that is resumed, changed or found ended by another process."""

    def start(self, *copies, keep="first-success"):
        with mock.patch.object(race, "poll", return_value={copy: QUEUED for copy in copies}), \
                mock.patch.object(race, "sleep_until", side_effect=KeyboardInterrupt):
            code, out, err = self.cli_streams("race", *copies, "--keep", keep, "--json")
        self.assertEqual(json.loads(out)["result"], "interrupted", err)
        return json.loads(out)["race"]

    def record(self, name):
        return json.loads((self.repo / ".cloudsweep/races" / (name + ".json")).read_text())

    def test_copies_given_in_another_order_resume_the_race_unchanged(self):
        queued, pool = self.held_copy(), self.submit("box")[0]
        name = self.start(queued, pool)
        with mock.patch.object(race, "poll", return_value={queued: QUEUED, pool: QUEUED}), \
                mock.patch.object(race, "sleep_until", side_effect=KeyboardInterrupt):
            self.cli_streams("race", pool, queued, "--json")
        self.assertEqual(self.record(name)["copies"], [queued, pool])
        self.assertNotIn("changed", [e["event"] for e in self.record(name)["events"]])

    def test_a_copy_cancelled_while_no_controller_ran_does_not_block_a_change(self):
        queued, first, second = self.held_copy(), self.submit("box")[0], self.submit("box")[0]
        name = self.start(queued, first)
        with mock.patch.object(race, "poll", return_value={queued: NEVER, first: QUEUED, second: QUEUED}), \
                mock.patch.object(race, "sleep_until", side_effect=KeyboardInterrupt):
            code, out, err = self.cli_streams("race", first, second, "--keep", "first-start", "--json")
        record = self.record(name)
        self.assertEqual((record["keep"], record["copies"], record["roles"][queued]),
                         ("first-start", [first, second, queued], "dropped"), err)

    def test_a_change_keeps_the_order_given_compares_with_the_first_copy_and_keeps_the_deadline(self):
        queued, pool, later = self.held_copy(), self.submit("box")[0], self.submit("box")[0]
        name = self.start(queued, pool)
        deadlines = []

        def poll(repo, submissions, extra, deadline=None):
            deadlines.append(deadline)
            return {queued: QUEUED, pool: QUEUED, later: QUEUED}
        with mock.patch.object(race, "poll", poll), mock.patch.object(race, "sleep_until", side_effect=KeyboardInterrupt):
            self.cli_streams("race", later, pool, queued, "--keep", "first-start", "--timeout", "1", "--json")
        self.assertEqual(self.record(name)["copies"], [later, pool, queued])  # the new copy where it was given
        self.assertTrue(deadlines and deadlines[0] is not None)  # the registration's poll counts toward --timeout
        self.sweep.write_text('["sh", "-c", "true"]\n')
        other = self.submit("box")[0]  # another job
        with mock.patch.object(race, "poll", return_value={queued: NEVER, pool: NEVER, later: NEVER, other: QUEUED}):
            code, out, err = self.cli_streams("race", other, "--keep", "first-start", "--json")
        self.assertEqual(code, 1)  # one copy alone is no race, and it never joins this one
        with mock.patch.object(race, "poll", return_value={queued: NEVER, pool: NEVER, later: NEVER, other: QUEUED}):
            code, out, err = self.cli_streams("race", later, other, "--keep", "first-start", "--json")
        self.assertIn("differ in template_sha256", json.loads(out)["error"])  # all dropped: still the first's job

    def test_a_busy_registry_is_waited_for_only_until_the_timeout(self):
        queued, pool = self.held_copy(), self.submit("box")[0]
        (self.repo / ".cloudsweep/races").mkdir(parents=True)
        holder = subprocess.Popen([sys.executable, "-c", "import fcntl, sys, time; s = open(sys.argv[1], 'a+'); "
                                   "fcntl.lockf(s, fcntl.LOCK_EX); print('held', flush=True); time.sleep(30)",
                                   str(self.repo / ".cloudsweep/races/registry.lock")], stdout=subprocess.PIPE)
        self.addCleanup(holder.kill)
        holder.stdout.readline()
        began = time.monotonic()
        code, out, err = self.cli_streams("race", queued, pool, "--keep", "first-start", "--timeout", "0.02", "--json")
        self.assertEqual(code, 1)
        self.assertIn("registering copies", json.loads(out)["error"])
        self.assertLess(time.monotonic() - began, 10)

    def test_a_race_another_controller_ended_meanwhile_is_not_decided_again(self):
        queued, pool = self.held_copy(), self.submit("box")[0]
        name = self.start(queued, pool)
        real = race.Lock

        def lock(path, wait, deadline=None):  # as another controller finishes between the read and the lock
            held = real(path, wait, deadline)
            if path.name == name + ".lock":
                ended = dict(self.record(name), phase="done", kept=queued)
                race.write_atomic(path.with_suffix(".json"), ended)
            return held
        with mock.patch.object(race, "Lock", side_effect=lock), \
                mock.patch.object(race, "poll", side_effect=AssertionError("polled a race that had ended")):
            code, out, err = self.cli_streams("race", queued, pool, "--json")
        self.assertEqual((code, json.loads(out)["kept"]), (0, queued), err)

    def test_an_ended_race_repairs_its_copies_receipts(self):
        queued, pool = self.held_copy(), self.submit("box")[0]
        name = self.start(queued, pool)
        race.write_atomic(self.repo / ".cloudsweep/races" / (name + ".json"), dict(self.record(name), phase="lost"))
        receipt = self.receipt(pool)
        del receipt["race"]
        (self.repo / ".cloudsweep/receipts" / (pool + ".json")).write_text(json.dumps(receipt))
        code, out, err = self.cli_streams("race", pool, "--json")
        self.assertEqual((code, json.loads(out)["result"]), (4, "lost"))
        self.assertEqual(self.receipt(pool)["race"]["id"], name)


class WaitTests(MachineFixture):
    def test_wait_until_running_and_until_done(self):
        mock.patch.object(race, "LEAST_POLL", 0).start()
        self.addCleanup(mock.patch.stopall)
        self.sweep.write_text('["sh", "-c", "sleep 2"]\n')
        submission, _ = self.submit("box")
        code, out, err = self.cli_streams("wait", submission, "--poll", "0", "--json")
        self.assertEqual((code, json.loads(out)["result"]), (0, "reached"))
        self.assertTrue(json.loads(out)["observation"]["started"])
        code, out, err = self.cli_streams("wait", submission, "--until", "done", "--poll", "0", "--json")
        self.assertEqual((code, json.loads(out)["outcome"]), (0, "succeeded"))
        os.environ["FAKE_HOLD"] = "1"
        held, _ = self.submit("cluster")
        (self.slurm / "queue.txt").write_text("0|PENDING|Priority|N/A||\n")
        # the deadline (3 s) comes during the sleep after the first poll: no poll is started after it
        code, out, err = self.cli_streams("wait", held, "--poll", "10", "--timeout", "0.05", "--json")
        self.assertEqual((code, json.loads(out)["result"], json.loads(out)["observation"]["started"]),
                         (3, "timeout", False))
        self.assertIn(held + " queued", err)
        self.assertNotIn("no answer", err)
        process = subprocess.Popen([sys.executable, "-m", "cloudsweep", "wait", held, "--poll", "10", "--json"],
                                   cwd=str(self.repo), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   env=dict(os.environ, PYTHONPATH=str(Path(race.__file__).parents[1])))
        for _ in range(100):  # until its first poll has been reported
            if held.encode() in (process.stderr.peek() if hasattr(process.stderr, "peek") else b""):
                break
            time.sleep(0.1)
        process.terminate()  # SIGTERM, as a service manager stops it
        out, _ = process.communicate(timeout=30)
        self.assertEqual((process.returncode, json.loads(out)["result"]), (1, "interrupted"))
        code, out, err = self.cli_streams("wait", "20261111T000000Z-abcdef", "--on", "aws", "--json")
        self.assertEqual((code, json.loads(out)["result"]), (1, "refused"))


if __name__ == "__main__":
    unittest.main()
