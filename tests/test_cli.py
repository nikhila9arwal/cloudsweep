"""init, the project recipe, deployment settings, submit's checks and the stack template,
offline: AWS is a stub that answers the few calls each command makes."""

import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from helpers import PACKAGE, TYPES, git, make_repo

from cloudsweep import admin, cli, project, settings, submit

try:
    import yaml
except ImportError:
    yaml = None

INI = """[project]
name = demo
[image]
dockerfile = cloud/Dockerfile
[build]
command = mkdir -p build && cp sim.sh build/sim
binary = build/sim
[run]
hours = 0.5
"""


class Cwd:
    def __init__(self, path):
        self.path, self.old = path, None

    def __enter__(self):
        self.old = os.getcwd()
        os.chdir(str(self.path))

    def __exit__(self, *exc):
        os.chdir(self.old)


class ProjectTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, str(self.root))

    def test_init_writes_a_valid_recipe_dockerfile_and_ignore_rules(self):
        make_repo(self.root / "My_Sim", {"README": "x\n"})
        with Cwd(self.root / "My_Sim"), redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["init", "--build", "make -j", "--binary", "build/sim"]), 0)
            self.assertEqual(cli.main(["init"]), 1)  # never overwrites
        config = project.load(self.root / "My_Sim")
        self.assertEqual(project.name(config), "my-sim")
        self.assertEqual(project.recipe(config), {"command": "make -j", "binary": "build/sim"})
        self.assertTrue((self.root / "My_Sim/cloud/Dockerfile").is_file())
        self.assertIn(".cloudsweep/", (self.root / "My_Sim/.gitignore").read_text())
        self.assertEqual(project.run_defaults(config), {"vcpus": 1, "memory_mib": 3500, "hours": "1"})

    def test_revision_recipe_comes_from_the_commit_not_the_working_tree(self):
        repo = self.root / "p"
        commit = make_repo(repo, {"cloudsweep.ini": INI})
        (repo / "cloudsweep.ini").write_text(INI.replace("build/sim", "other"))
        self.assertEqual(project.recipe(project.load(repo, commit))["binary"], "build/sim")
        self.assertEqual(project.recipe(project.load(repo))["binary"], "other")
        bare = self.root / "q"
        first = make_repo(bare, {"README": "x"})
        with self.assertRaisesRegex(project.Refusal, "has no cloudsweep.ini"):
            project.load(bare, first)

    def test_bad_names_and_acceptance_sections_are_explained(self):
        with self.assertRaisesRegex(project.Refusal, "lower-case"):
            project.name(project.parse("[project]\nname = Bad_Name\n", "t"))
        with self.assertRaisesRegex(project.Refusal, "JSON argv"):
            project.acceptance(project.parse("[acceptance]\ncommand = run it\ncheck = true\n", "t"))
        spec = project.acceptance(project.parse('[acceptance]\ncommand = ["{bin}", "{out}"]\n'
                                                'inputs = a.bin "b c.bin"\ncheck = cmp {out}/x ref/x\n', "t"))
        self.assertEqual(spec["inputs"], ["a.bin", "b c.bin"])


def deployment(path, **changes):
    values = {settings.ENABLED: "1", settings.MAX_RUN: "10", "PREFIX": "demo", "REGION": "us-east-1",
              "PROFILE": "demo-agent", "IDENTITY": "assume-role", "BUCKET": "demo-1-us-east-1",
              "QUEUE": "arn:queue", "COMPUTE_ENVIRONMENT": "arn:ce", "RUN_JOB_DEFINITION": "demo-run",
              "CONTROL_FUNCTION": "demo-control", "STATE_PARAMETER": "/demo/cloud/cutoff",
              "LOG_GROUP": "/aws/batch/demo", "REPOSITORY": "1.dkr.ecr.us-east-1.amazonaws.com/demo-worker",
              "RECEIPTS": str(path.parent / "receipts"), "ARCHITECTURE": "x86_64", "INSTANCE_TYPES": TYPES}
    values.update(changes)
    settings.write(path, values)


class FakeAws:
    def __init__(self, cap=32):
        self.cap, self.calls = cap, []

    def __call__(self, *args, parse=True, region=None):
        self.calls.append(args)
        if args[:2] == ("batch", "describe-compute-environments"):
            return {"computeEnvironments": [{"computeResources": {"maxvCpus": self.cap}}]}
        if args[:2] == ("ecr", "describe-images") and "--filter" in args:
            return {"imageDetails": [{"imageDigest": "sha256:" + "a" * 64, "imageTags": ["base-20261029-x"],
                                      "imagePushedAt": "2026-10-29T10:00:00.000000-04:00"},
                                     {"imageDigest": "sha256:" + "b" * 64, "imageTags": ["base-20261028-y"],
                                      "imagePushedAt": "2026-10-29T13:00:00+00:00"}]}
        if args[:2] == ("ecr", "describe-images"):
            raise settings.AwsError(list(args), "An error occurred (ImageNotFoundException) when ...")
        raise AssertionError("unexpected AWS call %s" % (args,))


class SubmitTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, str(self.root))
        self.repo = self.root / "p"
        self.commit = make_repo(self.repo, {"cloudsweep.ini": INI, "sim.sh": "#!/bin/sh\n", "data/x": "tracked"})
        (self.repo / "trace.bin").write_bytes(b"untracked input")
        deployment(self.repo / ".cloudsweep/deploy.env")
        self.aws = FakeAws()
        patcher = mock.patch.object(settings, "admit", side_effect=lambda repo: (settings.load(repo), self.aws))
        patcher.start()
        self.addCleanup(patcher.stop)

    def submit(self, *argv):
        out = io.StringIO()
        with Cwd(self.repo), redirect_stdout(out), mock.patch("sys.stderr", io.StringIO()) as err:
            code = cli.main(["submit"] + list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_dry_run_plans_the_cheapest_instance_newest_base_and_estimate(self):
        code, out, _ = self.submit("--dry-run", "--input", "trace.bin", "--", "{bin}", "{input:trace.bin}", "{out}")
        self.assertEqual(code, 0)
        plan = json.loads(out)
        self.assertEqual((plan["commit"], plan["instance"], plan["hours"]), (self.commit, "m7a.large", "0.5"))
        self.assertEqual(plan["base_digest"], "sha256:" + "a" * 64)  # 10:00-04:00 is 14:00 UTC, the newer
        self.assertEqual((plan["estimate"]["run_each_usd"], plan["estimate"]["run_worst_usd"]), ("0.06", "0.46"))

    def test_refusals_explain_what_to_do(self):
        cases = [
            (("--dry-run", "--", "{bin}", "{nope}"), "unknown placeholder"),
            (("--dry-run", "--input", "data/x", "--", "{bin}"), "is a file of the revision"),
            (("--dry-run", "--hours", "1000", "--", "{bin}"), "two weeks"),
            (("--dry-run", "--memory-mib", "500000", "--", "{bin}"), "no listed instance fits"),
            (("--dry-run", "--hours", "100", "--", "{bin}"), "above the ceiling"),
        ]
        for argv, message in cases:
            code, _, err = self.submit(*argv)
            self.assertEqual(code, 1, argv)
            self.assertIn(message, err, argv)

    def test_uncommitted_changes_and_the_cap_are_refused(self):
        self.aws.cap = 2
        code, _, err = self.submit("--dry-run", "--vcpus", "4", "--", "{bin}")
        self.assertIn("above the queue's cap", err)
        (self.repo / "sim.sh").write_text("changed\n")
        code, _, err = self.submit("--dry-run", "--", "{bin}")
        self.assertIn("uncommitted changes", err)

    def test_cloud_use_off_refuses_new_work(self):
        deployment(self.repo / ".cloudsweep/deploy.env", **{settings.ENABLED: "0"})
        with self.assertRaisesRegex(project.Refusal, "cloud use is off"):
            REAL_ADMIT(self.repo, read=lambda aws, values: None)

    def test_deployment_architecture_selects_the_image_platform(self):
        self.assertEqual(submit.platform_of(settings.load(self.repo)), "linux/amd64")
        self.assertIsNone(submit.platform_of(None))

    def test_settings_round_trip_and_worktrees_share_them(self):
        values = settings.load(self.repo)
        self.assertEqual(values["INSTANCE_TYPES"], TYPES)
        worktree = self.root / "wt"
        git(self.repo, "worktree", "add", "--quiet", "--detach", str(worktree))
        self.assertEqual(settings.settings_path(worktree), self.repo / ".cloudsweep/deploy.env")


REAL_ADMIT = settings.admit


def template():
    class Loader(yaml.SafeLoader):
        pass

    def intrinsic(loader, suffix, node):
        name = "Ref" if suffix == "Ref" else "Fn::" + suffix
        if isinstance(node, yaml.ScalarNode):
            value = loader.construct_scalar(node)
        elif isinstance(node, yaml.SequenceNode):
            value = loader.construct_sequence(node, deep=True)
        else:
            value = loader.construct_mapping(node, deep=True)
        return {name: value}
    Loader.add_multi_constructor("!", intrinsic)
    return yaml.load((PACKAGE / "stack.yaml").read_text(), Loader=Loader)


@unittest.skipIf(yaml is None, "needs PyYAML")
class StackTests(unittest.TestCase):
    def test_deploy_defaults_equal_the_template_defaults(self):
        parameters = template()["Parameters"]
        for key, value in admin.DEFAULTS.items():
            self.assertEqual(str(parameters[key]["Default"]), value, key)
        self.assertEqual(set(admin.REQUIRED) - set(parameters), set())

    def test_every_output_deploy_reads_exists(self):
        outputs = set(template()["Outputs"])
        for name in ("Bucket", "Queue", "ComputeEnvironment", "RunJobDefinitionName", "ControlFunction",
                     "StateParameter", "JobLogGroup", "Repository", "AgentRoleArn", "SubmitDenyArn",
                     "TrustAnchorArn", "ProfileArn"):
            self.assertIn(name, outputs)

    def test_agent_cannot_register_definitions_pass_roles_or_change_budget(self):
        agent = template()["Resources"]["AgentRole"]["Properties"]["Policies"][0]["PolicyDocument"]["Statement"]
        actions = {action for statement in agent for action in
                   (statement["Action"] if isinstance(statement["Action"], list) else [statement["Action"]])}
        for forbidden in ("batch:RegisterJobDefinition", "iam:PassRole", "budgets:ModifyBudget",
                          "ssm:PutParameter", "batch:TerminateJob", "batch:UpdateComputeEnvironment"):
            self.assertNotIn(forbidden, actions)

    def test_lambda_handlers_exist_in_guard(self):
        from cloudsweep import guard
        resources = template()["Resources"]
        for name in ("GuardFunction", "ControlFunction"):
            handler = resources[name]["Properties"]["Handler"].split(".", 1)[1]
            self.assertTrue(callable(getattr(guard, handler)), handler)


class DeployArgumentTests(unittest.TestCase):
    def args(self, **changes):
        base = dict(prefix="demo", vpc="vpc-1", subnets="subnet-1", email="a@example.org", cutoff=None,
                    identity=None, instance_types=None, max_vcpus=None, disk_gib=None, alerts=None)
        base.update(changes)
        return SimpleNamespace(**base)

    def test_reruns_keep_previous_values_and_alerts_must_rise_below_the_cutoff(self):
        previous = {"MaxvCpus": "64", "AlertEmail": "old@example.org", "Retired": "x"}
        parameters = admin.parameters_from(self.args(email=None), previous, "lambda/guard-x.zip", "absent")
        self.assertEqual((parameters["MaxvCpus"], parameters["AlertEmail"]), ("64", "old@example.org"))
        self.assertNotIn("Retired", parameters)
        with self.assertRaisesRegex(project.Refusal, "increasing"):
            admin.parameters_from(self.args(alerts=["90", "80"]), {}, "k", "absent")
        with self.assertRaisesRegex(project.Refusal, "--email"):
            admin.parameters_from(self.args(email=None), {}, "k", "absent")

    def test_a_latched_cutoff_keeps_the_queue_disabled(self):
        parameters = admin.parameters_from(self.args(), {}, "k", {"cutoff_active": True})
        self.assertEqual(parameters["QueueState"], "DISABLED")

    def test_low_lambda_concurrency_is_refused_before_anything_is_created(self):
        def aws(*args, **kwargs):
            return {"AccountLimit": {"ConcurrentExecutions": 10, "UnreservedConcurrentExecutions": 10}}
        aws.region = "us-east-1"
        with self.assertRaisesRegex(project.Refusal, "Service Quotas"):
            admin.check_lambda_concurrency(aws)

    def test_prices_come_from_the_price_list(self):
        product = {"product": {"attributes": {"licenseModel": "No License required"}},
                   "terms": {"OnDemand": {"x": {"priceDimensions": {"y": {"unit": "Hrs",
                                                                         "pricePerUnit": {"USD": "0.1159200000"}}}}}}}

        def aws(*args, parse=True, region=None):
            self.assertEqual(region, "us-east-1")
            return {"PriceList": [json.dumps(product)]}
        aws.region = "eu-west-1"
        self.assertEqual(str(admin.on_demand_price(aws, "m7a.large")), "0.11592")


if __name__ == "__main__":
    unittest.main()
