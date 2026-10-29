"""cloudsweep image: build, check and publish the base image.

  cloudsweep image build      build [image] dockerfile, add the worker, run the toolchain
                              check and, when [acceptance] is configured, the acceptance run
  cloudsweep image publish    push the accepted local base image to the deployment's registry

The base image changes only when the toolchain changes; each commit you submit is built
FROM the newest published base. A run of the acceptance command checks that a new base
still produces the known result before anything runs on it.
"""

import argparse
import json
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from . import core, engine, project, settings, submit
from .project import Refusal


def candidate(name):
    return "localhost/%s-base:candidate" % name


def acceptance_path(repo, image_id):
    return settings.state_dir(repo) / "acceptance" / (image_id.split(":", 1)[1] + ".json")


def checks(config, image):
    """The worker's own check and the project's toolchain check, inside image."""
    if engine.run_local(image, ["check"], stdout=sys.stderr):
        raise Refusal("the image cannot host the worker (it needs python3 >= 3.8 and git); see above")
    if config["check"]:
        print("toolchain check: %s" % config["check"], file=sys.stderr, flush=True)
        if engine.run_shell(image, config["check"], stdout=sys.stderr):
            raise Refusal("the toolchain check failed: %s" % config["check"])


def accept(repo, name, image, image_id, revision, keep=None):
    """Run the [acceptance] command through the worker on the new base and its host check."""
    config = project.load(repo)
    spec = project.acceptance(config)
    commit = settings.clean_commit(repo, revision)
    recipe = project.recipe(project.load(repo, commit))
    files = submit.input_files(spec["inputs"], [repo.resolve()])
    try:
        commands = [core.check_command(spec["command"], set(files), recipe["binary"] is not None)]
    except ValueError as error:
        raise Refusal("[acceptance] command: %s" % error)
    root = Path(keep) if keep else Path(tempfile.mkdtemp(prefix="cloudsweep-acceptance-"))
    values = _settings(repo)
    summary = submit.trial(repo, commit, recipe, image, image_id, files, commands, root, spec["hours"],
                           submit.platform_of(values), label="acceptance")
    out = root / "results" / summary["submission"] / "0" / "1" / "out"
    check = spec["check"].replace("{out}", _quote(str(out)))
    passed = summary["complete"] and subprocess.run(["/bin/sh", "-c", check], cwd=str(repo)).returncode == 0
    result = {"image_id": image_id, "commit": commit, "submission": summary["submission"], "passed": passed,
              "check": spec["check"], "evidence": str(root), "at": core.now_iso()}
    path = acceptance_path(repo, image_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(core.pretty(result))
    print(json.dumps(result, indent=2, sort_keys=True))
    if not passed:
        raise Refusal("acceptance failed%s; evidence in %s" % (
            "" if summary["complete"] else " (the run itself did not succeed)", root))
    return result


def _quote(text):
    import shlex
    return shlex.quote(text)


def _settings(repo):
    try:
        return settings.load(repo)
    except Refusal:
        return None


def build(args):
    repo = project.repository(args.repo)
    config = project.load(repo)
    name = project.name(config)
    image_config = project.image(config, repo)
    values = _settings(repo)
    if not image_config["platform"]:
        image_config["platform"] = submit.platform_of(values)
    image_id = engine.build_base(image_config, name, candidate(name))
    print("built %s (%s)" % (candidate(name), image_id), flush=True)
    checks(image_config, candidate(name))
    if project.acceptance(config) and not args.skip_acceptance:
        accept(repo, name, candidate(name), image_id, args.revision, args.keep)
        print("acceptance passed")
    print("next: cloudsweep image publish")
    return 0


def publish(args):
    repo = project.repository(args.repo)
    config = project.load(repo)
    name = project.name(config)
    image = candidate(name)
    image_id = engine.image_id(image)
    if project.acceptance(config):
        path = acceptance_path(repo, image_id)
        result = json.loads(path.read_text()) if path.is_file() else {}
        if result.get("image_id") != image_id or result.get("passed") is not True:
            raise Refusal("no passing acceptance result for %s (%s); run `cloudsweep image build`" % (image, image_id))
    checks(project.image(config, repo), image)
    values, aws = settings.admit(repo)
    repository = values["REPOSITORY"]
    engine.login(aws, repository)
    tag = "%s%s-%s" % (engine.BASE_TAG, datetime.now(timezone.utc).strftime("%Y%m%d"), image_id[7:19])
    digest = engine.push(image, "%s:%s" % (repository, tag))
    print("published base %s:%s (%s); submissions now build FROM it" % (repository, tag, digest))
    return 0


def add_arguments(commands):
    parser = commands.add_parser("image", help="build and publish the base image (your toolchain + worker)",
                                 description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    actions = parser.add_subparsers(dest="image_action", required=True)
    up = actions.add_parser("build", help="build the base image locally and check it")
    up.add_argument("--skip-acceptance", action="store_true", help="build and check only")
    up.add_argument("--revision", default="HEAD", help="commit the acceptance run builds (default HEAD)")
    up.add_argument("--keep", help="new directory for the acceptance run's store and results")
    up.add_argument("--repo", type=Path)
    up.set_defaults(handler=build)
    out = actions.add_parser("publish", help="push the checked local base image to the registry")
    out.add_argument("--repo", type=Path)
    out.set_defaults(handler=publish)
