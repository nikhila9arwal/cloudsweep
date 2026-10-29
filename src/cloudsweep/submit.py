"""Run one command, or a manifest of commands (one array job), on AWS Batch:

  cloudsweep submit --hours 2 -- {bin} --config configs/a.yaml --out {out}
  cloudsweep submit --hours 2 --commands sweep.jsonl --input data/trace.bin

Each command is an argv list run in a clean checkout of --revision (default HEAD), a
commit: uncommitted changes to tracked files are refused, never sent. A manifest holds one
JSON argv list per line; child i runs line i. Placeholders:
  {bin}         the program named by [build] binary, built from that revision
  {out}         the attempt's output directory; everything written there is uploaded
  {src}         the checkout (also the working directory)
  {index}       the child's index in the manifest (0, 1, ...)
  {input:NAME}  the path of an --input file
Write {{ and }} for literal braces. Each --input PATH is placed at its repository-relative
path inside the checkout, so relative paths work unchanged; PATH=NAME names it explicitly.

Each commit is built once, FROM the newest published base image, and reused after that.
The per-run ceiling (CLOUDSWEEP_MAX_RUN_USD) charges each run the whole cheapest instance
that fits it for its time limit; runs sharing an instance cost less, while a host-failure
retry, startup and idle time add. Runs share instances by default; to give each run a
machine of its own, request all of an instance's vCPUs (--vcpus) or use --isolated.

try runs the same thing on this machine, in the same container, with no AWS calls when a
local base image exists: use it to check a command before paying for it.
"""

import argparse
import json
import subprocess
import sys
import tempfile
from decimal import Decimal
from pathlib import Path

from . import core, engine, fetch, project, settings
from .project import Refusal


def input_files(specs, roots):
    """NAME -> local path. NAME defaults to PATH relative to whichever root contains it."""
    files = {}
    for spec in specs:
        path_text, _, name = spec.partition("=")
        path = Path(path_text).resolve()
        if not path.is_file():
            raise Refusal("input is not a file: %s" % path_text)
        if not name:
            under = [root for root in roots if root == path or root in path.parents]
            if not under:
                raise Refusal("input %s is outside the checkout; give PATH=NAME" % path_text)
            name = path.relative_to(under[0]).as_posix()
        files[core.input_name(name)] = path
    return files


def prepare(args, repo):
    """Commit, recipe, inputs and commands, checked the same way for submit and try."""
    commit = settings.clean_commit(repo, args.revision)
    config = project.load(repo, commit)
    recipe = project.recipe(config)
    defaults = project.run_defaults(config)
    files = input_files(args.input, [repo.resolve(), project.main_checkout(repo).resolve()])
    for name in files:
        if project.tracked(repo, commit, name):
            raise Refusal("input %s is a file of the revision; inputs may not replace tracked files" % name)
    text = args.commands.read_text() if args.commands else json.dumps(args.command)
    try:
        commands = core.parse_commands(text, files, recipe["binary"] is not None)
    except ValueError as error:
        raise Refusal(str(error))
    hours = args.hours or defaults.get("hours")
    if hours is None:
        raise Refusal("give --hours (the time limit per run) or set [run] hours in cloudsweep.ini")
    try:
        core.timeout_seconds(hours)
    except ValueError as error:
        raise Refusal(str(error))
    vcpus = args.vcpus or defaults.get("vcpus") or 1
    memory = args.memory_mib or defaults.get("memory_mib") or core.DEFAULT_MEMORY_MIB
    return commit, recipe, files, commands, Decimal(str(hours)), vcpus, memory


def submit_job(aws, queue, name, definition, submission, hours, size, vcpus, memory):
    args = ["batch", "submit-job", "--job-name", name, "--job-queue", queue,
            "--job-definition", definition, "--parameters", json.dumps({"submission": submission}),
            "--timeout", json.dumps({"attemptDurationSeconds": core.timeout_seconds(hours)}),
            "--container-overrides", json.dumps({"resourceRequirements": [
                {"type": "VCPU", "value": str(vcpus)}, {"type": "MEMORY", "value": str(memory)}]})]
    if size > 1:
        args += ["--array-properties", json.dumps({"size": size})]
    try:
        return aws(*args)["jobId"]
    except settings.AwsError as error:
        # A failed or timed-out call may still have created the job: look before reporting,
        # so a retry cannot silently duplicate the work.
        try:
            found = aws("batch", "list-jobs", "--job-queue", queue, "--filters",
                        json.dumps([{"name": "JOB_NAME", "values": [name]}]))
        except settings.AwsError as lookup_error:
            raise Refusal("submission outcome unknown for %s: %s; lookup failed: %s. Inspect the receipt "
                          "and `cloudsweep status` before trying again; nothing was resubmitted."
                          % (name, error, lookup_error)) from error
        jobs = [job for job in found.get("jobSummaryList", []) if job.get("jobName") == name]
        if len(jobs) == 1:
            return jobs[0]["jobId"]
        raise Refusal("submission outcome unknown for %s: %s; lookup found %d exact matches. Inspect the "
                      "receipt and `cloudsweep status` before trying again; nothing was resubmitted."
                      % (name, error, len(jobs))) from error


def platform_of(values):
    return engine.PLATFORMS.get(values.get("ARCHITECTURE")) if values else None


def main(args):
    repo = project.repository(args.repo)
    path, receipt = None, None
    try:
        values, aws = settings.admit(repo)
        commit, recipe, files, commands, hours, vcpus, memory_mib = prepare(args, repo)
        types = values["INSTANCE_TYPES"]
        if args.isolated:
            spec = core.largest(types)[1]
            vcpus, memory_mib = spec["vcpus"], core.schedulable_mib(spec)
        try:
            cost = core.estimate(types, len(commands), vcpus, memory_mib, hours)
        except ValueError as error:
            raise Refusal(str(error))
        instance = cost["instance"]
        cap = aws("batch", "describe-compute-environments", "--compute-environments",
                  values["COMPUTE_ENVIRONMENT"])["computeEnvironments"][0]["computeResources"]["maxvCpus"]
        if types[instance]["vcpus"] > cap:
            # Batch would leave the job waiting at the head of the queue, blocking later work.
            raise Refusal("these commands need an %s (%d vCPUs), above the queue's cap of %d vCPUs; request "
                          "less, or an administrator raises it (`cloudsweep deploy --max-vcpus`)"
                          % (instance, types[instance]["vcpus"], cap))
        ceiling = settings.max_run_usd(values)
        if core.worst_cost(types, vcpus, memory_mib, hours) > ceiling:
            raise Refusal("one run's worst case is $%s (alone on the most expensive instance type that fits it, "
                          "for the whole %s h limit), above the ceiling of $%s per run (%s in %s); request a "
                          "shorter time limit or fewer resources, raise the ceiling, or deploy without the "
                          "larger instance types" % (cost["run_worst_usd"], hours, ceiling, settings.MAX_RUN,
                                                     values["_path"]))
        repository = values["REPOSITORY"]
        base = engine.newest_base(aws, repository)
        tag = engine.revision_tag(commit, base, recipe)
        digest = engine.registry_digest(aws, repository, tag)
        submission = core.new_submission_id()
        plan = {"submission": submission, "commit": commit, "children": len(commands), "instance": instance,
                "vcpus": vcpus, "memory_mib": memory_mib, "hours": str(hours), "base_digest": base,
                "image": "%s:%s" % (repository, tag), "image_digest": digest or "built and pushed first",
                "estimate": cost}
        if args.dry_run:
            print(json.dumps(dict(plan, dry_run=True), indent=2, sort_keys=True))
            return 0
        if digest is None:
            print("building the image for %s (once per commit)" % commit[:12], file=sys.stderr, flush=True)
            engine.login(aws, repository)
            target = "%s:%s" % (repository, tag)
            local = engine.build_revision(repo, commit, "%s@%s" % (repository, base), target, recipe,
                                          platform_of(values))
            digest = engine.push(local, target)
            engine.engine("rmi", target, stdout=subprocess.DEVNULL, check=False)  # the registry keeps it
        definition = engine.run_definition(aws, values, digest)
        store = settings.CliStore(aws, values["BUCKET"])
        inputs = settings.stage_inputs(store, files)
        manifest = core.submission_manifest(submission, commit, digest, base, recipe, inputs, commands,
                                            vcpus, memory_mib, hours, args.label)
        prefix = "submissions/%s/" % submission
        store.put_bytes(core.commands_bytes(commands), prefix + "commands.jsonl")
        store.put_bytes(core.pretty(manifest), prefix + "submission.json")
        receipt = dict(plan, schema=core.SCHEMA, created=manifest["created"], image_digest=digest, label=args.label,
                       queue=values["QUEUE"], job_definition=definition, inputs=inputs,
                       manifest_sha256=core.sha256_bytes(core.pretty(manifest)),
                       job_name=submission + "-run", job=None, submission_state="prepared")
        path = settings.write_receipt(values["RECEIPTS"], receipt)
        # Building, pushing and uploading can be long. Check both controls again before the
        # non-idempotent call, and record the intended name before sending it.
        current, _ = settings.admit(repo)
        if current != values:
            raise Refusal("the deployment settings changed during submission; inspect the receipt")
        receipt.update(submission_state="submitting")
        settings.write_receipt(path.parent, receipt, replace=True)
        receipt["job"] = submit_job(aws, values["QUEUE"], receipt["job_name"], definition, submission,
                                    hours, len(commands), vcpus, memory_mib)
        receipt.update(submission_state="submitted")
        settings.write_receipt(path.parent, receipt, replace=True)
    except (Refusal, settings.AwsError, ValueError, OSError, subprocess.CalledProcessError) as error:
        if path is not None:
            receipt["error"] = str(error)
            try:
                settings.write_receipt(path.parent, receipt, replace=True)
            except (ValueError, OSError) as write_error:
                print("could not update receipt: %s" % write_error, file=sys.stderr)
            print("receipt: %s; inspect the recorded job before trying again" % path, file=sys.stderr)
        return settings.fail(error)
    print("submitted %s: %d command(s) at %s on %s; job %s" % (
        submission, len(commands), commit[:12], instance, receipt["job"]))
    print("estimate: $%s ($%s per run: a whole %s at $%s/h for the time limit); worst case $%s; excludes %s" % (
        cost["total_estimate_usd"], cost["run_each_usd"], instance, cost["instance_usd_per_hour"],
        cost["total_worst_usd"], cost["excludes"]))
    print("receipt: %s" % path)
    print("next: cloudsweep status %s    |    cloudsweep fetch %s --wait" % (submission, submission))
    return 0


def local_base(name, values, aws=None):
    """The base image for a trial: the local candidate if built, else the newest published one."""
    candidate = "localhost/%s-base:candidate" % name
    if engine.engine("image", "exists" if engine.engine_name() == "podman" else "inspect", candidate,
                     check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
        return candidate, engine.image_id(candidate)
    if values is None:
        raise Refusal("no local base image: run `cloudsweep image build` first")
    aws = aws or settings.aws_for(values)
    digest = engine.newest_base(aws, values["REPOSITORY"])
    engine.login(aws, values["REPOSITORY"])
    reference = "%s@%s" % (values["REPOSITORY"], digest)
    engine.engine("pull", reference, stdout=sys.stderr)
    return reference, digest


def trial(repo, commit, recipe, base, base_digest, files, commands, root, hours=1, platform=None, label="local"):
    """Build the revision image FROM base and run every command through the worker, as Batch
    would but on this machine, with a directory standing in for the bucket. Returns the
    verified summary; results are under root/results/SUBMISSION/."""
    root.mkdir(parents=True, exist_ok=True)
    image = engine.build_revision(repo, commit, base, "localhost/cloudsweep-trial:%s" % commit[:12], recipe, platform)
    store = core.LocalStore(root / "store")
    submission = core.new_submission_id()
    inputs = settings.stage_inputs(store, files)
    manifest = core.submission_manifest(submission, commit, image, base_digest, recipe, inputs, commands, 1,
                                        core.DEFAULT_MEMORY_MIB, hours, label)
    store.put_bytes(core.commands_bytes(commands), "submissions/%s/commands.jsonl" % submission)
    store.put_bytes(core.pretty(manifest), "submissions/%s/submission.json" % submission)
    (root / "store").chmod(0o777)
    for child in range(len(commands)):
        print("running child %d of %d locally" % (child + 1, len(commands)), file=sys.stderr, flush=True)
        engine.run_local(image, ["run", submission], mounts=[(root / "store", "/store")],
                         environment=[("CLOUDSWEEP_STORE", "file:///store"), ("IMAGE_DIGEST", image),
                                      ("AWS_BATCH_JOB_ATTEMPT", "1"), ("AWS_BATCH_JOB_ARRAY_INDEX", str(child)),
                                      ("CLOUDSWEEP_TIME_LIMIT", str(core.timeout_seconds(hours))),
                                      ("AWS_BATCH_JOB_ID", "local")], stdout=sys.stderr)
    return fetch.fetch(store, submission, root / "results")


def try_main(args):
    repo = project.repository(args.repo)
    try:
        values = settings.load(repo)
    except Refusal:
        values = None
    try:
        commit, recipe, files, commands, hours, _, _ = prepare(args, repo)
        name = project.name(project.load(repo, commit))
        base, base_digest = local_base(name, values)
        root = Path(args.keep) if args.keep else Path(tempfile.mkdtemp(prefix="cloudsweep-try-"))
        summary = trial(repo, commit, recipe, base, base_digest, files, commands, root, hours,
                        platform_of(values))
    except (Refusal, settings.AwsError, ValueError, OSError, subprocess.CalledProcessError) as error:
        return settings.fail(error)
    fetch.print_summary(summary, root / "results")
    return 0 if summary["complete"] else 1


def add_common(parser):
    parser.add_argument("--hours", help="time limit per run, in hours (default: [run] hours)")
    parser.add_argument("--commands", type=Path, help="command manifest: one JSON argv list per line")
    parser.add_argument("--input", action="append", default=[], metavar="PATH[=NAME]",
                        help="a file to place at NAME (repository-relative) in the checkout; repeatable")
    parser.add_argument("--revision", default="HEAD", help="committed Git revision to build and run (default HEAD)")
    parser.add_argument("--repo", type=Path, help="the project checkout (default: the current directory)")
    parser.add_argument("command", nargs=argparse.REMAINDER, help="after --: one command's argv")


def check_command_args(parser, args):
    if args.command[:1] == ["--"]:
        args.command = args.command[1:]
    if bool(args.command) == bool(args.commands):
        parser.error("give either one command after -- or --commands FILE")


def add_arguments(commands):
    parser = commands.add_parser("submit", help="run commands on AWS Batch",
                                 description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    add_common(parser)
    parser.add_argument("--vcpus", type=int, help="vCPUs per command (default: [run] vcpus, else 1)")
    parser.add_argument("--memory-mib", type=int, help="memory per command in MiB (default: [run] memory_mib, "
                        "else %d)" % core.DEFAULT_MEMORY_MIB)
    parser.add_argument("--isolated", action="store_true",
                        help="reserve a whole instance of the largest type per command (timing experiments)")
    parser.add_argument("--label", default="", help="free text recorded in the manifest and receipt")
    parser.add_argument("--dry-run", action="store_true", help="check and estimate; build, upload and submit nothing")
    parser.set_defaults(handler=lambda args: (check_command_args(parser, args), main(args))[1])

    trial_parser = commands.add_parser(
        "try", help="run commands locally in the same container, without AWS",
        description="Build the revision image and run the commands through the worker on this machine, "
                    "as Batch would, then verify the results like fetch. Uses the local base image from "
                    "`cloudsweep image build`, else pulls the newest published one.")
    add_common(trial_parser)
    trial_parser.add_argument("--keep", help="new directory for the local store and results (default: a temporary one)")
    trial_parser.set_defaults(vcpus=None, memory_mib=None,
                              handler=lambda args: (check_command_args(trial_parser, args), try_main(args))[1])


