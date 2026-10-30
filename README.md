# cloudsweep

[![CI](https://github.com/nikhila9arwal/cloudsweep/actions/workflows/ci.yml/badge.svg)](https://github.com/nikhila9arwal/cloudsweep/actions/workflows/ci.yml)

cloudsweep runs parameter sweeps on AWS Batch from a Git project. Each command runs in a container
built from your commit; `fetch` downloads and verifies the results. Use it with Linux
command-line programs that read and write files.

```text
Git commit + toolchain → revision image → AWS Batch → one container per command
                                         ↑ inputs from S3    ↓ outputs to S3
                                         verified results ← cloudsweep fetch
```

## Install

You need Linux or macOS, Python 3.9+, Git, Docker or Podman, and the
[AWS CLI v2](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html).
For AWS runs, configure an administrator profile; `deploy` creates a narrower agent profile for
routine commands. Read the [permissions](docs/how-it-works.md#permissions) before using a shared account.

```bash
git clone https://github.com/nikhila9arwal/cloudsweep
python3 -m venv .venv
. .venv/bin/activate
pip install ./cloudsweep
```

Alternatively, add the cloned repository's `bin/` directory to `PATH`.

## Quick start

From the directory where you cloned cloudsweep, copy the [cache simulator](examples/cache-sim/)
into its own Git repository:

```bash
cp -R cloudsweep/examples/cache-sim cache-sim
cd cache-sim
git init
git add .
git commit -m "Add cache simulator"
mkdir -p traces
printf '6000\n' > traces/footprint.txt
python3 configs/make_sweep.py             # writes sweep.jsonl
cloudsweep image build                   # builds and checks the toolchain image
cloudsweep try --commands sweep.jsonl --input traces/footprint.txt
```

Then run it on AWS. These steps create **billable resources**; see
[deployment prerequisites](docs/admin.md#deploy) and [spending limits](docs/admin.md#costs-and-limits).

```bash
cloudsweep deploy --admin-profile <admin profile> --email you@example.org
cloudsweep image publish
cloudsweep submit --commands sweep.jsonl --input traces/footprint.txt   # prints a submission ID
cloudsweep fetch <submission> --wait
```

Use results only when `fetch` reports **`complete`**. Resources retained after jobs finish still
cost money; see [cleanup](docs/admin.md#retire). Use `cloudsweep doctor` to diagnose failures.

A sweep file contains one JSON argument list per line, for example:

```json
["{bin}", "--sets", "64", "--ways", "4", "--trace", "{input:traces/footprint.txt}", "--out", "{out}"]
["{bin}", "--sets", "64", "--ways", "8", "--trace", "{input:traces/footprint.txt}", "--out", "{out}"]
```

`{bin}` is your program, `{out}` its output directory, and `{input:...}` a file passed with
`--input`. See [commands and inputs](docs/running.md#commands-and-inputs) for the full syntax.

## Your own project

```bash
cd your-simulator
cloudsweep init --build "make -j" --binary build/mysim   # writes cloudsweep.ini and cloud/Dockerfile
```

Edit `cloud/Dockerfile` to install your toolchain, commit both files, and continue from
`cloudsweep image build` as above. [Adapting your project](docs/project.md) covers the options.

## Limits

- Independent CPU jobs on On-Demand EC2 only; no GPUs, Spot, or multi-node MPI jobs.
- Programs must run on Linux for the deployment's architecture: x86_64 or arm64.
- Git submodules are refused; vendor their code into the repository.
- Output uploads use single requests. Files over 5 GiB and symbolic links are skipped;
  `fetch` reports the omissions.
- Spending controls have gaps; the monthly cutoff is not a hard bill limit. See
  [costs and limits](docs/admin.md#costs-and-limits).

## Documentation

- [Adapting your project](docs/project.md): configuration, Dockerfile, acceptance runs.
- [Running work](docs/running.md): commands, sizing, results, cancellation.
- [Administration](docs/admin.md): deployment, costs, identities, cleanup.
- [How it works](docs/how-it-works.md): lifecycle, verification, permissions.
- [For AI agents](docs/agents.md): instructions to copy into your project.

Version 0.1.0 is new; the [changelog](CHANGELOG.md) records tested and untested configurations.
See [Contributing](CONTRIBUTING.md) and [Security](SECURITY.md) for fixes and vulnerability reports.
Licensed under [Apache-2.0](LICENSE).
