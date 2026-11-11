"""Wait for a submission on a machine, and race copies of one job across machines or partitions.

  cloudsweep wait SUBMISSION --until running      (until it runs or ends; --until done: until it ends)
  cloudsweep race A B C --keep first-start        (keep the first copy to start; cancel the others)
  cloudsweep race A B --keep first-success        (one copy runs at a time; the others wait as backups)

A copy is an ordinary submission, as `submit --json` prints it; `submit --partition` puts two copies
on two partitions of one Slurm machine. Times are UTC. Run `race` again with any of its copies to
resume it. See docs/machines.md#waiting-and-racing.
"""

import argparse
import datetime
import errno
import fcntl
import json
import os
import signal
import sys
import tempfile
import time

from . import core, machines, project, settings
from .project import Refusal

POLL, LEAST_POLL = 60, 10  # seconds between polls
ALLOWANCE = 15 * 60  # seconds a stopped copy's accounting may take to become final
WAITING_STATES = ("PENDING", "REQUEUE_HOLD", "REQUEUED")
EXIT = {"reached": 0, "done": 0, "refused": 1, "interrupted": 1, "timeout": 3, "lost": 4}
POLICIES = ("first-start", "first-success")


def observe(row):
    """What one status row says of a copy. started and ended are True, False, or None when not
    known (the machine did not answer, or accounting is not final yet); start and end are the
    scheduler's times; stopped says nothing of it can still run; outcome is set once it ended."""
    found = {"started": None, "start": None, "ended": None, "end": None, "outcome": None, "stopped": None}
    state = str(row.get("state", "UNKNOWN")) if isinstance(row, dict) else "UNKNOWN"
    if row is None or row.get("error") or state.startswith(("UNKNOWN", "NOT STARTED")):
        return found
    children = row.get("children") or {}

    def outcome(started):
        if not started:
            return "never started"
        return "succeeded" if children.get("succeeded") == row.get("commands") else "failed"
    if row.get("kind") == "slurm":
        if not row.get("job"):  # stopped before sbatch, or not submitted
            return dict(found, started=False, ended=True, outcome="never started", stopped=True) \
                if state == "CANCELLED" else found
        queue, accounting = row.get("queue") or [], row.get("accounting") or {}
        starts = [task["start"] for task in queue if task["state"] not in WAITING_STATES and task["start"]]
        starts += [task["start"] for task in accounting.get("tasks") or [] if task["start"]]
        start = min(starts) if starts else None
        if queue:
            return dict(found, started=bool(start) if start or "error" not in accounting else None, start=start,
                        ended=False, stopped=False)
        found.update(start=start, started=True if start else None, stopped=True)
        if accounting.get("final"):
            ends = [task["end"] for task in accounting["tasks"] if task["end"]]
            found.update(started=bool(start), ended=True, end=max(ends) if ends else None, outcome=outcome(start))
        return found
    if state == "RUNNING":
        return dict(found, started=True, start=row.get("started"), ended=False, stopped=False)
    if row.get("orphans"):  # its supervisor died; commands still run
        return dict(found, started=True, start=row.get("started"), stopped=False)
    started = bool(row.get("started"))
    return dict(found, started=started, start=row.get("started"), ended=True, end=row.get("finished"),
                outcome=outcome(started), stopped=True)


def describe(seen):
    if seen["ended"]:
        return "ended%s, %s" % (" at " + seen["end"] if seen["end"] else "", seen["outcome"])
    if seen["started"]:
        return "started at %s%s" % (seen["start"], "" if seen["ended"] is False else ", end not yet known")
    return "queued" if seen["started"] is False else "not known (no answer, or accounting not final)"


def poll(repo, submissions, extra, deadline=None):
    """{submission: (observation, status row)}, one remote call per endpoint, each within the call
    limit or the time left before the deadline. A machine that does not answer gives unknowns. Each
    observation's "at" is when its endpoint was asked: a copy seen running was running then."""
    found = {}
    for machine, members in machines.groups(repo, submissions, extra):
        limit, asked = machines.call_limit(deadline), core.now_iso()
        try:
            if not limit:  # no time left: not asked, so not known
                raise Refusal("no time left before --timeout")
            rows = machines.call_status(machine, *members, accounting=True, timeout=limit)
        except Refusal as error:
            print("%s no answer from %s: %s" % (core.now_iso(), machine["name"], error), file=sys.stderr, flush=True)
            rows = {}
        for submission in members:
            found[submission] = (dict(observe(rows.get(submission)), at=asked), rows.get(submission))
    return found


def moment(text):
    return datetime.datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)


def seconds_since(text):
    return (datetime.datetime.now(datetime.timezone.utc) - moment(text)).total_seconds()


def output(args, value):
    if args.json:
        print(json.dumps(value, indent=2, sort_keys=True))


def sleep_until(deadline, seconds):
    """Sleep until the next poll, or the deadline if it comes first; whether time is left for that poll."""
    time.sleep(seconds if deadline is None else max(0, min(seconds, deadline - time.monotonic())))
    return deadline is None or time.monotonic() < deadline


def wait_main(args):
    """cloudsweep wait: poll one submission until it runs (or ends), or ends."""
    seen, result, error = None, None, None
    try:
        signal.signal(signal.SIGTERM, interrupt)
        repo = project.repository()
        if not core.SUBMISSION_RE.fullmatch(args.submission):
            raise Refusal("invalid submission id %r" % args.submission)
        machine = machines.AWS if args.on == machines.AWS else \
            machines.load(repo, args.on, args.machines) if args.on else machines.machine_for(repo, args.submission,
                                                                                            args.machines)
        if machine == machines.AWS:
            raise Refusal("wait is for machines; on AWS, `fetch --wait` waits for a submission")
        deadline = time.monotonic() + 60 * args.timeout if args.timeout else None
        last = None
        while True:
            limit = machines.call_limit(deadline)
            try:
                if not limit:
                    raise Refusal("no time left before --timeout")
                row = machines.call_status(machine, args.submission, accounting=True, timeout=limit).get(args.submission)
            except Refusal as refusal:
                print("%s no answer from %s: %s" % (core.now_iso(), machine["name"], refusal), file=sys.stderr)
                row = None
            seen = observe(row)
            if describe(seen) != last:
                last = describe(seen)
                print("%s %s %s" % (core.now_iso(), args.submission, last), file=sys.stderr, flush=True)
            if seen["ended"] or (args.until == "running" and seen["started"]):
                result = "reached"
                break
            if not sleep_until(deadline, max(LEAST_POLL, args.poll)):
                result = "timeout"
                break
    except Refusal as refusal:
        result, error = "refused", str(refusal)
        print("cloudsweep: %s" % refusal, file=sys.stderr)
    except KeyboardInterrupt:
        result = "interrupted"
    output(args, {"submission": args.submission, "result": result, "observation": seen, "error": error,
                  "outcome": seen["outcome"] if seen else None})
    return EXIT[result]


# The race file (one authoritative record) and the copies' receipts (mirrors of their part of it)

def races(repo):
    return settings.state_dir(repo) / "races"


def write_atomic(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=str(path.parent), prefix=".race-")
    with os.fdopen(handle, "wb") as stream:
        stream.write(core.pretty(value))
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, str(path))


def load_races(repo):
    found = []
    for path in sorted(races(repo).glob("R*.json")) if races(repo).is_dir() else []:
        try:
            found.append(json.loads(path.read_text()))
        except (OSError, ValueError) as error:
            raise Refusal("cannot read the race file %s: %s" % (path, error))
    return found


def reload(repo, race):
    return json.loads((races(repo) / (race["id"] + ".json")).read_text())


def interrupt(signum, frame):
    raise KeyboardInterrupt  # SIGTERM, as from a service manager, takes the interrupted path


def save(repo, race, mirrors_only=False):
    """The race file first, then each copy's mirror: its role and its events."""
    if not mirrors_only:
        write_atomic(races(repo) / (race["id"] + ".json"), race)
    for copy in race["copies"]:
        receipt = machines.receipt_of(repo, copy)
        if receipt is None:
            continue
        receipt["race"] = {"id": race["id"], "keep": race["keep"], "role": race["roles"][copy],
                           "events": [event for event in race["events"] if event.get("copy") == copy]}
        settings.write_receipt(settings.state_dir(repo) / "receipts", receipt, replace=True)


class Lock:
    """A process-held lock on a file (fcntl): the system releases it when this process exits or dies."""

    def __init__(self, path, wait, deadline=None):
        """wait: block until the lock is free (until the deadline, if one is given); else refuse at once."""
        path.parent.mkdir(parents=True, exist_ok=True)
        self.stream = open(str(path), "a+")
        while True:
            try:
                fcntl.lockf(self.stream, fcntl.LOCK_EX | (0 if wait and deadline is None else fcntl.LOCK_NB))
                return
            except OSError as error:
                held = error.errno in (errno.EACCES, errno.EAGAIN)
                expired = deadline is not None and time.monotonic() >= deadline
                if not held or not wait or expired:
                    self.stream.close()
                if not held:
                    raise Refusal("cannot lock %s (%s); race needs a file system that supports locks" % (path, error))
                if not wait:
                    raise Refusal("another race process controls this race (it holds %s); stop it, or let it finish"
                                  % path)
                if expired:
                    raise Refusal("another race command holds %s, registering copies; try again" % path)
            time.sleep(0.2)

    def release(self):
        self.stream.close()


def event(race, copy, name, **fields):
    entry = dict({"at": core.now_iso(), "copy": copy, "event": name}, **{k: v for k, v in fields.items() if v is not None})
    race["events"].append(entry)
    print("%s %s %s%s" % (entry["at"], copy or race["id"], name, "".join(
        " %s=%s" % (key, value) for key, value in sorted(fields.items()) if value is not None)), file=sys.stderr, flush=True)


# Decisions

def retire(race, copy, because):
    race["roles"][copy] = "retiring"
    event(race, copy, "retiring", because=because)


def keep(race, copy, unknown):
    race.update(kept=copy, phase="cleanup", runner=None)
    race["roles"][copy] = "kept"
    event(race, copy, "kept", unknown=unknown)
    for other in race["copies"]:
        if race["roles"][other] in ("waiting", "runner"):
            retire(race, other, "%s is kept" % copy)


def step(race, found):
    """Record what this poll learned, then decide, as the design's §4 says: elections on the known
    starts (an unknown copy is not waited for); a first-success copy is retired once it is shown to
    have started while the runner ran; nothing is judged against a runner that cannot be seen."""
    copies, roles, seen = race["copies"], race["roles"], race["seen"]
    for copy in copies:
        observation, facts = found[copy][0], seen[copy]
        if observation["start"] and "start" not in facts:
            facts["start"] = observation["start"]
            event(race, copy, "started", start=observation["start"])  # the scheduler's time; "at" is ours
        if observation["ended"] and "ended" not in facts:
            facts.update(ended=observation["end"] or core.now_iso(), outcome=observation["outcome"])
            event(race, copy, "ended", end=observation["end"], outcome=observation["outcome"])
        if observation["stopped"] and "stopped" not in facts:
            facts["stopped"] = core.now_iso()
        if roles[copy] == "waiting" and facts.get("outcome") == "never started":
            roles[copy] = "dropped"
            event(race, copy, "dropped", because="ended, never started")
    while True:
        waiting = [copy for copy in copies if roles[copy] == "waiting"]
        if race["phase"] == "waiting":
            for copy in waiting:  # seen only now, it started while an earlier runner ran
                start = seen[copy].get("start")
                ran = [run for run in race.get("runs", []) if start and start < run[1]]
                if ran:
                    retire(race, copy, "started while the runner %s ran" % ran[0][0])
            waiting = [copy for copy in copies if roles[copy] == "waiting"]
            started = [copy for copy in waiting if seen[copy].get("start")]
            if not started:
                if not waiting:
                    race["phase"] = "cleanup"
                    event(race, None, "no copy left")
                return
            first = min(started, key=lambda copy: (seen[copy]["start"], copies.index(copy)))
            unknown = [copy for copy in waiting if found[copy][0]["started"] is None] or None
            if race["keep"] == "first-start":
                keep(race, first, unknown)
                return
            race.update(runner=first, phase="running")
            roles[first] = "runner"
            event(race, first, "runner", unknown=unknown)
            continue
        if race["phase"] != "running":
            return
        runner = race["runner"]
        end = seen[runner].get("ended")
        if found[runner][0]["ended"] is not False and end is None:
            return  # the runner cannot be seen: nothing is judged against it
        # Endpoints are asked one after another, so a runner seen running may have ended before a
        # copy asked later started: a start overlaps it only before its end, or while it is not ended
        # before the moment it was seen running; a later start is judged when the runner is seen again.
        running_at = found[runner][0].get("at") or core.now_iso()
        for copy in waiting:  # the same second: an end comes before a start
            start = seen[copy].get("start")
            if start and start < (end or running_at):
                retire(race, copy, "started while the runner %s ran" % runner)
        if end is None:
            return
        if seen[runner]["outcome"] == "succeeded":
            keep(race, runner, None)
            return
        roles[runner], race["runner"], race["phase"] = "dropped", None, "waiting"
        race.setdefault("runs", []).append([runner, end])
        event(race, runner, "dropped", because="the runner ended: %s" % seen[runner]["outcome"])


def cleanup(race, found):
    """Retiring copies that have stopped are cancelled; every copy that is not kept and has stopped
    is accounted for; the race ends when cleanup has nothing left. Returns the copies to cancel."""
    roles, seen = race["roles"], race["seen"]
    for copy in race["copies"]:
        observation, row = found[copy]
        if roles[copy] == "retiring" and observation["stopped"]:
            roles[copy] = "cancelled"
            event(race, copy, "cancelled")
        if roles[copy] not in ("cancelled", "dropped") or copy in race["accounting"] or "stopped" not in seen[copy]:
            continue
        accounting = (row or {}).get("accounting") or {}
        if not seen[copy].get("start") and observation["started"] is False:
            record = {"elapsed_s": 0, "gpu_hours": 0}
        elif accounting.get("final") and "totals" in accounting:
            totals = accounting["totals"]
            record = {key: totals[key] for key in ("elapsed_s", "gpu_hours", "billing_unit_hours")}
        elif (row or {}).get("kind") == "ssh" and seen[copy].get("start") and observation["end"]:
            record = {"elapsed_s": int((moment(observation["end"]) - moment(seen[copy]["start"])).total_seconds())}
        elif seconds_since(seen[copy]["stopped"]) > ALLOWANCE:
            record = {"unavailable": True}
        else:
            continue
        race["accounting"][copy] = record
        event(race, copy, "accounting unavailable" if record.get("unavailable") else "accounted",
              **{key: value for key, value in record.items() if key != "unavailable"})
    if race["phase"] == "cleanup" and all(roles[copy] in ("cancelled", "dropped") and copy in race["accounting"]
                                          for copy in race["copies"] if copy != race["kept"]):
        race["phase"] = "done" if race["kept"] else "lost"
        event(race, race["kept"], race["phase"])
    return [copy for copy in race["copies"] if roles[copy] == "retiring"]


def cancel(repo, race, extra, copies, deadline):
    for copy in copies:
        first = not any(e["event"] == "cancel requested" and e.get("copy") == copy for e in race["events"])
        try:
            machine = machines.machine_for(repo, copy, extra)
            limit = machines.call_limit(deadline)
            if not limit:  # the next run of the race repeats it
                return
            reply = machines.call(machine, "cancel", copy, timeout=limit).get(copy)
        except Refusal as error:
            event(race, copy, "cancel failed", reply=str(error))
            continue
        if first:
            event(race, copy, "cancel requested", reply=reply)


# Registration: a new race, a resumed one, or a change of plan

def copy_receipt(repo, submission):
    if not core.SUBMISSION_RE.fullmatch(submission):
        raise Refusal("invalid submission id %r" % submission)
    receipt = machines.receipt_of(repo, submission)
    if receipt is None:
        raise Refusal("no receipt for %s in this checkout" % submission)
    if not receipt.get("machine"):
        raise Refusal("%s ran on AWS; race is for machines" % submission)
    if receipt.get("submission_state") != "submitted":
        raise Refusal("%s was not submitted (its receipt says %s)" % (submission, receipt.get("submission_state")))
    if not receipt.get("template_sha256"):
        raise Refusal("%s's receipt predates races (it has no template_sha256); submit it again" % submission)
    return receipt


def same_job(repo, submissions):
    receipts = [copy_receipt(repo, submission) for submission in submissions]
    for receipt in receipts[1:]:
        for field in ("commit", "template_sha256", "inputs", "no_build"):
            if receipt.get(field) != receipts[0].get(field):
                raise Refusal("%s and %s differ in %s; a race keeps one of several copies of one job" % (
                    receipts[0]["submission"], receipt["submission"], field))


def register(repo, args, deadline=None):
    """The race these copies name, created, resumed or changed under the registration lock. A
    change (new copies, another policy or order) holds the race's own lock too, and is made only
    while every copy still in the race is queued."""
    given = list(dict.fromkeys(args.submissions))
    registry = Lock(races(repo) / "registry.lock", wait=True, deadline=deadline)
    try:
        owners = {copy: race for race in load_races(repo) for copy in race["copies"]}
        named = {owners[copy]["id"]: owners[copy] for copy in given if copy in owners}
        if len(named) > 1:
            raise Refusal("these copies belong to %d races (%s); give one race's" % (len(named), ", ".join(named)))
        if not named:
            if not args.keep:
                raise Refusal("give --keep first-start or --keep first-success for a new race")
            if len(given) < 2:
                raise Refusal("a race needs two or more copies")
            same_job(repo, given)
            race = {"schema": core.SCHEMA, "id": "R" + core.new_submission_id(), "keep": args.keep, "copies": given,
                    "phase": "waiting", "runner": None, "kept": None, "roles": {copy: "waiting" for copy in given},
                    "seen": {copy: {} for copy in given}, "accounting": {}, "events": []}
            event(race, None, "created", keep=args.keep, copies=" ".join(given))
            save(repo, race)
            return race, None
        race = next(iter(named.values()))
        new = [copy for copy in given if copy not in race["copies"]]
        if race["phase"] in ("done", "lost"):
            if new:
                raise Refusal("race %s has ended (%s); its copies stay with it" % (race["id"], race["phase"]))
            return race, None
        if not new and (not args.keep or args.keep == race["keep"]):
            return race, None  # a resume, in the recorded tie order whatever the order given
        control = Lock(races(repo) / (race["id"] + ".lock"), wait=False)
        try:
            race = reload(repo, race)
            found = poll(repo, race["copies"], args.machines, deadline)
            for copy in race["copies"]:  # a copy cancelled while no controller ran is dropped first
                if race["roles"][copy] == "waiting" and found[copy][0]["outcome"] == "never started":
                    race["roles"][copy] = "dropped"
                    event(race, copy, "dropped", because="ended, never started")
            live = [copy for copy in race["copies"] if race["roles"][copy] != "dropped"]
            if not set(live) <= set(given):
                raise Refusal("to change race %s, give every copy still in it (%s) and the new ones, in the new "
                              "order" % (race["id"], " ".join(live)))
            moved = [copy for copy in live if found[copy][0]["started"] is not False]
            if race["phase"] != "waiting" or moved:
                raise Refusal("race %s can change only while every copy is queued; %s %s" % (
                    race["id"], " ".join(moved) or "it", "has started or cannot be seen" if len(moved) <= 1 else
                    "have started or cannot be seen"))
            if new:  # against the race's first copy, which stays its reference even once dropped
                same_job(repo, race["copies"][:1] + new)
        except BaseException:
            control.release()
            raise
        order = [copy for copy in given if copy in live or copy in new]  # the new tie order, as given
        race["copies"] = order + [copy for copy in race["copies"] if copy not in order]
        race["keep"] = args.keep or race["keep"]
        for copy in new:
            race["roles"][copy], race["seen"][copy] = "waiting", {}
        event(race, None, "changed", keep=race["keep"], copies=" ".join(race["copies"]))
        save(repo, race)
        return race, control
    finally:
        registry.release()


def race_main(args):
    repo, race, control, result, error = None, None, None, None, None
    try:
        repo = project.repository()
        signal.signal(signal.SIGTERM, interrupt)
        deadline = time.monotonic() + 60 * args.timeout if args.timeout else None  # registration included
        race, control = register(repo, args, deadline)
        if race["phase"] not in ("done", "lost") and control is None:
            control = Lock(races(repo) / (race["id"] + ".lock"), wait=False)
            race = reload(repo, race)  # another controller may have moved it on since it was read
        if race["phase"] in ("done", "lost"):
            result = race["phase"]
            save(repo, race, mirrors_only=True)  # repairs mirrors a controller that stopped did not write
        else:
            while True:
                found = poll(repo, race["copies"], args.machines, deadline)
                step(race, found)
                retiring = cleanup(race, found)
                save(repo, race)  # decisions are recorded before they are acted on
                if retiring:
                    cancel(repo, race, args.machines, retiring, deadline)
                    save(repo, race)
                if race["phase"] in ("done", "lost"):
                    result = race["phase"]
                    break
                if not sleep_until(deadline, max(LEAST_POLL, args.poll)):
                    result = "timeout"
                    break
    except Refusal as refusal:
        result, error = "refused", str(refusal)
        print("cloudsweep: %s" % refusal, file=sys.stderr)
    except KeyboardInterrupt:
        result = "interrupted"
        print("cloudsweep: interrupted; run the same race command again to resume", file=sys.stderr)
    finally:
        if control:
            control.release()
    if race and result in ("done", "lost", "timeout", "interrupted"):
        print("race %s: %s%s" % (race["id"], result, ", kept %s" % race["kept"] if race.get("kept") else ""),
              file=sys.stderr)
    output(args, {"race": race and race["id"], "result": result, "kept": race and race.get("kept") if result == "done"
                  else None, "error": error})
    return EXIT[result]


def status_line(repo, submission):
    """The line `status` shows under a copy: its race, from the race file."""
    receipt = machines.receipt_of(repo, submission) or {}
    mirror = receipt.get("race")
    if not isinstance(mirror, dict):
        return None, None
    try:
        race = json.loads((races(repo) / (mirror["id"] + ".json")).read_text())
    except (OSError, ValueError, KeyError):
        return None, None
    others = ["%s %s" % (copy, race["roles"][copy]) for copy in race["copies"] if copy != submission]
    text = "race %s (%s, %s): %s; %s" % (race["id"], race["keep"], race["phase"], race["roles"][submission],
                                         ", ".join(others))
    return text, {"id": race["id"], "keep": race["keep"], "phase": race["phase"], "role": race["roles"][submission],
                  "events": [entry for entry in race["events"] if entry.get("copy") == submission]}


def add_arguments(commands):
    waiting = commands.add_parser("wait", help="wait until a submission runs or ends (machines)",
                                  description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    waiting.add_argument("submission")
    waiting.add_argument("--until", choices=("running", "done"), default="running",
                         help="running (the default): until it has started or ended; done: until it has ended")
    waiting.add_argument("--timeout", type=float, metavar="MINUTES", help="give up after this long (default: never)")
    waiting.add_argument("--poll", type=int, default=POLL, metavar="SECONDS",
                         help="seconds between polls (default %d, at least %d)" % (POLL, LEAST_POLL))
    waiting.add_argument("--on", metavar="MACHINE", help="the machine (default: the one its receipt names)")
    waiting.add_argument("--json", action="store_true", help="print one JSON object at the end")
    machines.add_file_argument(waiting)
    waiting.set_defaults(handler=wait_main)
    racing = commands.add_parser("race", help="keep one of several copies of a job (first-start or first-success)",
                                 description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    racing.add_argument("submissions", nargs="+", metavar="SUBMISSION", help="the copies, in tie order")
    racing.add_argument("--keep", choices=POLICIES,
                        help="first-start: keep the first copy to start; first-success: run one copy at a time "
                             "until one succeeds (required for a new race)")
    racing.add_argument("--timeout", type=float, metavar="MINUTES", help="stop after this long (default: never); "
                        "the race is recorded, and the same command resumes it")
    racing.add_argument("--poll", type=int, default=POLL, metavar="SECONDS",
                        help="seconds between polls (default %d, at least %d)" % (POLL, LEAST_POLL))
    racing.add_argument("--json", action="store_true", help="print one JSON object at the end: race, result, kept")
    machines.add_file_argument(racing)
    racing.set_defaults(handler=race_main)
