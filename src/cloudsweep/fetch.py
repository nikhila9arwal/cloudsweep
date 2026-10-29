"""cloudsweep fetch: download a submission's attempts, verify them and lay them out locally.

Writes DEST/SUBMISSION/ with the submission manifest, its commands, and every attempt as
CHILD/ATTEMPT/ (outputs under out/, stdout.txt, stderr.txt, started.json, manifest.json).
Each file named by an attempt's manifest is checked against its sha256 and size. An
attempt without manifest.json is incomplete evidence even if Batch reports the job ended;
it is downloaded as found and reported. Existing local files are never overwritten with
different content. Exits 0 only when every child has a verified succeeded attempt.
"""

import json
import os
import tempfile
import time
from pathlib import Path

from . import core, project, settings

FINISHED = ("SUCCEEDED", "FAILED")


def no_symlinks(target):
    # Remote output and existing local symlinks must never redirect writes.
    for path in (target, *target.parents):
        core.require(not path.is_symlink(), "refusing symbolic link in fetch destination: " + str(path))


def place(store, key, target, expected=None):
    """Download key to target, never replacing different local content; 'ok' or 'mismatch'."""
    no_symlinks(target)
    if (expected and target.is_file() and target.stat().st_size == expected["size"]
            and core.sha256_file(target) == expected["sha256"]):
        return "ok"
    target.parent.mkdir(parents=True, exist_ok=True)
    handle, name = tempfile.mkstemp(dir=str(target.parent), prefix=".fetch-")
    os.close(handle)
    temporary = Path(name)
    try:
        store.get_file(key, temporary)
        digest = core.sha256_file(temporary)
        if target.exists():
            if core.sha256_file(target) != digest:
                raise ValueError("%s exists with different content; not overwritten" % target)
        else:
            temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    if expected and (digest != expected["sha256"] or target.stat().st_size != expected["size"]):
        return "mismatch"
    return "ok"


def fetch(store, submission, destination):
    core.require(core.SUBMISSION_RE.fullmatch(submission), "invalid submission id")
    manifest_data = store.get_bytes("submissions/%s/submission.json" % submission)
    if manifest_data is None:
        raise ValueError("no submission %s in the bucket" % submission)
    manifest = core.validate_submission(json.loads(manifest_data))
    core.require(manifest["submission"] == submission, "submission manifest identity mismatch")
    attempts = {}
    prefix = "runs/%s/" % submission
    for key, size in store.list(prefix):
        core.require(key.startswith(prefix), "object outside submission prefix")
        parts = key[len(prefix):].split("/", 2)
        core.require(len(parts) == 3, "invalid attempt object: " + key)
        child, attempt, relative = parts
        core.require(child.isascii() and child.isdigit() and str(int(child)) == child
                     and int(child) < manifest["commands"]["count"], "invalid child in " + key)
        core.require(core.ATTEMPT_RE.fullmatch(attempt), "invalid attempt in " + key)
        core.input_name(relative)
        attempts.setdefault(int(child), {}).setdefault(attempt, {})[relative] = key
    root = Path(destination).absolute() / submission
    no_symlinks(root)
    root.mkdir(parents=True, exist_ok=True)
    place(store, "submissions/%s/submission.json" % submission, root / "submission.json")
    place(store, "submissions/%s/commands.jsonl" % submission, root / "commands.jsonl")
    if core.sha256_file(root / "commands.jsonl") != manifest["commands"]["sha256"]:
        raise ValueError("commands.jsonl does not match the submission manifest")
    children = []
    for child in range(manifest["commands"]["count"]):
        found = attempts.get(child, {})
        reports = []
        for attempt in sorted(found, key=core.attempt_order):
            keys = found[attempt]
            local = root / str(child) / attempt
            report = {"attempt": attempt, "problems": [], "status": "incomplete (no completion manifest)"}
            files = {}
            try:
                if "manifest.json" not in keys:
                    raise ValueError(report["status"])
                place(store, keys["manifest.json"], local / "manifest.json")
                final = json.loads((local / "manifest.json").read_bytes())
                core.require(isinstance(final, dict) and final.get("schema") == core.SCHEMA
                             and final.get("submission") == submission and final.get("child") == child
                             and final.get("attempt") == attempt, "attempt manifest identity mismatch")
                core.require(final.get("status") in ("succeeded", "failed", "terminated"), "invalid status")
                core.require(isinstance(final.get("files"), dict), "invalid files manifest")
                for relative, expected in final["files"].items():
                    core.input_name(relative)
                    core.require(relative != "manifest.json" and isinstance(expected, dict), "invalid file record")
                    if "sha256" in expected:
                        core.require(core.SHA_RE.fullmatch(str(expected["sha256"]))
                                     and type(expected.get("size")) is int and expected["size"] >= 0,
                                     "invalid checksum record")
                if final["status"] == "succeeded":
                    binary = final.get("binary_sha256")
                    core.require(final.get("commit") == manifest["commit"]
                                 and final.get("image_digest") == manifest["image_digest"]
                                 and (binary is None if manifest["recipe"]["binary"] is None
                                      else core.SHA_RE.fullmatch(str(binary)))
                                 and type(final.get("exit_code")) is int and final["exit_code"] == 0,
                                 "success manifest provenance mismatch")
                    core.require({"started.json", "stdout.txt", "stderr.txt"} <= set(final["files"]),
                                 "success manifest lacks required evidence")
                files = final["files"]
                report["status"] = final["status"]
                report["error"] = final.get("error")
            except (ValueError, TypeError, UnicodeError) as error:
                if "manifest.json" in keys:
                    report.update(status="invalid completion manifest", problems=[str(error)])
            # One retrieval path keeps evidence for incomplete and invalid attempts too.
            for relative in sorted(set(keys) - {"manifest.json"}):
                expected = files.get(relative)
                checksum = expected if expected and "sha256" in expected else None
                if place(store, keys[relative], local / relative, checksum) == "mismatch":
                    report["problems"].append("%s fails its sha256 or size" % relative)
            for relative, expected in files.items():
                if "sha256" not in expected:
                    report["problems"].append("%s not uploaded (%s)" % (relative, expected.get("skipped")))
                elif relative not in keys:
                    report["problems"].append("%s listed but missing" % relative)
            if files:
                report["problems"].extend("%s present but not in the manifest" % relative
                                          for relative in sorted(set(keys) - set(files) - {"manifest.json"}))
            reports.append(report)
        verified = [r for r in reports if r["status"] == "succeeded" and not r["problems"]]
        verdict = ("succeeded" if verified else "verification failed" if reports and reports[-1]["problems"]
                   else reports[-1]["status"] if reports
                   else "no attempt yet (queued, cancelled or never started)")
        children.append({"child": child, "verdict": verdict, "attempts": reports})
    summary = {"submission": submission, "children": children,
               "complete": all(c["verdict"] == "succeeded" for c in children)}
    no_symlinks(root / "fetch.json")
    (root / "fetch.json").write_bytes(core.pretty(summary))
    return summary


def print_summary(summary, destination):
    for child in summary["children"]:
        notes = "; ".join(p for a in child["attempts"] for p in a["problems"])
        errors = "; ".join(a["error"] for a in child["attempts"] if a.get("error"))
        tries = ", ".join("%s %s" % (a["attempt"], a["status"]) for a in child["attempts"])
        print("child %d: %s%s%s%s" % (child["child"], child["verdict"], " [%s]" % tries if tries else "",
                                      " problems: " + notes if notes else "", " error: " + errors if errors else ""))
    print("%s: %s" % ("complete" if summary["complete"] else "INCOMPLETE",
                      Path(destination).absolute() / summary["submission"]))


def wait_for(aws, values, submission, minutes):
    """Poll the submission's job until Batch reports it finished, at most minutes long."""
    deadline = time.monotonic() + 60 * minutes
    while True:
        found = aws("batch", "list-jobs", "--job-queue", values["QUEUE"], "--filters",
                    json.dumps([{"name": "JOB_NAME", "values": [submission + "-run"]}]))
        jobs = found.get("jobSummaryList", [])
        status = jobs[0]["status"] if len(jobs) == 1 else "UNKNOWN (%d jobs found)" % len(jobs)
        counts = jobs[0].get("arrayProperties", {}).get("statusSummary") if len(jobs) == 1 else None
        print("%s: %s%s" % (submission, status, " %s" % json.dumps(counts, sort_keys=True) if counts else ""),
              flush=True)
        if status in FINISHED or len(jobs) != 1:
            return status
        if time.monotonic() > deadline:
            raise settings.Refusal("still %s after --wait %d minutes" % (status, minutes))
        time.sleep(60)


def main(args):
    if not core.SUBMISSION_RE.fullmatch(args.submission):
        return settings.fail("invalid submission id %r" % args.submission)
    values = settings.load(project.repository())
    aws = settings.aws_for(values)
    if args.wait is not None:
        wait_for(aws, values, args.submission, args.wait)
    summary = fetch(settings.CliStore(aws, values["BUCKET"]), args.submission, args.destination)
    print_summary(summary, args.destination)
    return 0 if summary["complete"] else 1


def add_arguments(commands):
    parser = commands.add_parser("fetch", help="download and verify a submission's results",
                                 description=__doc__, formatter_class=_raw())
    parser.add_argument("submission")
    parser.add_argument("destination", nargs="?", type=Path, default=Path("cloudsweep-results"),
                        help="directory to place SUBMISSION/ in (default ./cloudsweep-results)")
    parser.add_argument("--wait", type=int, nargs="?", const=24 * 60, metavar="MINUTES",
                        help="first wait until the submission finishes (default at most a day)")
    parser.set_defaults(handler=main)


def _raw():
    import argparse
    return argparse.RawDescriptionHelpFormatter

