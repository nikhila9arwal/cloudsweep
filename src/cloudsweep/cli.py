"""cloudsweep: run parameter sweeps on AWS Batch from committed source.

Start with the README quick start. For your own project, run cloudsweep init.
Use cloudsweep COMMAND --help for options and cloudsweep doctor to diagnose failures.
AWS resources cost money; see docs/admin.md for spending limits and cleanup.
"""

import argparse
import json
import platform
import shutil
import subprocess
import sys

from . import __version__, admin, engine, fetch, image, project, settings, status, submit
from .project import Refusal


def init(args):
    repo = project.repository()
    path = repo / project.NAME
    if path.exists():
        raise Refusal("%s already exists" % path)
    name = args.name or "".join(c if c.isalnum() else "-" for c in repo.name.lower()).strip("-")[:20]
    if not name or not name[0].isalpha():
        name = "sim-" + name
    name = name[:20].rstrip("-")
    project.name(project.parse("[project]\nname = %s\n" % name, "--name"))
    dockerfile = repo / "cloud" / "Dockerfile"
    path.write_text(project.TEMPLATE.format(name=name, dockerfile=dockerfile.relative_to(repo),
                                            command=args.build or "", binary=args.binary or ""))
    print("wrote %s" % path)
    if not dockerfile.exists():
        dockerfile.parent.mkdir(exist_ok=True)
        dockerfile.write_text(project.DOCKERFILE)
        print("wrote %s (edit it to install your toolchain)" % dockerfile)
    ignore = project.main_checkout(repo) / ".gitignore"
    lines = ignore.read_text().splitlines() if ignore.exists() else []
    if ".cloudsweep/" not in [line.strip() for line in lines]:
        with ignore.open("a") as stream:
            stream.write(("\n" if lines and lines[-1] else "") + "# cloudsweep: local deployment settings, receipts\n"
                         ".cloudsweep/\ncloudsweep-results/\n")
        print("added .cloudsweep/ and cloudsweep-results/ to %s" % ignore)
    print("next: edit [build] in %s and the Dockerfile, commit both, then `cloudsweep doctor`" % path.name)
    return 0


def doctor(args):
    """Check each prerequisite and say what to do about the first ones missing."""
    rows = []

    def row(name, ok, detail):
        rows.append((name, ok, detail))
        print("%-22s %s  %s" % (name, "ok " if ok else "-- ", detail), flush=True)
        return ok

    row("python", sys.version_info >= (3, 9), platform.python_version() + " (3.9+ needed here)")
    aws = shutil.which("aws")
    version = subprocess.run(["aws", "--version"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             universal_newlines=True).stdout.strip() if aws else ""
    row("aws cli", version.startswith("aws-cli/2"), version or "missing: install AWS CLI v2")
    try:
        name = engine.engine_name()
        found = subprocess.run([name, "--version"], stdout=subprocess.PIPE, universal_newlines=True).stdout.strip()
        row("container engine", True, found)
    except Refusal as error:
        row("container engine", False, str(error))
    row("git", bool(shutil.which("git")), "required")
    try:
        repo = project.repository()
        row("git repository", True, str(repo))
    except Refusal as error:
        row("git repository", False, str(error))
        return 1
    try:
        config = project.load(repo)
        name = project.name(config)
        recipe = project.recipe(config)
        project.image(config, repo)
        project.acceptance(config)
        row(project.NAME, True, "project %s; build %r; binary %s" % (name, recipe["command"], recipe["binary"]))
        committed = project.tracked(repo, "HEAD", project.NAME)
        row("committed recipe", committed, "in HEAD" if committed else "commit cloudsweep.ini: runs use committed files")
    except Refusal as error:
        row(project.NAME, False, str(error))
    try:
        values = settings.load(repo)
        row("deployment", True, "%s in %s (%s)" % (values["PREFIX"], values["REGION"], values["_path"]))
    except Refusal as error:
        row("deployment", False, str(error))
        return 1
    row("cloud use", values[settings.ENABLED] == "1", "%s=%s" % (settings.ENABLED, values[settings.ENABLED]))
    aws = settings.aws_for(values)
    try:
        row("agent credentials", True, aws("sts", "get-caller-identity")["Arn"])
    except settings.AwsError as error:
        row("agent credentials", False, "%s: %s" % (values["PROFILE"], error.message.splitlines()[-1]))
        return 1
    try:
        settings.read_latch(aws, values)
        row("budget cutoff", True, "clear")
    except Refusal as error:
        row("budget cutoff", False, str(error))
    try:
        digest = engine.newest_base(aws, values["REPOSITORY"])
        row("base image", True, "published " + digest[:19])
    except (Refusal, settings.AwsError) as error:
        row("base image", False, str(error))
    try:
        spend = settings.CliStore(aws, values["BUCKET"]).get_bytes("guard/spend.json")
        snapshot = json.loads(spend) if spend else {}
        tag = snapshot.get("cost_tag", "no guard snapshot yet")
        row("cost tag", tag == "active", "Project tag %s (the budget counts spend only once it is active)" % tag)
    except (settings.AwsError, ValueError) as error:
        row("cost tag", False, str(error))
    return 0 if all(ok for _, ok, _ in rows) else 1


def main(argv=None):
    parser = argparse.ArgumentParser(prog="cloudsweep", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--version", action="version", version="cloudsweep " + __version__)
    commands = parser.add_subparsers(dest="command_name", metavar="COMMAND", required=True)
    start = commands.add_parser("init", help="add cloudsweep.ini and a toolchain Dockerfile to this project")
    start.add_argument("--name", help="project name (default: the directory name)")
    start.add_argument("--build", help="build command, e.g. 'make -j' (default: none)")
    start.add_argument("--binary", help="the program {bin} names, relative to the checkout")
    start.set_defaults(handler=init)
    commands.add_parser("doctor", help="check prerequisites and the deployment").set_defaults(handler=doctor)
    admin.add_arguments(commands)
    image.add_arguments(commands)
    submit.add_arguments(commands)
    status.add_arguments(commands)
    fetch.add_arguments(commands)
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    try:
        return args.handler(args)
    except (Refusal, settings.AwsError, ValueError, OSError, subprocess.CalledProcessError) as error:
        return settings.fail(error)
    except KeyboardInterrupt:
        return settings.fail("interrupted")


if __name__ == "__main__":
    sys.exit(main())
