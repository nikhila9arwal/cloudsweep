"""retire --delete-data, against a fake AWS CLI: what is deleted, in what order, and that a rerun is safe."""

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

from helpers import make_repo
from test_cli import INI, Cwd, deployment

from cloudsweep import admin, settings


class FakeAdminAws:
    """The stack is already deleted; the bucket holds versions and delete markers in pages."""

    def __init__(self, versions=2500, markers=3):
        self.versions = [{"Key": "runs/%d" % i, "VersionId": "v%d" % i} for i in range(versions)]
        self.markers = [{"Key": "inputs/%d" % i, "VersionId": "m%d" % i} for i in range(markers)]
        self.bucket, self.repository, self.parameter = True, True, True
        self.calls = []

    def __call__(self, *args, parse=True, region=None):
        self.calls.append(args[:2])
        if args[:2] == ("cloudformation", "describe-stacks"):
            raise settings.AwsError(list(args), "Stack with id demo-cloud does not exist")
        if args[:2] == ("batch", "describe-job-definitions"):
            return {"jobDefinitions": [{"jobDefinitionArn": "arn:demo-run:3", "revision": 3}]}
        if args[:2] == ("batch", "deregister-job-definition"):
            return {}
        if args[:2] == ("s3api", "list-object-versions"):
            if not self.bucket:
                raise settings.AwsError(list(args), "An error occurred (NoSuchBucket) when calling ...")
            return {"Versions": self.versions[:1000], "DeleteMarkers": self.markers[:1000]}
        if args[:2] == ("s3api", "delete-objects"):
            payload = json.loads(Path(args[args.index("--delete") + 1][len("file://"):]).read_text())
            assert len(payload["Objects"]) <= 1000
            gone = {(o["Key"], o["VersionId"]) for o in payload["Objects"]}
            self.versions = [v for v in self.versions if (v["Key"], v["VersionId"]) not in gone]
            self.markers = [m for m in self.markers if (m["Key"], m["VersionId"]) not in gone]
            return {}
        if args[:2] == ("s3api", "delete-bucket"):
            assert not self.versions and not self.markers, "bucket not empty"
            self.bucket = False
            return {}
        if args[:2] == ("ecr", "delete-repository"):
            if not self.repository:
                raise settings.AwsError(list(args), "An error occurred (RepositoryNotFoundException) ...")
            self.repository = False
            return {}
        if args[:2] == ("ssm", "delete-parameter"):
            if not self.parameter:
                raise settings.AwsError(list(args), "An error occurred (ParameterNotFound) ...")
            self.parameter = False
            return {}
        raise AssertionError("unexpected AWS call %s" % (args,))


CONFIG = """[profile admin]
region = us-east-1
# keep this comment

[profile demo-agent]
role_arn = arn:aws:iam::1:role/demo-agent
source_profile = admin
region = us-east-1

[profile other]
region = eu-west-1
"""


class RetireDeleteDataTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, str(self.root))
        self.repo = self.root / "p"
        make_repo(self.repo, {"cloudsweep.ini": INI})
        self.env = self.repo / ".cloudsweep/deploy.env"
        deployment(self.env)
        self.config = self.root / "aws-config"
        self.config.write_text(CONFIG)
        self.identity = self.root / "identity"
        self.identity.mkdir()
        for name in ("ca.pem", "agent.pem", "agent.key"):
            (self.identity / name).write_text(name)
        patcher = mock.patch.dict(os.environ, {"AWS_CONFIG_FILE": str(self.config),
                                               "CLOUDSWEEP_IDENTITY_DIR": str(self.identity)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.aws = FakeAdminAws()

    def retire(self, delete_data=True):
        out = io.StringIO()
        with Cwd(self.repo), redirect_stdout(out), mock.patch.object(admin, "admin_aws", return_value=self.aws):
            code = admin.retire(SimpleNamespace(yes=True, delete_data=delete_data, admin_profile="admin"))
        return code, out.getvalue()

    def test_deletes_every_version_then_the_bucket_repository_parameter_profile_and_settings(self):
        code, out = self.retire()
        self.assertEqual(code, 0)
        self.assertIn("already deleted", out)
        self.assertFalse(self.aws.bucket or self.aws.repository or self.aws.parameter)
        self.assertIn(("batch", "deregister-job-definition"), self.aws.calls)
        self.assertIn("deleted 2503 object versions", out)
        self.assertEqual(self.config.read_text(), CONFIG.replace(
            "[profile demo-agent]\nrole_arn = arn:aws:iam::1:role/demo-agent\nsource_profile = admin\n"
            "region = us-east-1\n\n", ""))
        self.assertFalse(self.identity.exists())
        self.assertFalse(self.env.exists())

    def test_a_rerun_after_a_partial_delete_finishes_the_job(self):
        self.aws.bucket, self.aws.versions, self.aws.markers = False, [], []
        self.aws.parameter = False
        code, out = self.retire()
        self.assertEqual(code, 0)
        self.assertFalse(self.aws.repository)
        self.assertFalse(self.env.exists())
        self.assertNotIn(("s3api", "delete-bucket"), self.aws.calls)

    def test_without_the_flag_a_deleted_stack_is_still_a_refusal(self):
        with self.assertRaises(admin.Refusal) as caught:
            self.retire(delete_data=False)
        self.assertIn("no deployed stack", str(caught.exception))
        self.assertTrue(self.env.exists() and self.aws.bucket)

    def test_an_object_that_cannot_be_deleted_stops_before_anything_else(self):
        original = self.aws.__call__

        def failing(*args, **kwargs):
            if args[:2] == ("s3api", "delete-objects"):
                return {"Errors": [{"Key": "runs/0", "Code": "AccessDenied"}]}
            return original(*args, **kwargs)
        self.aws = mock.Mock(side_effect=failing)
        with self.assertRaises(admin.Refusal) as caught:
            self.retire()
        self.assertIn("rerun retire --delete-data", str(caught.exception))
        self.assertTrue(self.env.exists())
        self.assertIn("demo-agent", self.config.read_text())


class RemoveProfileTests(unittest.TestCase):
    def test_absent_profile_or_file_changes_nothing(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, str(root))
        config = root / "config"
        with mock.patch.dict(os.environ, {"AWS_CONFIG_FILE": str(config)}):
            self.assertIsNone(admin.remove_profile("demo-agent"))
            config.write_text("[profile other]\nregion = x\n")
            self.assertIsNone(admin.remove_profile("demo-agent"))
            self.assertEqual(config.read_text(), "[profile other]\nregion = x\n")


if __name__ == "__main__":
    unittest.main()
