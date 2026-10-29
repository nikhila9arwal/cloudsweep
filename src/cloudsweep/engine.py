"""Container images on this host and in the deployment's registry, with docker or podman.

The base image is the project's toolchain (its own Dockerfile) with cloudsweep's worker
layered on top. A revision image is built FROM a published base by digest: a clean checkout
of one commit, with its .git, at /work/src, and whatever the project's build command makes
there. Its tag names the commit and the build key, so each revision is built and pushed
once. Each image a job runs is pinned by digest in a revision of the run job definition.

CLOUDSWEEP_ENGINE selects docker or podman (default: podman if installed, else docker).
CLOUDSWEEP_PODMAN_ROOT moves podman's storage, e.g. off a network home directory.
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

from . import core, settings
from .project import Refusal

HERE = Path(__file__).resolve().parent
WORKER_FILES = ("worker.py", "core.py", "s3lite.py")
BASE_TAG, REVISION_TAG = "base-", "rev-"
PLATFORMS = {"x86_64": "linux/amd64", "arm64": "linux/arm64"}  # deployment architecture -> image platform
WORKER_LAYER = """\
FROM {toolchain}
USER 0
COPY worker.py core.py s3lite.py /opt/cloudsweep/
RUN ["/bin/sh", "-c", "python3 /opt/cloudsweep/worker.py check \\
 && mkdir -p {work} && chmod 1777 {work} \\
 && git config --system --add safe.directory '*' \\
 && (grep -q '^[^:]*:[^:]*:1000:' /etc/passwd || echo 'cloudsweep:x:1000:1000::{work}:/bin/sh' >> /etc/passwd) \\
 && (grep -q '^[^:]*:[^:]*:1000:' /etc/group || echo 'cloudsweep:x:1000:' >> /etc/group)"]
ENV HOME={work}
USER 1000:1000
WORKDIR {work}
LABEL org.cloudsweep.worker="1"
ENTRYPOINT ["/usr/bin/env", "python3", "/opt/cloudsweep/worker.py"]
CMD []
"""
REVISION = """\
FROM {base}
USER 0
COPY src {work}/src
COPY {recipe} {work}/{recipe}
RUN ["/usr/bin/env", "python3", "/opt/cloudsweep/worker.py", "build"]
USER 1000:1000
"""


def engine_name():
    chosen = os.environ.get("CLOUDSWEEP_ENGINE")
    if chosen:
        if chosen not in ("docker", "podman"):
            raise Refusal("CLOUDSWEEP_ENGINE must be docker or podman, not %r" % chosen)
        if not shutil.which(chosen):
            raise Refusal("CLOUDSWEEP_ENGINE is %s, which is not installed" % chosen)
        return chosen
    for name in ("podman", "docker"):
        if shutil.which(name):
            return name
    raise Refusal("neither podman nor docker is installed; cloudsweep builds images with one of them")


def engine(*args, **kwargs):
    """Run the container engine; podman uses CLOUDSWEEP_PODMAN_ROOT for storage when set."""
    name = engine_name()
    argv, env = [name], dict(os.environ)
    root = os.environ.get("CLOUDSWEEP_PODMAN_ROOT")
    if name == "podman" and root:
        root = Path(root)
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        argv += ["--root", str(root / "storage"), "--runroot", str(root / "run"),
                 "--storage-opt", "overlay.ignore_chown_errors=true"]
        env["REGISTRY_AUTH_FILE"] = str(root / "auth.json")
    if name == "docker":
        env.setdefault("BUILDX_NO_DEFAULT_ATTESTATIONS", "1")  # push a plain image, not an index
    kwargs.setdefault("check", True)
    return subprocess.run(argv + list(args), env=env, universal_newlines=True, **kwargs)


def image_id(image):
    """The local image ID as sha256:<hex>."""
    found = engine("image", "inspect", "--format", "{{.Id}}", image, stdout=subprocess.PIPE).stdout.strip()
    return found if found.startswith("sha256:") else "sha256:" + found


def build(context, tag, dockerfile=None, platform=None):
    args = ["build", "-t", tag]
    if engine_name() == "podman":
        args.append("--pull=missing")
    if dockerfile:
        args += ["-f", str(dockerfile)]
    if platform:
        args += ["--platform", platform]
    engine(*(args + [str(context)]), stdout=sys.stderr)
    return image_id(tag)


def build_base(config, name, tag):
    """Build the project's toolchain image, then the worker layer on top; returns its ID."""
    toolchain = "localhost/%s-toolchain:candidate" % name
    build(config["context"], toolchain, config["dockerfile"], config["platform"])
    with tempfile.TemporaryDirectory() as context:
        for file in WORKER_FILES:
            shutil.copy2(str(HERE / file), context)
        (Path(context) / "Dockerfile").write_text(WORKER_LAYER.format(toolchain=toolchain, work=core.WORK))
        return build(context, tag, platform=config["platform"])


def checkout(repo, commit, destination):
    """A clean, shallow checkout of commit with its .git: no remote, no reflog, no host paths."""
    destination.mkdir()
    for args in (("init", "--quiet"), ("fetch", "--quiet", "--depth", "1", str(repo), commit),
                 ("-c", "advice.detachedHead=false", "checkout", "--quiet", "--detach", "FETCH_HEAD")):
        subprocess.run(["git", "-C", str(destination)] + list(args), check=True, stdout=subprocess.DEVNULL)
    if subprocess.run(["git", "-C", str(destination), "submodule", "status"], stdout=subprocess.PIPE,
                      universal_newlines=True).stdout.strip():
        raise Refusal("the commit uses Git submodules, which cloudsweep does not check out; vendor them")
    shutil.rmtree(str(destination / ".git/logs"), ignore_errors=True)
    (destination / ".git/FETCH_HEAD").unlink()


def build_revision(repo, commit, base, tag, recipe, platform=None):
    """Build the revision image for commit FROM base (an image reference); returns its local ID."""
    with tempfile.TemporaryDirectory() as context:
        checkout(repo, commit, Path(context) / "src")
        # Jobs run unprivileged and place inputs inside the checkout; Git ignores these modes.
        subprocess.run(["chmod", "-R", "a+rwX", str(Path(context) / "src")], check=True)
        (Path(context) / core.RECIPE).write_bytes(core.pretty(recipe))
        (Path(context) / "Dockerfile").write_text(REVISION.format(base=base, work=core.WORK, recipe=core.RECIPE))
        return build(context, tag, platform=platform)


def local_flags():
    """Unprivileged and without network, as the host user (so bind mounts stay writable)."""
    argv = ["run", "--rm", "--network", "none", "--user", "%d:%d" % (os.getuid(), os.getgid())]
    if engine_name() == "podman":
        # keep-id maps the host user to itself; the ping range names a mapped group so this
        # works on hosts without subordinate IDs.
        argv += ["--userns=keep-id", "--sysctl", "net.ipv4.ping_group_range=%d %d" % (os.getgid(), os.getgid()),
                 "--security-opt", "label=disable"]
    return argv


def run_local(image, args, mounts=(), environment=(), stdout=None):
    """Run the worker image like Batch would: unprivileged, no network."""
    argv = local_flags()
    for source, target in mounts:
        argv += ["-v", "%s:%s" % (source, target)]
    for name, value in environment:
        argv += ["-e", "%s=%s" % (name, value)]
    return engine(*(argv + [image] + list(args)), check=False, stdout=stdout).returncode


def run_shell(image, command, stdout=None):
    """Run a shell command inside the image (the project's toolchain check)."""
    return engine(*(local_flags() + ["--entrypoint", "/bin/sh", image, "-c", command]),
                  check=False, stdout=stdout).returncode


def login(aws, repository):
    password = aws("ecr", "get-login-password", parse=False)
    engine("login", "--username", "AWS", "--password-stdin", repository.split("/", 1)[0],
           input=password, stdout=subprocess.DEVNULL)


def push(local, target):
    """Push a local image to target (repository:tag); returns the registry digest."""
    engine("tag", local, target)
    if engine_name() == "podman":
        with tempfile.TemporaryDirectory() as scratch:
            digest_file = Path(scratch) / "digest"
            engine("push", "--digestfile", str(digest_file), target, stdout=sys.stderr)
            digest = digest_file.read_text().strip()
    else:
        output = engine("push", target, stdout=subprocess.PIPE).stdout
        sys.stderr.write(output)
        found = re.findall(r"digest: (sha256:[0-9a-f]{64})", output)
        digest = found[-1] if found else ""
    core.require(core.DIGEST_RE.fullmatch(digest), "push returned no digest")
    return digest


def described(aws, repository, *args):
    return aws("ecr", "describe-images", "--repository-name", repository.rsplit("/", 1)[1], *args)


def registry_digest(aws, repository, tag):
    """The digest the registry holds under tag, or None."""
    try:
        return described(aws, repository, "--image-ids", "imageTag=" + tag)["imageDetails"][0]["imageDigest"]
    except settings.AwsError as error:
        if error.code == "ImageNotFoundException":
            return None
        raise


def newest_base(aws, repository):
    """The most recently published base image."""
    bases = [item for item in described(aws, repository, "--filter", "tagStatus=TAGGED").get("imageDetails", [])
             if any(tag.startswith(BASE_TAG) for tag in item.get("imageTags", []))]
    if not bases:
        raise Refusal("no published base image in %s: run `cloudsweep image build` and "
                      "`cloudsweep image publish` first" % repository)
    return max(bases, key=lambda item: _pushed(item["imagePushedAt"]))["imageDigest"]


def _pushed(value):
    """A push time (epoch seconds or ISO 8601, as the CLI prints it) as epoch seconds."""
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).replace("Z", "+00:00")
    return datetime.fromisoformat(text).timestamp()


def revision_tag(commit, base_digest, recipe):
    return "%s%s-%s" % (REVISION_TAG, commit, core.build_key(commit, base_digest, recipe)[:16])


def run_definition(aws, settings_values, digest):
    """The control function pins the digest in the stack's template, reusing a matching revision."""
    return settings.invoke_lambda(aws, settings_values["CONTROL_FUNCTION"],
                                  {"image_digest": digest})["jobDefinitionArn"]
