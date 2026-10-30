# Adapting your project

From your Git checkout, generate the configuration and toolchain Dockerfile:

```bash
cloudsweep init --build "make -j" --binary build/mysim
```

Edit the generated files for your program, then commit them and the updated `.gitignore`.
The [cache simulator](../examples/cache-sim/) is a complete example.

## Configuration

Build and run settings come from `cloudsweep.ini` in the commit being run. `[image]` and
`[acceptance]` come from your working tree. A committed recipe fixes the build command;
reproducible rebuilds also need pinned toolchain dependencies.

```ini
[project]
name = cachesim              # 1-20 lowercase letters, digits or hyphens; starts with a letter

[image]
dockerfile = cloud/Dockerfile
# context = cloud            # default: the Dockerfile's directory
# check = gcc --version      # optional command inside the base image
# platform = linux/amd64     # default: deployment architecture, else engine default

[build]
command = make -j            # /bin/sh, as root, in a clean checkout at image build time
binary = build/cachesim      # {bin}; omit if commands name their program directly

[run]                        # submit defaults; try also uses hours
vcpus = 1
memory_mib = 3500
hours = 1

[acceptance]                 # optional
command = ["{bin}", "--accesses", "200000", "--seed", "7", "--out", "{out}"]
check = cmp {out}/result.json tests/reference.json
# inputs = traces/fixture.txt  # optional files, as for --input
```

The project name names AWS resources and the cost tag. Commands run in the checkout;
[commands and inputs](running.md#commands-and-inputs) defines placeholders and external files.

## Toolchain image

```dockerfile
FROM docker.io/library/debian:bookworm-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends gcc libc6-dev make git python3 ca-certificates \
 && rm -rf /var/lib/apt/lists/*
```

Use a Linux base with a shell and standard Unix tools. Install your compiler, libraries and
packages, plus `python3` 3.8+, `git` and CA certificates. `image build` checks those worker
requirements and adds the worker under `/opt/cloudsweep`, writable `/work`, and UID 1000.

Do not copy project source into the base image; cloudsweep adds the selected commit in a
[revision image](how-it-works.md#revision-images). Build products may go anywhere in the checkout,
including changes to tracked files: the worker records and checks the resulting state.

```bash
cloudsweep image build
cloudsweep image publish
```

Rebuild and publish when your toolchain changes. Submissions use the newest published base image.
For reproducible rebuilds, pin the base by digest (`FROM debian@sha256:...`) and package versions,
or use a dated package mirror. An unpinned install can change the compiler.

## Acceptance runs

When `[acceptance]` is configured, `image build` runs its command through the worker, then runs
`check` in your checkout on your machine. `{out}` names the downloaded output directory. Both the
run and the check must succeed; `image publish` requires a passing result for that base image.
Without `[acceptance]`, publication requires only the worker and toolchain checks.

Use a deterministic run lasting seconds to a minute. `cmp` against a committed reference is
sufficient; use a script to ignore variable fields such as timings. Acceptance results are stored
under `.cloudsweep/acceptance/`, keyed by base image ID.

## Local container settings

Set `CLOUDSWEEP_ENGINE=docker` or `podman` to choose an engine; Podman is preferred when installed.
With a network home directory, set `CLOUDSWEEP_PODMAN_ROOT` to local-disk storage.

Rootless Podman without subordinate user IDs can make `apt-get` fail with
`setgroups ... Operation not permitted`. Before `apt-get update`, add this line (also safe on
other hosts):

```dockerfile
RUN echo 'APT::Sandbox::User "root";' > /etc/apt/apt.conf.d/99sandbox
```
