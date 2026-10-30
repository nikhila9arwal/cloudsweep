"""Administrator commands: deploy (create or update the stack), reset-cutoff and retire.

These run with an administrator AWS CLI profile (--admin-profile, CLOUDSWEEP_ADMIN_PROFILE,
or "default"), never with the agent profile they create.
"""

import configparser
import fcntl
import io
import json
import os
import re
import shlex
import subprocess
import tempfile
import time
import urllib.request
import zipfile
from decimal import Decimal, InvalidOperation
from pathlib import Path

from . import core, engine, project, settings
from .guard import initial_state, valid_state, validate_budget
from .project import Refusal

HERE = Path(__file__).resolve().parent
HELPER_URL = "https://rolesanywhere.amazonaws.com/releases/1.8.5/X86_64/Linux/Amzn2023/aws_signing_helper"
HELPER_SHA256 = "beec9ed1c492d93db809890f16713e3556353294b823c2184ad4e891f1b2b54d"
DEFAULT_TYPES = "m7a.large,m7a.xlarge,m7a.2xlarge,m7a.4xlarge"
# Must equal stack.yaml's defaults (tested): every deploy passes every parameter explicitly.
DEFAULTS = {"Prefix": "cloudsweep", "InstanceTypes": DEFAULT_TYPES, "MaxvCpus": "32", "RootVolumeGiB": "60",
            "QueueState": "ENABLED", "LogRetentionDays": "90", "Identity": "assume-role", "CaCertificate": "none",
            "AlertAmount1": "50", "AlertAmount2": "80", "CutoffAmount": "100"}
REQUIRED = ("VpcId", "Subnets", "AlertAmount1", "AlertAmount2", "CutoffAmount", "AlertEmail", "GuardCodeKey")
BOOTSTRAP = {
    "AWSTemplateFormatVersion": "2010-09-09",
    "Description": "First step of the cloudsweep stack: its bucket, so the guard code can be stored first",
    "Parameters": {"Prefix": {"Type": "String"}},
    "Resources": {"Bucket": {"Type": "AWS::S3::Bucket", "DeletionPolicy": "Retain", "UpdateReplacePolicy": "Retain",
                             "Properties": {
                                 "BucketName": {"Fn::Sub": "${Prefix}-${AWS::AccountId}-${AWS::Region}"},
                                 "PublicAccessBlockConfiguration": {
                                     "BlockPublicAcls": True, "BlockPublicPolicy": True,
                                     "IgnorePublicAcls": True, "RestrictPublicBuckets": True},
                                 "Tags": [{"Key": "Project", "Value": {"Ref": "Prefix"}}]}}},
    "Outputs": {"Bucket": {"Value": {"Ref": "Bucket"}}},
}


def die(message):
    raise Refusal(message)


def say(message):
    print("deploy: " + message, flush=True)


def money(value):
    try:
        amount = Decimal(value)
    except InvalidOperation:
        die("amounts must be USD decimals: %r" % value)
    if not amount.is_finite() or amount < 1 or amount.as_tuple().exponent < -2:
        die("amounts must be at least 1 USD with at most two decimal places: %r" % value)
    return amount


def admin_profile(args):
    profile = getattr(args, "admin_profile", None) or os.environ.get("CLOUDSWEEP_ADMIN_PROFILE") or "default"
    if profile.endswith("-agent"):
        die("use an administrator profile, not the agent profile")
    return profile


def admin_aws(args, region):
    return settings.Aws(admin_profile(args), region)


def configured_region(profile):
    done = subprocess.run(["aws", "configure", "get", "region", "--profile", profile], stdout=subprocess.PIPE,
                          stderr=subprocess.DEVNULL, universal_newlines=True)
    return done.stdout.strip() or None


# Identity (roles-anywhere only): a fresh CA and leaf certificate.

def openssl(*args, **kwargs):
    subprocess.run(["openssl"] + list(args), check=True, stdout=subprocess.DEVNULL,
                   stderr=subprocess.PIPE, **kwargs)


def prepare_identity(prefix):
    """Fresh issuer and leaf for a deploy; no named CA private-key file.

    Keep the pending leaf/key on AWS failure, and leave the installed leaf unchanged.
    The unlinked issuer file closes before any AWS operation, even on issuance failure.
    """
    name = prefix + "-agent"
    directory = settings.identity_dir(prefix)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory.chmod(0o700)
    pending = Path(tempfile.mkdtemp(prefix="pending-", dir=str(directory)))
    old = os.umask(0o077)
    try:
        with tempfile.TemporaryFile() as issuer, tempfile.TemporaryDirectory() as scratch:
            key_path = "/proc/self/fd/%d" % issuer.fileno()
            files = {part: str(pending / part) for part in ("ca.pem", "agent.pem", "agent.key")}
            extensions, request = Path(scratch) / "agent.ext", Path(scratch) / "agent.csr"
            extensions.write_text("basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature\n"
                                  "extendedKeyUsage=clientAuth\nsubjectKeyIdentifier=hash\nauthorityKeyIdentifier=keyid\n")
            openssl("req", "-x509", "-new", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1",
                    "-nodes", "-keyout", key_path, "-out", files["ca.pem"],
                    "-days", "3650", "-sha256", "-subj", "/CN=%s-ca" % name,
                    "-addext", "basicConstraints=critical,CA:TRUE",
                    "-addext", "keyUsage=critical,keyCertSign,cRLSign", pass_fds=(issuer.fileno(),))
            openssl("req", "-new", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes",
                    "-keyout", files["agent.key"], "-out", str(request), "-subj", "/CN=" + name)
            openssl("x509", "-req", "-in", str(request), "-CA", files["ca.pem"], "-CAkey", key_path,
                    "-set_serial", "0x" + os.urandom(16).hex(), "-days", "365", "-sha256",
                    "-extfile", str(extensions), "-out", files["agent.pem"], pass_fds=(issuer.fileno(),))
    finally:
        os.umask(old)
    return directory, pending


def install_identity(directory, pending):
    for name in ("ca.pem", "agent.pem", "agent.key"):
        (pending / name).chmod(0o600)
        (pending / name).replace(directory / name)
    pending.rmdir()


def reusable_identity(prefix):
    certificate = settings.certificate_state(prefix)
    identity = Path(certificate["directory"])
    # Expiry alone does not establish that the reusable leaf, CA and key belong together.
    openssl("verify", "-CAfile", str(identity / "ca.pem"), str(identity / "agent.pem"))
    public = subprocess.check_output(["openssl", "x509", "-in", str(identity / "agent.pem"), "-pubkey", "-noout"])
    if public != subprocess.check_output(["openssl", "pkey", "-in", str(identity / "agent.key"), "-pubout"]):
        die("installed certificate and key differ; rerun with --rotate-certificate")
    return identity


def install_helper():
    if os.uname().sysname != "Linux" or os.uname().machine != "x86_64":
        die("the pinned Roles Anywhere signing helper is for x86_64 Linux; use --identity assume-role here")
    helper = Path(os.environ.get("CLOUDSWEEP_SIGNING_HELPER") or Path.home() / ".local/bin/aws_signing_helper")
    helper.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not helper.exists():
        with tempfile.TemporaryDirectory(dir=str(helper.parent)) as scratch:
            download = Path(scratch) / "helper"
            with urllib.request.urlopen(HELPER_URL, timeout=120) as response:
                download.write_bytes(response.read())
            if core.sha256_file(download) != HELPER_SHA256:
                die("credential helper checksum mismatch")
            download.chmod(0o700)
            os.replace(str(download), str(helper))
    if core.sha256_file(helper) != HELPER_SHA256:
        die("existing %s differs from the pinned checksum" % helper)
    helper.chmod(0o700)
    return helper


def ensure_profile(name, wanted):
    """Add [profile name] to the AWS CLI config, or confirm it already says exactly this."""
    config_path = Path(os.environ.get("AWS_CONFIG_FILE") or Path.home() / ".aws/config")
    config = configparser.RawConfigParser()
    config.read(str(config_path))
    section = "profile " + name
    if config.has_section(section):
        if dict(config.items(section)) == wanted:
            return
        die("%s has a different [%s]; replace it with: %s" % (
            config_path, section, "; ".join("%s = %s" % item for item in sorted(wanted.items()))))
    config_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with open(str(config_path), "a") as stream:
        stream.write("\n[%s]\n%s" % (section, "".join("%s = %s\n" % item for item in wanted.items())))
    config_path.chmod(0o600)


def agent_profile(args, identity, outputs, region, directory=None):
    if identity == "assume-role":
        return {"role_arn": outputs["AgentRoleArn"], "source_profile": admin_profile(args), "region": region}
    helper = install_helper()
    command = shlex.join([str(helper), "credential-process", "--certificate", str(directory / "agent.pem"),
                          "--private-key", str(directory / "agent.key"),
                          "--trust-anchor-arn", outputs["TrustAnchorArn"], "--profile-arn", outputs["ProfileArn"],
                          "--role-arn", outputs["AgentRoleArn"]])
    return {"credential_process": command, "region": region}


# Stack.

def guard_zip():
    """guard.py as index.py in a deterministic zip, named by its hash."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        info = zipfile.ZipInfo("index.py", date_time=(1980, 1, 1, 0, 0, 0))
        info.external_attr = 0o644 << 16
        archive.writestr(info, (HERE / "guard.py").read_bytes())
    data = buffer.getvalue()
    return "lambda/guard-%s.zip" % core.sha256_bytes(data), data


def describe_stack(aws, name):
    try:
        stack = aws("cloudformation", "describe-stacks", "--stack-name", name)["Stacks"][0]
    except settings.AwsError as error:
        if "does not exist" in error.message:
            return None
        raise
    return {"status": stack["StackStatus"],
            "parameters": {p["ParameterKey"]: p.get("ParameterValue", "") for p in stack.get("Parameters", [])},
            "outputs": {o["OutputKey"]: o["OutputValue"] for o in stack.get("Outputs", [])}}


def read_admin_latch(aws, prefix, region):
    """'absent', or the validated state; anything unreadable stops deploy."""
    name = "/%s/cloud/cutoff" % prefix
    try:
        raw = aws("ssm", "get-parameter", "--name", name)["Parameter"]["Value"]
    except settings.AwsError as error:
        if error.code == "ParameterNotFound":
            return "absent"
        die("cannot read %s: %s" % (name, error))
    try:
        state = json.loads(raw)
    except ValueError:
        state = None
    if not valid_state(state, prefix, region):
        die("invalid cutoff state at %s; inspect it" % name)
    return state


def cloudformation_deploy(aws, stack, template, parameters, prefix):
    overrides = ["%s=%s" % item for item in sorted(parameters.items())]
    aws("cloudformation", "deploy", "--stack-name", stack, "--template-file", str(template),
        "--capabilities", "CAPABILITY_NAMED_IAM", "--no-fail-on-empty-changeset",
        "--tags", "Project=" + prefix, "--parameter-overrides", *overrides, parse=False)


def queue_state(latch):
    """The queue and compute environment stay disabled while the cutoff is latched."""
    return "DISABLED" if isinstance(latch, dict) and latch["cutoff_active"] else "ENABLED"


def parameters_from(args, previous, guard_key, latch):
    given = {"Prefix": args.prefix}
    for key, value in (("VpcId", args.vpc), ("Subnets", args.subnets), ("AlertEmail", args.email),
                       ("CutoffAmount", args.cutoff), ("Identity", args.identity),
                       ("InstanceTypes", args.instance_types)):
        if value:
            given[key] = value
    if args.max_vcpus is not None:
        given["MaxvCpus"] = str(args.max_vcpus)
    if args.disk_gib is not None:
        given["RootVolumeGiB"] = str(args.disk_gib)
    if args.alerts:
        given["AlertAmount1"], given["AlertAmount2"] = args.alerts
    # Parameters only an earlier template declared are dropped on upgrade.
    declared = set(DEFAULTS) | set(REQUIRED)
    parameters = dict(DEFAULTS, **{key: value for key, value in previous.items() if key in declared})
    parameters.update(given, GuardCodeKey=guard_key, QueueState=queue_state(latch))
    if not parameters.get("AlertEmail"):
        die("the first deploy needs --email: the address AWS sends budget alerts and guard warnings to")
    alerts = [money(parameters["AlertAmount1"]), money(parameters["AlertAmount2"])]
    if not alerts[0] < alerts[1] < money(parameters["CutoffAmount"]):
        die("alerts must be two increasing amounts below the cutoff")
    return parameters


def check_lambda_concurrency(aws):
    """The guard reserves one concurrent Lambda execution, and AWS keeps 100 unreserved: an
    account whose quota leaves fewer than 101 unreserved cannot create the guard."""
    limit = aws("lambda", "get-account-settings")["AccountLimit"]
    if limit.get("UnreservedConcurrentExecutions", 0) < 101:
        die("this account's Lambda concurrency (%s total, %s unreserved) is too low for the guard, which "
            "reserves 1 while AWS keeps 100 unreserved. New accounts often have a quota of 10: request "
            "'Concurrent executions' of at least 1000 in Service Quotas (Lambda) for %s, then rerun deploy"
            % (limit.get("ConcurrentExecutions"), limit.get("UnreservedConcurrentExecutions"), aws.region))


def default_network(aws):
    """The default VPC and its default subnets (public, with an internet-gateway route)."""
    vpcs = aws("ec2", "describe-vpcs", "--filters", "Name=isDefault,Values=true")["Vpcs"]
    if not vpcs:
        die("this region has no default VPC; pass --vpc and --subnets (public subnets that assign "
            "public IPv4 addresses and route 0.0.0.0/0 to an internet gateway)")
    vpc = vpcs[0]["VpcId"]
    subnets = aws("ec2", "describe-subnets", "--filters", "Name=vpc-id,Values=" + vpc,
                  "Name=default-for-az,Values=true")["Subnets"]
    if not subnets:
        die("the default VPC %s has no default subnets; pass --subnets" % vpc)
    return vpc, ",".join(sorted(subnet["SubnetId"] for subnet in subnets))


def network_problems(aws, vpc, subnets):
    problems = []
    if not vpc or not subnets or not all(subnets):
        return ["supply a VPC and public subnets"]
    found = aws("ec2", "describe-subnets", "--subnet-ids", *subnets)["Subnets"]
    tables = aws("ec2", "describe-route-tables", "--filters", "Name=vpc-id,Values=" + vpc)["RouteTables"]
    for subnet in found:
        name = subnet["SubnetId"]
        if subnet["VpcId"] != vpc:
            problems.append("%s is not in %s" % (name, vpc))
        if not subnet.get("MapPublicIpOnLaunch"):
            problems.append("%s does not assign public addresses" % name)
        explicit = [t for t in tables if any(a.get("SubnetId") == name for a in t["Associations"])]
        effective = explicit or [t for t in tables if any(a.get("Main") for a in t["Associations"])]
        if len(effective) != 1 or not any(r.get("DestinationCidrBlock") == "0.0.0.0/0"
                                          and r.get("GatewayId", "").startswith("igw-")
                                          and r.get("State") == "active" for r in effective[0]["Routes"]):
            problems.append("%s lacks an active default route to an internet gateway" % name)
    if not aws("ec2", "describe-vpc-attribute", "--vpc-id", vpc,
               "--attribute", "enableDnsSupport")["EnableDnsSupport"]["Value"]:
        problems.append("VPC DNS support is disabled")
    return problems


def instance_specs(aws, names, prices):
    """vCPUs, memory and On-Demand Linux price for each type; one shared architecture."""
    described = aws("ec2", "describe-instance-types", "--instance-types", *names)["InstanceTypes"]
    found = {item["InstanceType"]: item for item in described}
    missing = sorted(set(names) - set(found))
    if missing:
        die("instance types not offered in this region: " + ", ".join(missing))
    architectures = set()
    types = {}
    for name in names:
        item = found[name]
        supported = item["ProcessorInfo"]["SupportedArchitectures"]
        architectures.add("x86_64" if "x86_64" in supported else supported[0])
        price = prices.get(name) or on_demand_price(aws, name)
        types[name] = {"vcpus": item["VCpuInfo"]["DefaultVCpus"], "memory_mib": item["MemoryInfo"]["SizeInMiB"],
                       "usd_per_hour": str(price)}
    if len(architectures) != 1 or not architectures <= set(engine.PLATFORMS):
        die("instance types must share one architecture, x86_64 or arm64 (found %s)" % ", ".join(sorted(architectures)))
    return core.check_instance_types(types), architectures.pop()


def on_demand_price(aws, name):
    """The On-Demand Linux price per hour from the AWS Price List API (served from us-east-1)."""
    filters = ["Type=TERM_MATCH,Field=%s,Value=%s" % pair for pair in (
        ("instanceType", name), ("regionCode", aws.region), ("operatingSystem", "Linux"), ("tenancy", "Shared"),
        ("preInstalledSw", "NA"), ("capacitystatus", "Used"))]
    try:
        listed = aws("pricing", "get-products", "--service-code", "AmazonEC2", "--format-version", "aws_v1",
                     "--filters", *filters, region="us-east-1")
    except settings.AwsError as error:
        die("cannot read the price of %s (%s); pass --price %s=<USD per hour>" % (name, error.message, name))
    prices = set()
    for item in listed.get("PriceList", []):
        product = json.loads(item) if isinstance(item, str) else item
        if product.get("product", {}).get("attributes", {}).get("licenseModel", "No License required") \
                != "No License required":
            continue
        for term in product.get("terms", {}).get("OnDemand", {}).values():
            for dimension in term.get("priceDimensions", {}).values():
                if dimension.get("unit") == "Hrs" and Decimal(dimension["pricePerUnit"].get("USD", "0")) > 0:
                    prices.add(Decimal(dimension["pricePerUnit"]["USD"]))
    if len(prices) != 1:
        die("found %d On-Demand Linux prices for %s in %s; pass --price %s=<USD per hour>"
            % (len(prices), name, aws.region, name))
    return prices.pop().normalize()


def activate_cost_tag(aws):
    """Ask Billing to count the Project tag; it can only be activated after AWS has seen it."""
    try:
        result = aws("ce", "update-cost-allocation-tags-status", "--cost-allocation-tags-status",
                     "TagKey=Project,Status=Active", region="us-east-1")
    except settings.AwsError as error:
        if "Linked account" in error.message:
            return ("managed by your organization's management account: activate the Project tag there "
                    "(Billing and Cost Management, Cost allocation tags)")
        return ("not active yet: AWS lists a new tag up to 24 hours after its first use, and the guard "
                "activates it then (%s)" % error.message.splitlines()[-1])
    if result.get("Errors"):
        return "not active yet (%s); the guard retries daily" % json.dumps(result["Errors"])
    return "active"


def write_deploy_env(repo, prefix, region, identity, outputs, types, architecture):
    """The local settings; the user's flag and ceiling survive a rerun."""
    path = settings.settings_path(repo)
    previous = settings.read_values(path) if path.exists() else {}
    flag = previous.get(settings.ENABLED, "1")
    values = {settings.ENABLED: flag if flag in ("0", "1") else "0",
              settings.MAX_RUN: previous.get(settings.MAX_RUN, "10"),
              "PREFIX": prefix, "REGION": region, "PROFILE": prefix + "-agent", "IDENTITY": identity,
              "BUCKET": outputs["Bucket"], "QUEUE": outputs["Queue"],
              "COMPUTE_ENVIRONMENT": outputs["ComputeEnvironment"],
              "RUN_JOB_DEFINITION": outputs["RunJobDefinitionName"],
              "CONTROL_FUNCTION": outputs["ControlFunction"], "STATE_PARAMETER": outputs["StateParameter"],
              "LOG_GROUP": outputs["JobLogGroup"], "REPOSITORY": outputs["Repository"],
              "RECEIPTS": previous.get("RECEIPTS") or str(settings.state_dir(repo) / "receipts"),
              "ARCHITECTURE": architecture, "INSTANCE_TYPES": types}
    settings.write(path, values)
    ignore = project.main_checkout(repo) / ".gitignore"
    lines = ignore.read_text().splitlines() if ignore.exists() else []
    if not any(line.strip().rstrip("/") in (".cloudsweep", "/.cloudsweep") for line in lines):
        say("note: add .cloudsweep/ to %s (it holds local deployment settings and receipts)" % ignore)
    return path, values[settings.ENABLED]


def deploy(args):
    validate(args)
    repo = project.repository()
    config = project.load(repo) if (repo / project.NAME).is_file() else None
    path = settings.settings_path(repo)
    recorded = settings.read_values(path) if path.is_file() else {}
    # A rerun updates the recorded deployment unless told otherwise.
    args.prefix = args.prefix or recorded.get("PREFIX") or (project.name(config) if config else None)
    if not args.prefix or not project.PREFIX_RE.fullmatch(args.prefix):
        die("name the deployment: [project] name in cloudsweep.ini, or --prefix (1-20 lower-case letters, "
            "digits or hyphens, starting with a letter)")
    prefix = args.prefix
    profile = admin_profile(args)
    region = (args.region or (recorded.get("REGION") if recorded.get("PREFIX") == prefix else None)
              or configured_region(profile) or "us-east-1")
    stack = prefix + "-cloud"
    aws = admin_aws(args, region)
    say("administrator %s in %s (profile %s)" % (aws("sts", "get-caller-identity")["Arn"], region, profile))
    guard_key, guard_data = guard_zip()
    latch = read_admin_latch(aws, prefix, region)
    current = describe_stack(aws, stack)
    if current and ("Bucket" not in current["outputs"] or not current["status"].endswith("_COMPLETE")
                    or current["status"] in ("ROLLBACK_COMPLETE", "DELETE_COMPLETE")):
        die("existing stack %s is %s and needs inspection in the CloudFormation console" % (stack, current["status"]))
    previous = current["parameters"] if current and "GuardCodeKey" in current["parameters"] else {}
    if not previous and queue_state(latch) == "DISABLED":
        die("a cutoff latch from an earlier deployment is set; inspect /%s/cloud/cutoff first" % prefix)
    if not previous:
        check_lambda_concurrency(aws)
    if not (args.vpc or previous.get("VpcId")):
        args.vpc, found = default_network(aws)
        args.subnets = args.subnets or found
        say("using the default VPC %s and its subnets %s" % (args.vpc, args.subnets))
    # Validate before writing identity files or creating even the bootstrap bucket.
    parameters = parameters_from(args, previous, guard_key, latch)
    prices = {}
    for item in args.price or []:
        name, _, value = item.partition("=")
        prices[name] = Decimal(value)
    names = [name.strip() for name in parameters["InstanceTypes"].split(",") if name.strip()]
    types, architecture = instance_specs(aws, names, prices)
    smallest = min(spec["vcpus"] for spec in types.values())
    if int(parameters["MaxvCpus"]) < smallest:
        die("--max-vcpus must be at least %d, the smallest instance's vCPUs" % smallest)
    parameters["InstanceTypes"] = ",".join(names)
    problems = network_problems(aws, parameters["VpcId"], parameters["Subnets"].split(","))
    if problems:
        die("; ".join(problems))
    identity, pending = None, None
    if parameters["Identity"] == "roles-anywhere":
        certificate = settings.certificate_state(prefix)
        if args.rotate_certificate or certificate["renew"]:
            identity, pending = prepare_identity(prefix)
        else:
            identity = reusable_identity(prefix)
        parameters["CaCertificate"] = ((pending or identity) / "ca.pem").read_text()
    else:
        parameters["CaCertificate"] = "none"
    if current is None:
        say("creating the bucket (first step)")
        with tempfile.NamedTemporaryFile("w", suffix=".json") as bootstrap:
            json.dump(BOOTSTRAP, bootstrap)
            bootstrap.flush()
            cloudformation_deploy(aws, stack, bootstrap.name, {"Prefix": prefix}, prefix)
        current = describe_stack(aws, stack)
    store = settings.CliStore(aws, current["outputs"]["Bucket"])
    if not store.exists(guard_key):
        store.put_bytes(guard_data, guard_key)
    say("deploying stack %s (several minutes on first deploy)" % stack)
    cloudformation_deploy(aws, stack, HERE / "stack.yaml", parameters, prefix)
    if pending:
        install_identity(identity, pending)
    outputs = describe_stack(aws, stack)["outputs"]
    if latch == "absent":
        # Create only after provisioning, and never overwrite: a rerun cannot reset a latch.
        try:
            aws("ssm", "put-parameter", "--name", outputs["StateParameter"], "--type", "String",
                "--value", json.dumps(initial_state(prefix, region), separators=(",", ":")),
                "--no-overwrite", "--tags", "Key=Project,Value=" + prefix)
        except settings.AwsError as error:
            if error.code != "ParameterAlreadyExists":
                raise
    reconcile_latch(aws, prefix, region, outputs)
    check_cluster_scope(aws, prefix, region, outputs)
    ensure_profile(prefix + "-agent", agent_profile(args, parameters["Identity"], outputs, region, identity))
    path, flag = write_deploy_env(repo, prefix, region, parameters["Identity"], outputs, types, architecture)
    agent = settings.Aws(prefix + "-agent", region)
    for attempt in range(6):
        try:
            say("agent profile %s-agent works as %s" % (prefix, agent("sts", "get-caller-identity")["Arn"]))
            break
        except settings.AwsError:
            time.sleep(10)
    else:
        die("the agent profile %s-agent does not work yet; the stack and %s exist. Check that the "
            "administrator profile may call sts:AssumeRole, then rerun deploy" % (prefix, path))
    if parameters["Identity"] == "roles-anywhere":
        say("%s the agent certificate, which expires %s" % (
            "issued" if pending else "kept", settings.certificate_state(prefix)["expires"]))
    tag = activate_cost_tag(aws)
    say("wrote %s (%s=%s, %s=%s)" % (path, settings.ENABLED, flag, settings.MAX_RUN,
                                     settings.read_values(path)[settings.MAX_RUN]))
    say("instance types: %s" % ", ".join("%s (%d vCPU, %d MiB, $%s/h)" % (
        name, spec["vcpus"], spec["memory_mib"], spec["usd_per_hour"]) for name, spec in core.by_size(types)))
    say("budget: alerts at $%s and $%s, cutoff at $%s per month, emails to %s" % (
        parameters["AlertAmount1"], parameters["AlertAmount2"], parameters["CutoffAmount"], parameters["AlertEmail"]))
    if tag != "active":
        say("cost-allocation tag Project: %s. AWS Budgets counts only active tags, so until then the budget "
            "cutoff sees no spend; the vCPU cap and the per-run ceiling still bound cost." % tag)
    if not previous:
        say("first deploy: confirm the subscription email AWS sent to %s (guard warnings)" % parameters["AlertEmail"])
    say("next: `cloudsweep image build` and `cloudsweep image publish`, then `cloudsweep submit`")
    return 0


def check_cluster_scope(aws, prefix, region, outputs):
    ce = aws("batch", "describe-compute-environments", "--compute-environments",
             outputs["ComputeEnvironment"])["computeEnvironments"][0]
    account = outputs["AgentRoleArn"].split(":")[4]
    expected = ":ecs:%s:%s:cluster/AWSBatch-%s-cpu-" % (region, account, prefix)
    if expected not in ce.get("ecsClusterArn", ""):
        die("Batch's ECS cluster ARN does not match the guard policy; keep cloud use off and inspect it")


def reconcile_latch(aws, prefix, region, outputs):
    # Re-read after the long stack update: the guard may have latched meanwhile.
    latch = read_admin_latch(aws, prefix, region)
    if latch == "absent":
        die("cutoff state is absent after deployment; keep cloud use off")
    if latch["cutoff_active"]:
        aws("iam", "attach-role-policy", "--role-name", prefix + "-agent", "--policy-arn", outputs["SubmitDenyArn"])
        aws("batch", "update-job-queue", "--job-queue", outputs["Queue"], "--state", "DISABLED")
        aws("batch", "update-compute-environment", "--compute-environment", outputs["ComputeEnvironment"],
            "--state", "DISABLED")
        say("the cutoff is latched; queue and compute environment disabled")


def previous_stack(aws, prefix):
    current = describe_stack(aws, prefix + "-cloud")
    if current is None or "GuardCodeKey" not in current["parameters"]:
        die("no deployed stack %s-cloud; run `cloudsweep deploy` first" % prefix)
    return current


def reset_cutoff(args):
    values = settings.load(project.repository())
    prefix, region = values["PREFIX"], values["REGION"]
    aws = admin_aws(args, region)
    current = previous_stack(aws, prefix)
    latch = read_admin_latch(aws, prefix, region)
    budget = aws("budgets", "describe-budget", "--account-id", current["outputs"]["AgentRoleArn"].split(":")[4],
                 "--budget-name", prefix + "-monthly")["Budget"]
    spend = validate_budget(budget, {"PROJECT": prefix, "BUDGET_NAME": prefix + "-monthly",
                                     "CUTOFF_AMOUNT": current["parameters"]["CutoffAmount"]})
    actual = Decimal(spend["actual_usd"])
    if actual >= Decimal(spend["cutoff_usd"]):
        die("reported spend %s is at or above the cutoff %s; raise it first with `cloudsweep deploy --cutoff`"
            % (actual, budget["BudgetLimit"]["Amount"]))
    rule, function = prefix + "-guard-reconcile", prefix + "-guard"
    aws("events", "disable-rule", "--name", rule)
    aws("lambda", "put-function-concurrency", "--function-name", function, "--reserved-concurrent-executions", "0")
    try:
        print("reset-cutoff: pausing the guard for 130 s so in-flight runs finish", flush=True)
        time.sleep(130)  # in-flight guard invocations finish (its timeout is 120 s)
        try:
            aws("iam", "detach-role-policy", "--role-name", prefix + "-agent",
                "--policy-arn", current["outputs"]["SubmitDenyArn"])
        except settings.AwsError as error:
            if error.code != "NoSuchEntity":
                raise
        aws("ssm", "put-parameter", "--name", current["outputs"]["StateParameter"], "--type", "String",
            "--overwrite", "--value", json.dumps(initial_state(prefix, region), separators=(",", ":")))
        # No stack deployment here: it could resume the guard inside this paused section.
        aws("batch", "update-compute-environment", "--compute-environment", current["outputs"]["ComputeEnvironment"],
            "--state", "ENABLED")
        aws("batch", "update-job-queue", "--job-queue", current["outputs"]["Queue"], "--state", "ENABLED")
    finally:
        # Resuming the guard is always safe: a latch that is still set is enforced again.
        aws("lambda", "put-function-concurrency", "--function-name", function, "--reserved-concurrent-executions", "1")
        aws("events", "enable-rule", "--name", rule)
    print("reset-cutoff: cleared (was %s); reported spend %s of %s" % (
        latch if latch == "absent" else latch.get("reason"), actual, budget["BudgetLimit"]["Amount"]))
    return 0


def retire(args):
    if not args.yes:
        die("retire deletes the compute, guard and agent identity; rerun with --yes")
    repo = project.repository()
    values = settings.load(repo)
    prefix, region = values["PREFIX"], values["REGION"]
    aws = admin_aws(args, region)
    current = describe_stack(aws, prefix + "-cloud")
    if current is None and args.delete_data:
        # A rerun after --delete-data stopped partway: the stack is gone, the data may not be.
        print("retire: stack %s-cloud is already deleted" % prefix, flush=True)
    else:
        delete_stack(aws, dict(values), prefix, previous_stack(aws, prefix))
    # The stack removes only its own current revision; agents registered the others.
    for definition in settings.active_definitions(aws, values["RUN_JOB_DEFINITION"]):
        aws("batch", "deregister-job-definition", "--job-definition", definition["jobDefinitionArn"])
    if args.delete_data:
        delete_data(aws, values, prefix)
        return 0
    print("retire: deleted %s-cloud and its job definitions. Kept, and still billed for storage: bucket %s and "
          "image repository %s. Also left: the latch parameter %s, the %s-agent profile in your AWS config and "
          "%s. To delete all of them, rerun with --delete-data."
          % (prefix, values["BUCKET"], values["REPOSITORY"], values["STATE_PARAMETER"], prefix, values["_path"]))
    return 0


def delete_stack(aws, values, prefix, current):
    """Close admission, stop every job, wait for the instances to go, then delete the stack."""
    outputs = current["outputs"]
    values[settings.ENABLED] = "0"
    settings.write(values["_path"], values)
    # Close admission before listing jobs to stop; keep the guard alive during drainage.
    aws("iam", "attach-role-policy", "--role-name", prefix + "-agent", "--policy-arn", outputs["SubmitDenyArn"])
    for name, command in (("job-queue", "update-job-queue"), ("compute-environment", "update-compute-environment")):
        target = outputs["Queue"] if name == "job-queue" else outputs["ComputeEnvironment"]
        aws("batch", command, "--" + name, target, "--state", "DISABLED")

    def control(payload):
        result = settings.invoke_lambda(aws, outputs["ControlFunction"], payload)
        if result.get("errors") or "capacity" not in result or "stopped" not in result:
            die("cancellation incomplete; stack retained: " + json.dumps(result))
        return result
    result = control({"all": True})
    print("retire: stop requests %s" % json.dumps(result["stopped"]), flush=True)
    deadline = time.time() + 1800
    while True:
        capacity = result["capacity"]
        # The administrator can check real EC2 inventory, including instances that never
        # registered with ECS. A project-tag collision requires inspection, never deletion.
        reservations = aws("ec2", "describe-instances", "--filters", "Name=tag:Project,Values=" + prefix,
                           "Name=instance-state-name,Values=pending,running,shutting-down,stopping,stopped")
        remaining = [i["InstanceId"] for r in reservations.get("Reservations", []) for i in r.get("Instances", [])]
        if capacity["desired_vcpus"] == 0 and capacity["instances"] == 0 and not remaining:
            break
        if time.time() >= deadline:
            die("capacity still present after 30 minutes; stack retained: %s; tagged EC2 %s" % (capacity, remaining))
        print("retire: waiting for instances to stop (Batch scales down a few minutes after work ends): "
              "%d desired vCPUs, %d registered instance(s), EC2 %s"
              % (capacity["desired_vcpus"], capacity["instances"], ", ".join(remaining) or "none"), flush=True)
        time.sleep(30)
        result = control({"status": True})
    # Prevent an in-flight reconciler from reattaching the policy during deletion.
    aws("events", "disable-rule", "--name", prefix + "-guard-reconcile")
    aws("lambda", "put-function-concurrency", "--function-name", prefix + "-guard",
        "--reserved-concurrent-executions", "0")
    time.sleep(130)
    aws("iam", "detach-role-policy", "--role-name", prefix + "-agent", "--policy-arn", outputs["SubmitDenyArn"])
    aws("cloudformation", "delete-stack", "--stack-name", prefix + "-cloud")
    aws("cloudformation", "wait", "stack-delete-complete", "--stack-name", prefix + "-cloud", parse=False)


def delete_data(aws, values, prefix):
    """--delete-data: delete what the stack keeps, then the local settings. Safe to rerun."""
    bucket = values["BUCKET"]
    deleted = 0
    with tempfile.TemporaryDirectory() as scratch:
        batch = Path(scratch) / "delete.json"
        while True:
            try:
                listing = aws("s3api", "list-object-versions", "--bucket", bucket, "--max-items", "1000")
            except settings.AwsError as error:
                if "NoSuchBucket" in error.message:
                    break
                raise
            objects = [{"Key": item["Key"], "VersionId": item["VersionId"]}
                       for item in (listing.get("Versions") or []) + (listing.get("DeleteMarkers") or [])]
            if not objects:
                aws("s3api", "delete-bucket", "--bucket", bucket)
                break
            for start in range(0, len(objects), 1000):
                batch.write_text(json.dumps({"Objects": objects[start:start + 1000], "Quiet": True}))
                result = aws("s3api", "delete-objects", "--bucket", bucket, "--delete", "file://" + str(batch))
                if result.get("Errors"):
                    die("could not delete objects in %s: %s; rerun retire --delete-data"
                        % (bucket, json.dumps(result["Errors"][:3])))
                deleted += min(1000, len(objects) - start)
            print("retire: deleted %d object versions from %s" % (deleted, bucket), flush=True)
    print("retire: bucket %s is gone" % bucket, flush=True)
    for command, missing in ((("ecr", "delete-repository", "--repository-name", prefix + "-worker", "--force"),
                              "RepositoryNotFoundException"),
                             (("ssm", "delete-parameter", "--name", values["STATE_PARAMETER"]), "ParameterNotFound")):
        try:
            aws(*command)
        except settings.AwsError as error:
            if missing not in error.message:
                raise
    print("retire: image repository %s-worker and parameter %s are gone" % (prefix, values["STATE_PARAMETER"]),
          flush=True)
    removed = remove_profile(prefix + "-agent")
    directory = settings.identity_dir(prefix)
    for name in ("ca.pem", "agent.pem", "agent.key"):
        if (directory / name).exists():
            (directory / name).unlink()
    if directory.is_dir() and not any(directory.iterdir()):
        directory.rmdir()
    Path(values["_path"]).unlink()
    print("retire: removed %s, the agent certificate (if any) and %s. Left on this machine: receipts and "
          "acceptance results in %s, and local container images."
          % ("the %s-agent profile from %s" % (prefix, removed) if removed else "no agent profile",
             values["_path"], Path(values["_path"]).parent))


def remove_profile(name):
    """Delete [profile name] from the AWS CLI config, keeping every other line as it was."""
    config_path = Path(os.environ.get("AWS_CONFIG_FILE") or Path.home() / ".aws/config")
    if not config_path.exists():
        return None
    lines = config_path.read_text().splitlines(keepends=True)
    kept, inside, found = [], False, False
    for line in lines:
        header = re.match(r"\s*\[(.*)\]\s*$", line)
        if header:
            inside = header.group(1).strip() == "profile " + name
            found = found or inside
        if not inside:
            kept.append(line)
    if not found:
        return None
    config_path.write_text("".join(kept))
    return config_path


def locked(action, args):
    """Serialize administrator commands on this host (remote updates and local identity files)."""
    lock = Path.home() / ".aws/cloudsweep-admin.lock"
    lock.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with lock.open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            die("another cloudsweep administrator command is running on this host")
        return action(args)


def add_arguments(commands):
    up = commands.add_parser(
        "deploy", help="(administrator) create or update the AWS stack",
        description="Create or update billable AWS resources and the agent profile. "
                    "Reruns preserve omitted settings. See docs/admin.md for costs and limits.")
    up.add_argument("--admin-profile", help="administrator AWS CLI profile (default: $CLOUDSWEEP_ADMIN_PROFILE or default)")
    up.add_argument("--region", help="region (default: recorded for this deployment, else admin profile's, else us-east-1)")
    up.add_argument("--prefix", help="deployment name (default: recorded name, else [project] name)")
    up.add_argument("--email", help="address for budget alerts and guard warnings (required the first time)")
    up.add_argument("--instance-types", help="comma-separated EC2 types (default %s)" % DEFAULT_TYPES)
    up.add_argument("--max-vcpus", type=int, help="vCPU cap; Batch may exceed it by one instance (default %s)" % DEFAULTS["MaxvCpus"])
    up.add_argument("--alerts", type=lambda text: text.split(","), metavar="A1,A2",
                    help="monthly email alert amounts in USD (default %s,%s)" % (DEFAULTS["AlertAmount1"], DEFAULTS["AlertAmount2"]))
    up.add_argument("--cutoff", help="monthly reported spend in USD that stops all work (default %s)" % DEFAULTS["CutoffAmount"])
    up.add_argument("--disk-gib", type=int, help="root disk per instance in GiB (default %s)" % DEFAULTS["RootVolumeGiB"])
    up.add_argument("--vpc", help="VPC id (default: the region's default VPC)")
    up.add_argument("--subnets", help="comma-separated public subnet ids of that VPC")
    up.add_argument("--identity", choices=("assume-role", "roles-anywhere"),
                    help="how the agent profile signs in (default assume-role)")
    up.add_argument("--price", action="append", metavar="TYPE=USD",
                    help="On-Demand USD per hour for a type, when the Price List API cannot be read")
    up.add_argument("--rotate-certificate", action="store_true",
                    help="(roles-anywhere) force a new agent certificate")
    up.set_defaults(handler=lambda args: locked(deploy, args))

    group = commands.add_parser("admin", help="(administrator) reset the budget cutoff or remove the stack")
    actions = group.add_subparsers(dest="admin_action", required=True)
    reset = actions.add_parser("reset-cutoff", help="lift a tripped budget cutoff",
                               description="Clear a latched budget cutoff and re-enable the queue. Raise the "
                                           "cutoff first (`cloudsweep deploy --cutoff`) if spend is above it.")
    reset.add_argument("--admin-profile")
    reset.set_defaults(handler=lambda args: locked(reset_cutoff, args))
    gone = actions.add_parser("retire", help="stop all work and delete the stack (bucket and images are kept)",
                              description="Stop jobs, wait for instances to terminate, then delete the stack and "
                                          "job definitions. Retained storage remains billable. "
                                          "See docs/admin.md for retained resources and cleanup.")
    gone.add_argument("--admin-profile")
    gone.add_argument("--yes", action="store_true", help="confirm")
    gone.add_argument("--delete-data", action="store_true",
                      help="permanently delete all bucket versions, images and deployment settings; "
                           "cannot be undone. Fetch needed results first")
    gone.set_defaults(handler=lambda args: locked(retire, args))


def validate(args):
    if getattr(args, "alerts", None) is not None and len(args.alerts) != 2:
        die("--alerts takes two amounts, A1,A2")
    for item in getattr(args, "price", None) or []:
        name, _, value = item.partition("=")
        try:
            ok = name and Decimal(value) > 0
        except InvalidOperation:
            ok = False
        if not ok or not re.fullmatch(r"[a-z0-9-]+\.[a-z0-9-]+", name):
            die("--price takes TYPE=USD_PER_HOUR, e.g. m7a.large=0.1159")
