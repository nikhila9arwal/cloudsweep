"""The deployment's local settings (.cloudsweep/deploy.env), AWS CLI calls, the bucket,
receipts and the cutoff latch.

deploy writes .cloudsweep/deploy.env in the project's main checkout; keep it untracked.
CLOUDSWEEP_ENV overrides its location. Every AWS call goes through the AWS CLI with the
profile named there, never with ambient credentials.
"""

import json
import os
import shlex
import ssl
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

from . import core, project
from .guard import valid_state
from .project import Refusal

ENABLED = "CLOUDSWEEP_ENABLED"
MAX_RUN = "CLOUDSWEEP_MAX_RUN_USD"
SETTINGS = (ENABLED, MAX_RUN, "PREFIX", "REGION", "PROFILE", "IDENTITY", "BUCKET", "QUEUE",
            "COMPUTE_ENVIRONMENT", "RUN_JOB_DEFINITION", "CONTROL_FUNCTION", "STATE_PARAMETER",
            "LOG_GROUP", "REPOSITORY", "RECEIPTS", "ARCHITECTURE", "INSTANCE_TYPES")
AMBIENT = ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_PROFILE",
           "AWS_DEFAULT_PROFILE", "AWS_WEB_IDENTITY_TOKEN_FILE", "AWS_ROLE_ARN")
RENEW_DAYS = 30  # deploy issues a new agent certificate this close to expiry (roles-anywhere identity)
RENEW_COMMAND = "cloudsweep deploy --admin-profile <administrator profile>"


class AwsError(Exception):
    def __init__(self, args, message):
        super().__init__("aws %s: %s" % (" ".join(args[:2]), message))
        self.message = message

    @property
    def code(self):
        text = self.message
        return text.split("(", 1)[1].split(")", 1)[0] if "An error occurred (" in text else ""


def state_dir(repo):
    return project.main_checkout(repo) / ".cloudsweep"


def settings_path(repo):
    return Path(os.environ.get("CLOUDSWEEP_ENV") or state_dir(repo) / "deploy.env")


def read_values(path):
    values = {}
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            key, _, value = line.partition("=")
            words = shlex.split(value) or [""]
            values[key.strip()] = words[0]
    return values


def load(repo):
    path = settings_path(repo)
    if not path.is_file():
        raise Refusal("%s is missing: deploy the stack first (`cloudsweep deploy`), or point "
                      "CLOUDSWEEP_ENV at the deployment's settings" % path)
    values = read_values(path)
    missing = [key for key in SETTINGS if key not in values]
    if missing:
        raise Refusal("%s lacks %s: rerun `cloudsweep deploy`" % (path, ", ".join(missing)))
    try:
        values["INSTANCE_TYPES"] = core.check_instance_types(json.loads(values["INSTANCE_TYPES"]))
    except ValueError as error:
        raise Refusal("%s: INSTANCE_TYPES is invalid (%s); rerun `cloudsweep deploy`" % (path, error))
    values["_path"] = str(path)
    return values


def write(path, values):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = ("# Written by `cloudsweep deploy`; keep untracked. You may edit %s (1 = new cloud work\n"
            "# allowed, 0 = refused) and %s (the most one run may be estimated to cost).\n" % (ENABLED, MAX_RUN))
    keys = SETTINGS + tuple(key for key in values if key not in SETTINGS and not key.startswith("_"))
    for key in keys:
        value = values[key]
        if key == "INSTANCE_TYPES" and not isinstance(value, str):
            value = json.dumps(value, sort_keys=True, separators=(",", ":"))
        text += "%s=%s\n" % (key, shlex.quote(str(value)))
    handle, temporary = tempfile.mkstemp(dir=str(path.parent), prefix=".deploy.env.")
    with os.fdopen(handle, "w") as stream:
        stream.write(text)
    os.chmod(temporary, 0o600)
    os.replace(temporary, str(path))


class Aws:
    def __init__(self, profile, region):
        self.profile, self.region = profile, region

    def __call__(self, *args, parse=True, region=None):
        env = {key: value for key, value in os.environ.items()
               if key not in AMBIENT and not key.startswith("AWS_ENDPOINT_URL")}
        env["AWS_PAGER"] = ""
        env["AWS_IGNORE_CONFIGURED_ENDPOINT_URLS"] = "true"
        # SubmitJob has no idempotency token. A lost response must be reconciled by name,
        # never retried automatically by the CLI transport.
        if args[:2] == ("batch", "submit-job"):
            env["AWS_MAX_ATTEMPTS"] = "1"
        command = ["aws", "--profile", self.profile, "--region", region or self.region, "--output", "json"]
        try:
            done = subprocess.run(command + list(args), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  universal_newlines=True, env=env)
        except FileNotFoundError:
            raise Refusal("the AWS CLI (aws, version 2) is not installed or not on PATH")
        if done.returncode:
            raise AwsError(list(args), done.stderr.strip() or "exit %d" % done.returncode)
        if parse:
            return json.loads(done.stdout) if done.stdout.strip() else {}
        return done.stdout


def aws_for(settings):
    return Aws(settings["PROFILE"], settings["REGION"])


def invoke_lambda(aws, function, payload):
    with tempfile.TemporaryDirectory() as scratch:
        out = Path(scratch) / "result.json"
        called = aws("lambda", "invoke", "--function-name", function,
                     "--cli-binary-format", "raw-in-base64-out", "--payload", json.dumps(payload), str(out))
        result = json.loads(out.read_text())
    if called.get("FunctionError") or not isinstance(result, dict) or "error" in result:
        raise ValueError("the control function failed: %s" % json.dumps(result))
    return result


class CliStore:
    """The bucket through the AWS CLI; same interface as core.LocalStore."""

    def __init__(self, aws, bucket):
        self.aws, self.bucket = aws, bucket

    def put_file(self, local, key):
        self.aws("s3api", "put-object", "--bucket", self.bucket, "--key", key, "--body", str(local))

    def put_bytes(self, data, key):
        with tempfile.NamedTemporaryFile() as stream:
            stream.write(data)
            stream.flush()
            self.put_file(stream.name, key)

    def get_file(self, key, local):
        Path(local).parent.mkdir(parents=True, exist_ok=True)
        self.aws("s3api", "get-object", "--bucket", self.bucket, "--key", key, str(local))

    def get_bytes(self, key):
        with tempfile.TemporaryDirectory() as scratch:
            target = Path(scratch) / "object"
            try:
                self.get_file(key, target)
            except AwsError as error:
                if error.code in ("NoSuchKey", "404"):
                    return None
                raise
            return target.read_bytes()

    def exists(self, key):
        try:
            self.aws("s3api", "head-object", "--bucket", self.bucket, "--key", key)
            return True
        except AwsError as error:
            if error.code in ("404", "NoSuchKey", "NotFound"):
                return False
            raise

    def list(self, prefix):
        found, token = [], None
        while True:
            args = ["s3api", "list-objects-v2", "--bucket", self.bucket, "--prefix", prefix, "--no-paginate"]
            listed = self.aws(*(args + (["--continuation-token", token] if token else [])))
            found += [(item["Key"], item["Size"]) for item in (listed or {}).get("Contents") or []]
            token = (listed or {}).get("NextContinuationToken")
            if not token:
                return found


def read_latch(aws, settings):
    """The cutoff state, or a refusal when it is latched, missing, unreadable or invalid."""
    try:
        raw = aws("ssm", "get-parameter", "--name", settings["STATE_PARAMETER"])["Parameter"]["Value"]
        state = json.loads(raw)
    except (AwsError, ValueError, KeyError) as error:
        raise Refusal("the budget cutoff state cannot be read (%s); no new cloud work" % error)
    if not valid_state(state, settings["PREFIX"], settings["REGION"]):
        raise Refusal("the budget cutoff state is invalid; an administrator must inspect it")
    if state["cutoff_active"]:
        raise Refusal("the budget cutoff is latched (%s, %s); an administrator lifts it with "
                      "`cloudsweep admin reset-cutoff`" % (state.get("reason", "?"), state.get("triggered_at", "?")))
    return state


def admit(repo, read=read_latch):
    """Settings and an AWS caller when new work is allowed; otherwise Refusal."""
    settings = load(repo)
    if settings[ENABLED] != "1":
        raise Refusal("cloud use is off (%s=%r in %s); set it to 1 to allow new cloud work"
                      % (ENABLED, settings[ENABLED], settings["_path"]))
    max_run_usd(settings)
    aws = aws_for(settings)
    read(aws, settings)
    return settings, aws


def max_run_usd(settings):
    try:
        ceiling = Decimal(settings[MAX_RUN])
        if ceiling.is_finite() and ceiling > 0:
            return ceiling
    except (KeyError, InvalidOperation):
        pass
    raise Refusal("%s needs %s, the ceiling in USD on one run's estimated cost; no new cloud work"
                  % (settings.get("_path", "deploy.env"), MAX_RUN))


def clean_commit(repo, revision):
    """The commit a revision names. Refused when it is the checkout's HEAD and tracked files have
    uncommitted changes: a run gets only committed source, never the working tree."""
    commit = project.git(repo, "rev-parse", "--verify", revision + "^{commit}")
    if commit == project.git(repo, "rev-parse", "HEAD") and project.git(
            repo, "status", "--porcelain", "--untracked-files=no"):
        raise Refusal("%s has uncommitted changes to tracked files; commit or stash them: a run uses "
                      "only committed source" % repo)
    return commit


def stage_inputs(store, inputs):
    """Upload each named input by content hash unless that object exists; returns name -> sha256."""
    staged = {}
    for name, path in sorted(inputs.items()):
        digest = core.sha256_file(path)
        if not any(key == "inputs/" + digest for key, _ in store.list("inputs/" + digest)):
            store.put_file(path, "inputs/" + digest)
        staged[name] = digest
    return staged


def active_definitions(aws, name):
    """Active revisions of a job definition, oldest first."""
    found = aws("batch", "describe-job-definitions", "--job-definition-name", name, "--status", "ACTIVE")
    return sorted(found.get("jobDefinitions", []), key=lambda item: item["revision"])


def identity_dir(prefix):
    """Where deploy installs the agent certificate and key (roles-anywhere identity)."""
    return Path(os.environ.get("CLOUDSWEEP_IDENTITY_DIR") or Path.home() / ".aws/rolesanywhere" / (prefix + "-agent"))


def certificate_expiry(path):
    """A certificate's expiry as a UTC datetime, or None when it is missing or unreadable."""
    try:
        text = subprocess.run(["openssl", "x509", "-noout", "-enddate", "-in", str(path)], check=True,
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, universal_newlines=True).stdout
        return datetime.fromtimestamp(ssl.cert_time_to_seconds(text.strip().split("=", 1)[1]), timezone.utc)
    except (OSError, subprocess.CalledProcessError, ValueError, IndexError):
        return None


def certificate_state(prefix, now=None):
    """The installed agent certificate's expiry, and whether the next deploy renews it:
    within RENEW_DAYS of expiry, or when the certificate, its key or its CA is missing."""
    directory = identity_dir(prefix)
    expires = certificate_expiry(directory / "agent.pem")
    if expires is None:
        return {"directory": str(directory), "expires": None, "days_left": None, "renew": True}
    left = expires - (now or datetime.now(timezone.utc))
    complete = all((directory / name).is_file() for name in ("agent.key", "ca.pem"))
    return {"directory": str(directory), "expires": expires.strftime("%Y-%m-%d"), "days_left": left.days,
            "renew": not complete or left <= timedelta(days=RENEW_DAYS)}


def write_receipt(directory, receipt, replace=False):
    """Atomically create or update this submission's receipt; first creation cannot overwrite."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / (receipt["submission"] + ".json")
    handle, temporary = tempfile.mkstemp(dir=str(directory), prefix=".receipt-")
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(core.pretty(receipt))
            stream.flush()
            os.fsync(stream.fileno())
        if replace:
            os.replace(temporary, str(target))
        else:
            try:
                os.link(temporary, str(target))
            except FileExistsError as error:
                raise ValueError("receipt exists: %s" % target) from error
    finally:
        Path(temporary).unlink(missing_ok=True)
    return target


def submission_of(job_name):
    """Job names are SUBMISSION-run."""
    base, _, kind = job_name.rpartition("-")
    return base if kind == "run" and core.SUBMISSION_RE.fullmatch(base) else None


def fail(error):
    print("cloudsweep: %s" % error, file=sys.stderr)
    return 1
