"""cloudsweep status and cloudsweep cancel.

status reports jobs, desired capacity and reported month-to-date spend. Reported spend
comes from AWS Budgets through the guard's snapshot; billing data arrive hours after usage,
so it lags the work shown above it. It works when cloud use is off or the cutoff is latched.
--storage inventories retained objects and images instead of jobs.

cancel stops one submission's jobs, or all work, then reports remaining capacity. Waiting
jobs are cancelled and starting or running ones terminated, array children included. The
deployment's control function does this and acts only on jobs in its own queue. Cancelling
stays available when cloud use is off or the cutoff is latched.
"""

import argparse
import json
import time
from decimal import Decimal

from . import core, project, settings
from .project import Refusal

STATUSES = ("SUBMITTED", "PENDING", "RUNNABLE", "STARTING", "RUNNING", "SUCCEEDED", "FAILED")


def collect(aws, values, wanted):
    found = {}
    for status in STATUSES:
        listed = aws("batch", "list-jobs", "--job-queue", values["QUEUE"], "--job-status", status)
        for job in listed.get("jobSummaryList", []):
            submission = settings.submission_of(job["jobName"])
            if submission and (not wanted or submission in wanted):
                row = {"job": job["jobId"], "status": job["status"], "reason": job.get("statusReason", "")}
                if "statusSummary" in job.get("arrayProperties", {}):
                    row["children"] = job["arrayProperties"]["statusSummary"]
                found.setdefault(submission, {})[job["jobId"]] = row
    return {submission: list(jobs.values()) for submission, jobs in sorted(found.items())}


def storage_report(aws, values):
    """Explicit inventory: routine status reads only the small guard snapshot from S3."""
    sizes = {}
    for key, size in settings.CliStore(aws, values["BUCKET"]).list(""):
        prefix = key.split("/", 1)[0]
        sizes[prefix] = sizes.get(prefix, 0) + size
    images = aws("ecr", "describe-images", "--repository-name", values["REPOSITORY"].rsplit("/", 1)[1])
    image_bytes = sum(item.get("imageSizeInBytes", 0) for item in images.get("imageDetails", []))
    return {"s3_bytes_by_prefix": sizes, "ecr_image_bytes_sum": image_bytes,
            "excludes": "noncurrent S3 object versions, log storage; summed image sizes may count shared layers twice"}


def certificate_line(certificate):
    if certificate["expires"] is None:
        state = "agent certificate: missing or unreadable in %s" % certificate["directory"]
    else:
        state = "agent certificate %s %s (%d days)" % ("expired" if certificate["days_left"] < 0 else "expires",
                                                       certificate["expires"], certificate["days_left"])
    if certificate["renew"]:
        state = "WARNING: %s; an administrator renews it with: %s" % (state, settings.RENEW_COMMAND)
    return state


def status(args):
    repo = project.repository()
    values = settings.load(repo)
    certificate = settings.certificate_state(values["PREFIX"]) if values["IDENTITY"] == "roles-anywhere" else None
    aws = settings.aws_for(values)
    try:
        if args.storage:
            storage = storage_report(aws, values)
            if args.json:
                print(json.dumps(storage, indent=2, sort_keys=True))
            else:
                print("retained storage: S3 %s; summed image sizes %d bytes" % (
                    json.dumps(storage["s3_bytes_by_prefix"], sort_keys=True), storage["ecr_image_bytes_sum"]))
                print("excludes: " + storage["excludes"])
            return 0
        try:
            latch = "clear" if settings.read_latch(aws, values) else "?"
        except Refusal as refusal:
            latch = str(refusal)
        submissions = collect(aws, values, set(args.submissions))
        ce = aws("batch", "describe-compute-environments", "--compute-environments",
                 values["COMPUTE_ENVIRONMENT"])["computeEnvironments"][0]
        spend = settings.CliStore(aws, values["BUCKET"]).get_bytes("guard/spend.json")
    except (Refusal, settings.AwsError, ValueError):
        if certificate and certificate["renew"]:  # an expired certificate is the likely cause
            print(certificate_line(certificate))
        raise
    resources = ce.get("computeResources", {})
    per_vcpu = core.usd_per_vcpu_hour(values["INSTANCE_TYPES"])
    largest = core.largest(values["INSTANCE_TYPES"])[1]["vcpus"]

    def usd(vcpus):
        return str((vcpus * per_vcpu).quantize(Decimal("0.01")))
    capacity = {"desired_vcpus": resources.get("desiredvCpus", 0), "max_vcpus": resources.get("maxvCpus"),
                "state": ce.get("state"), "status": ce.get("status"),
                "desired_capacity_usd_per_hour": usd(resources.get("desiredvCpus", 0)),
                "usd_per_hour_at_cap": usd(resources.get("maxvCpus", 0) + largest),
                "note": "desired capacity is not EC2 inventory; Batch may exceed the cap by one instance "
                        "(up to %d vCPUs)" % largest}
    report = {"enabled": values[settings.ENABLED], "cutoff": latch, "submissions": submissions,
              "capacity": capacity, "reported_spend": json.loads(spend) if spend else None,
              "agent_certificate": certificate}
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0
    print("cloud use %s; cutoff: %s" % ("on" if values[settings.ENABLED] == "1" else "OFF", latch))
    for submission, rows in submissions.items():
        for row in rows:
            extra = " children %s" % json.dumps(row["children"], sort_keys=True) if "children" in row else ""
            print("  %s %s%s %s" % (submission, row["status"], extra, row["reason"]))
    if not submissions:
        print("  no jobs%s in the queue's history" % (" for those submissions" if args.submissions else ""))
    print("desired capacity: %s of %s vCPUs (%s/%s); about $%s/h now, at most $%s/h at the cap" % (
        capacity["desired_vcpus"], capacity["max_vcpus"], capacity["state"], capacity["status"],
        capacity["desired_capacity_usd_per_hour"], capacity["usd_per_hour_at_cap"]))
    if spend:
        snapshot = report["reported_spend"]
        print("reported month-to-date spend (AWS Budgets, hours behind; read %s): $%s of the $%s cutoff, "
              "forecast $%s" % (snapshot.get("read_at"), snapshot.get("actual_usd", "?"),
                                snapshot.get("cutoff_usd", "?"), snapshot.get("forecast_usd", "?")))
        tag = snapshot.get("cost_tag")
        if tag and tag != "active":
            print("WARNING: the Project cost-allocation tag is %s; until it is active the budget cutoff sees "
                  "no spend" % tag)
    else:
        print("reported spend: no guard snapshot yet (the guard runs every 5 minutes)")
    if certificate:
        print(certificate_line(certificate))
    return 0


def cancel(args):
    if args.submission and not core.SUBMISSION_RE.fullmatch(args.submission):
        raise Refusal("invalid submission id %r" % args.submission)
    values = settings.load(project.repository())
    aws = settings.aws_for(values)
    payload = {"all": True} if args.all else {"submission": args.submission}
    result = settings.invoke_lambda(aws, values["CONTROL_FUNCTION"], payload)
    stopped = result["stopped"]
    print("stop requests: cancelled %d waiting and terminated %d starting/running job(s)" % (
        len(stopped["cancelled"]), len(stopped["terminated"])))
    for problem in result.get("errors", []):
        print("  incomplete: " + problem)
    deadline = time.monotonic() + 60 * args.wait
    capacity = result.get("capacity") or {}
    while True:
        print("capacity: %s desired vCPUs, %s registered instances (%s)" % (
            capacity.get("desired_vcpus"), capacity.get("instances"), capacity.get("status")), flush=True)
        drained = capacity.get("desired_vcpus") == 0 and capacity.get("instances") == 0
        if drained or args.wait == 0:
            break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise Refusal("capacity did not reach zero within --wait; check `cloudsweep status` or cancel again")
        time.sleep(min(30, remaining))
        checked = settings.invoke_lambda(aws, values["CONTROL_FUNCTION"], {"status": True})
        if checked.get("errors"):
            raise ValueError("capacity check failed: " + "; ".join(checked["errors"]))
        capacity = checked.get("capacity") or {}
    return 1 if result.get("errors") else 0


def add_arguments(commands):
    parser = commands.add_parser("status", help="jobs, capacity and reported spend",
                                 description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("submissions", nargs="*", help="limit to these submissions")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--storage", action="store_true", help="inventory retained storage instead of jobs")
    parser.set_defaults(handler=status)

    stop = commands.add_parser("cancel", help="stop a submission, or all work",
                               description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    target = stop.add_mutually_exclusive_group(required=True)
    target.add_argument("submission", nargs="?")
    target.add_argument("--all", action="store_true", help="all work in the queue")
    stop.add_argument("--wait", type=int, default=0, metavar="MINUTES",
                      help="poll until capacity is zero, at most this long")
    stop.set_defaults(handler=cancel)
