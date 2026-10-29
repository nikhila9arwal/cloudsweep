# Adapting your project

To adapt a project you add two files: `cloudsweep.ini`, which says how to build and run your code,
and a Dockerfile, which says what toolchain it needs. `cloudsweep init` writes both, with comments
in them.

## cloudsweep.ini is read from the commit being run

Every revision image is built with the `cloudsweep.ini` committed in that revision. A change to the
build command therefore takes effect when you commit it, and an old commit always rebuilds the same
way. The `[image]` and `[acceptance]` sections are read from your working tree, because they
concern the base image, not a particular commit.

```ini
[project]
name = cachesim              # names every AWS resource and the cost tag: 1-20 of a-z 0-9 -

[image]
dockerfile = cloud/Dockerfile
# context = cloud            # default: the Dockerfile's directory
# check = g++ --version      # optional toolchain check, run inside the image
# platform = linux/amd64     # default: the deployment's architecture

[build]
command = make -j            # run with /bin/sh in a clean checkout, as root, at image build time
binary = build/cachesim      # what {bin} means; leave empty if commands name the program

[run]                        # defaults for submit and try
vcpus = 1
memory_mib = 3500
hours = 1

[acceptance]                 # optional; see below
command = ["{bin}", "--accesses", "200000", "--seed", "7", "--out", "{out}"]
inputs = tests/fixture.txt
check = cmp {out}/result.json tests/reference.json
```

## The Dockerfile holds your toolchain and nothing of cloudsweep's

Write the Dockerfile you would use to build and run your simulator: compilers, libraries, Python
packages, anything else. It must also install `python3` (3.8 or newer), `git` and CA certificates
(`ca-certificates` on Debian and Ubuntu), which the worker uses to reach S3 over HTTPS. `image
build` checks all three. `cloudsweep image build` builds your Dockerfile, then adds a small layer on top: the worker in
`/opt/cloudsweep`, a writable `/work`, and an unprivileged user with UID 1000. Any base
distribution works.

```dockerfile
FROM docker.io/library/debian:bookworm-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends gcc libc6-dev make git python3 ca-certificates \
 && rm -rf /var/lib/apt/lists/*
```

Do not copy your source code into this image. Each commit is added on top of it later. Rebuild and
publish the base image only when the toolchain changes. Every submission is built from the newest
published base image.

If results must be the same after a rebuild, pin everything that can change: the base image by
digest (`FROM debian@sha256:...`), and package versions, or a dated snapshot mirror. An unpinned
`apt-get install` can give a different compiler next month.

**Rootless Podman without subordinate user IDs.** On such hosts (common on shared clusters),
`apt-get` fails with `setgroups ... Operation not permitted`. Put this line before
`apt-get update`; it is harmless elsewhere:

```dockerfile
RUN echo 'APT::Sandbox::User "root";' > /etc/apt/apt.conf.d/99sandbox
```

On such hosts, also set `CLOUDSWEEP_PODMAN_ROOT` to a directory on local disk (for example
`/var/tmp/$USER-podman`) if your home directory is on a network file system.

## The build runs once per commit, in a clean checkout

When you submit a commit for the first time, cloudsweep:

1. makes a shallow clean checkout of that commit, with its `.git` directory, at `/work/src`
   (uncommitted and untracked files are never included);
2. runs `[build] command` there, as root, inside the newest published base image;
3. records the commit, the SHA-256 of `[build] binary`, and the state of the tracked files;
4. pushes the result as a revision image tagged with the commit and a hash of the base image and
   the recipe.

Later submissions of the same commit reuse that image. At run time, the worker checks that the
checkout and the binary still match the record before it runs anything. Your program can read its
own Git identity (`git rev-parse HEAD`) in `/work/src`.

Build products may be written anywhere inside the checkout. A build that changes tracked files is
allowed: the changed state is recorded, and runs check against it.

## Commands, placeholders and inputs

A command is a list of arguments. No shell is involved: write `["sh", "-c", "..."]` if you need
one. The working directory is the checkout, so relative paths in your repository work as they do
locally.

| Placeholder | Becomes |
|---|---|
| `{bin}` | `/work/src/<[build] binary>` |
| `{out}` | the attempt's output directory. Everything written here is uploaded |
| `{src}` | `/work/src`, the checkout |
| `{index}` | the command's line number in the sweep, from 0. Useful as a seed |
| `{input:NAME}` | the path of the input file named NAME |
| `{{`, `}}` | literal braces |

Inputs are files that are not in the repository: traces, checkpoints, large data. Pass each with
`--input PATH`. It is uploaded once (by content hash; identical files are never uploaded twice)
and placed at the same repository-relative path in the checkout, so `--input traces/a.txt`
appears at `/work/src/traces/a.txt`. Use `PATH=NAME` to place a file from outside the checkout. An
input may not replace a file that the commit tracks.

Standard output and standard error are saved as `stdout.txt` and `stderr.txt`. A command succeeds
when it exits with status 0.

## An acceptance run checks a new base image before anything runs on it

A new toolchain image can change results: a different compiler, a different libm. If you add an
`[acceptance]` section, `cloudsweep image build` runs that command in the new image, through the
same worker that runs on AWS. It then runs `check` on your machine, with `{out}` replaced by the
run's output directory. `check` is any shell command that exits with status 0 when the output is
right; `cmp` against a committed reference file is the simplest. `cloudsweep image publish`
refuses an image without a passing acceptance result.

Choose a short run (seconds to a minute) whose output is deterministic. If your outputs contain
timings or host names, make `check` a small script that compares only the deterministic fields.
