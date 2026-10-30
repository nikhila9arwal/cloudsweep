# cloudsweep

[![CI](https://github.com/nikhila9arwal/cloudsweep/actions/workflows/ci.yml/badge.svg)](https://github.com/nikhila9arwal/cloudsweep/actions/workflows/ci.yml)

cloudsweep runs a simulator's parameter sweeps on AWS Batch, straight from a Git project. You
commit your code and run one command. Each point of the sweep then runs in a clean container
built from that commit, and you get back verified result files. The AWS machines start only while
work is waiting and stop when the queue is empty. A monthly budget cutoff stops all work if
spending goes past a limit you set.

It works for any program that runs from the command line on Linux, reads files and writes files:
cycle-level and functional simulators, network simulators, solvers, Monte Carlo codes, scripts.
Your toolchain goes in your own Dockerfile. cloudsweep needs only `python3` (3.8+), `git` and CA
certificates inside that image.

```
your Git repo ──commit──▶ revision image ──▶ AWS Batch queue ──▶ one container per command
  cloudsweep.ini            (your toolchain     (scales 0 → cap      │ inputs from S3
  cloud/Dockerfile           + this commit,      → 0)                │ outputs, logs, manifest to S3
  sweep.jsonl                built once)                             ▼
                                                     cloudsweep fetch: download, check every hash
```

## What you need

- A Linux or macOS machine with Python 3.9+, Git, the
  [AWS CLI v2](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html),
  and Docker or Podman.
- An account whose Lambda concurrency quota is at least 1,000. This is the default, but new accounts
  sometimes start at 10. `deploy` checks the quota and says how to raise it.
- An AWS account, and an AWS CLI profile with administrator rights in it. You need these rights
  only for `cloudsweep deploy` and the other `admin` commands. Day-to-day commands use a narrower
  agent profile that deploy creates.
- Your simulator in a Git repository.

Install it:

```bash
pip install git+https://github.com/nikhila9arwal/cloudsweep
```

Or clone the repository and put its `bin/` directory on your `PATH`; nothing else is needed.

## Quick start

Run these from your project's checkout. The [example project](examples/cache-sim/) goes through
the same steps with a toy cache simulator.

```bash
cloudsweep init --build "make -j" --binary build/mysim   # writes cloudsweep.ini and cloud/Dockerfile
# edit cloud/Dockerfile to install your toolchain, then:
git add cloudsweep.ini cloud/Dockerfile .gitignore && git commit -m "Add cloudsweep"
cloudsweep deploy --admin-profile <admin profile> --email you@example.org   # once, about 5 minutes
cloudsweep image build      # your toolchain + the worker, checked locally
cloudsweep image publish    # pushed to your deployment's registry
cloudsweep try -- {bin} --quick-test --out {out}       # runs locally in that container, free
cloudsweep submit --hours 2 --commands sweep.jsonl     # runs on AWS
cloudsweep status
cloudsweep fetch <submission> --wait                   # waits, downloads, verifies
```

A sweep is a text file with one command per line, each a JSON list of arguments:

```json
["{bin}", "--sets", "64", "--ways", "4", "--trace", "{input:traces/a.txt}", "--out", "{out}"]
["{bin}", "--sets", "64", "--ways", "8", "--trace", "{input:traces/a.txt}", "--out", "{out}"]
```

`{bin}` is your built program. `{out}` is a directory: everything the command writes there comes
back. `{input:...}` is a file you pass with `--input`. Run `cloudsweep doctor` whenever something
does not work. It checks each prerequisite in order and says what to fix.

## What it costs

You pay On-Demand EC2 prices for the instances that run your commands, plus small amounts for
storage, logs and the guard. Example prices for the default instance types, read from the AWS Price
List API for us-east-1 on 2026-10-29: m7a.large (2 vCPUs, 8 GiB) costs $0.11592 per hour, and
m7a.4xlarge (16 vCPUs, 64 GiB) costs $0.92736 per hour. `deploy` reads the current prices for your
region and instance types, and every cost figure cloudsweep prints uses them.

Three limits bound spending. You set each one:

| Limit | Default | What it does |
|---|---|---|
| vCPU cap | 32 vCPUs | Batch never runs more than this, plus at most one extra instance |
| Per-run ceiling | $10 | `submit` refuses a command whose worst-case cost is above this. Worst case: the command alone on the most expensive deployed instance type that fits it, for its full time limit |
| Monthly cutoff | $100 (emails at $50 and $80) | When AWS Budgets reports spend at the cutoff, the guard stops all jobs and blocks new ones until an administrator lifts it |

AWS billing data arrive hours after usage, so the final bill can exceed the cutoff. The cutoff
counts only resources tagged `Project=<name>`, and only after that cost-allocation tag is active.
[Administration](docs/admin.md#the-budget-counts-only-an-active-cost-allocation-tag) explains how
the tag gets activated.

## Documentation

- [Adapting your project](docs/project.md): `cloudsweep.ini`, the Dockerfile, build commands,
  inputs, and acceptance runs.
- [Running work](docs/running.md): `try`, `submit`, `status`, `fetch`, `cancel`; sweep files,
  placeholders, sizing, and the layout of the results.
- [Administration](docs/admin.md): deploying, budgets, identities, lifting the cutoff, removing
  everything.
- [How it works](docs/how-it-works.md): what runs where, what is verified, the security model, and
  the limits.
- [For AI agents](docs/agents.md): rules for an agent that runs cloudsweep, and a block to paste
  into your project's agent instructions.

## Limits

- CPU only, On-Demand instances only. GPUs and Spot capacity are not supported.
- Each output file is uploaded in a single request, so files over 5 GiB are skipped. The skip is
  recorded and `fetch` reports it.
- Commits that use Git submodules are refused. Vendor the submodule's code into the repository.
- One container runs per command. The containers cannot talk to each other (no MPI across
  instances).
- The program inside the image must run on Linux for the deployment's CPU architecture: x86_64,
  or arm64 if you deploy Graviton instance types.

## Status

Version 0.1.0 is new. It has been tested end to end on AWS in one configuration (us-east-1, m7a
instances, the assume-role identity); see the [changelog](CHANGELOG.md) for what has and has not
been exercised. Read [How it works](docs/how-it-works.md) before you deploy it into an account
that holds other work.

## Contributing and license

See [CONTRIBUTING.md](CONTRIBUTING.md) for the development setup and tests, and
[SECURITY.md](SECURITY.md) to report a vulnerability. cloudsweep is released under the
[MIT License](LICENSE).
