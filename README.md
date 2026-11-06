# cloudsweep

[![CI](https://github.com/nikhila9arwal/cloudsweep/actions/workflows/ci.yml/badge.svg)](https://github.com/nikhila9arwal/cloudsweep/actions/workflows/ci.yml)

cloudsweep runs a sweep of commands from a Git commit on this host, a machine you reach over ssh, a
Slurm cluster, or AWS Batch, and brings back verified results. On your machines it builds and runs
the commit in the machine's own environment, on CPUs or GPUs. On AWS each command runs in a
container built from your toolchain Dockerfile.

```bash
cloudsweep submit --commands sweep.jsonl                      # this host
cloudsweep submit --on deltaai --commands sweep.jsonl --gpus 1   # a Slurm cluster
cloudsweep submit --on aws --commands sweep.jsonl             # AWS Batch
cloudsweep fetch <submission> --wait                          # verified results, from any of them
```

## Install

You need Python 3.9+, Git and rsync. Running on this host, the default, needs Linux; from macOS you
can run on other machines and AWS. Each machine you run on needs bash, git, rsync and Python 3.6+.

To run commands on a machine in your toolchain image instead (`container = apptainer`), the machine
needs Apptainer. The image is built here when you have Podman or Docker (with QEMU emulation for a
machine of another architecture, such as aarch64 DeltaAI from an x86 host:
`sudo apt install qemu-user-static`), and converted here when you also have Apptainer; otherwise the
machine converts or builds it.

```bash
git clone https://github.com/nikhila9arwal/cloudsweep
python3 -m venv .venv
. .venv/bin/activate
pip install ./cloudsweep
```

## Run your project

Start in your project's Git repository, with your work committed. Replace the build command,
program path and arguments with your own. For a script that needs no build, run `cloudsweep init`
without them and submit `-- python3 simulate.py --out {out}` instead.

```bash
cloudsweep init --build "make -j" --binary build/mysim    # writes cloudsweep.ini
git add cloudsweep.ini .gitignore
git commit -m "Run with cloudsweep"
cloudsweep submit -- {bin} --seed 1 --out {out}   # runs on this host; prints a submission ID
cloudsweep fetch <submission> --wait
```

`{bin}` is your built program and `{out}` the directory it writes results to. The results are in
`cloudsweep-results/<submission>/0/1/`: `out/` holds what the program wrote to `{out}`, and
`stdout.txt` and `stderr.txt` its output. Files written anywhere else are not kept. Use results only
when `fetch` reports **`complete`**; otherwise it names each command that failed. The checkout
comes from the commit (uncommitted changes are refused), and every result records that commit and
the hashes of its files.

## Make a sweep

A sweep file holds one command per line, as a JSON argument list:

```bash
cat > sweep.jsonl <<'EOF'
["{bin}", "--seed", "1", "--out", "{out}"]
["{bin}", "--seed", "2", "--out", "{out}"]
EOF
cloudsweep submit --commands sweep.jsonl
```

Write large sweeps with a short script, like the example's
[make_sweep.py](examples/cache-sim/configs/make_sweep.py). `{index}` is the command's number, and
`--input data/trace.bin` sends a file the commands name as `{input:data/trace.bin}`. Programs run as
argument lists, without a shell: use `["sh", "-c", "..."]` for shell syntax, and
`["python3", "simulate.py", "--out", "{out}"]` for a script. See
[commands and inputs](docs/running.md#commands-and-inputs). The
[cache simulator example](examples/cache-sim/) is a complete project with a 24-command sweep.

## Run on other machines

Describe each machine once in `~/.config/cloudsweep/machines.ini`: a name, which is also its ssh
alias, and its kind. A `slurm` machine gets one `sbatch --array` job per sweep, and cloudsweep writes
the batch script. An `ssh` machine runs a pool of slots: by default one per CPU (one per `[run]
vcpus` CPUs), or one per GPU set for GPU commands. The partitions and account below are examples;
use your own.

```ini
[pembroke2]
kind = ssh

[ece-gpu]
kind = slurm
cpu_sbatch = --partition=defq
gpu_sbatch = --partition=HGPU
gpu_type = h100

[deltaai]
kind = slurm
sbatch = --account=bbxx-dtai-gh --partition=ghx4
```

```bash
cloudsweep machines                   # lists them, with your note on what each is for (use =)
cloudsweep doctor --on ece-gpu        # checks what the machine provides
cloudsweep submit --on ece-gpu --commands sweep.jsonl
cloudsweep submit --on ece-gpu --commands sweep.jsonl --gpus 1
```

Commands use the machine's own compilers, modules and Python; `setup` lines load them (a module, or
`source ~/venvs/sim/bin/activate`). To run in your toolchain image instead, as on AWS, add
`container = apptainer`: cloudsweep builds the image on this host and sends it. Or name an image
already on the machine, such as `container = ~/mirage.sif` ([containers](docs/machines.md#containers)).
Set `[run] gpus = 1` in `cloudsweep.ini` to make every run of a project ask for a GPU. On a machine that needs a password and Duo, such as DeltaAI, you sign in once
and cloudsweep reuses that connection; [your own machines](docs/machines.md) shows the ssh settings
and every option.

## Run on AWS

Running many jobs on AWS normally means setting up a cluster or login node, a job queue, a container
registry, storage and permissions, then logging in to manage them. `cloudsweep deploy` creates all
of that in one step with your administrator profile. After that, you run `submit`, `status`, `fetch`
and `cancel` from your own machine under a narrower identity that deploy created, with no console or
server to log in to. By default that identity borrows your administrator profile's credentials, so
they must stay valid; with `--identity roles-anywhere` (x86_64 Linux) it uses a certificate that
lasts a year instead.

Instances run only while there is work, and Batch scales them down after it finishes. A guard checks
every five minutes. It stops all work when the month's reported spending reaches your cutoff, and it
stops jobs stuck starting for 30 minutes. It emails you at two spending thresholds, when jobs have
waited three hours with nothing starting or running, and when instances stay up 30 minutes after the
work has ended. Confirm the subscription email AWS sends you to receive these.

AWS also needs Docker or Podman and the
[AWS CLI v2](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html). Edit the
`cloud/Dockerfile` that `init` wrote to install your toolchain and commit it. These steps create
**billable resources**; see [deployment](docs/admin.md#deploy) and
[spending limits](docs/admin.md#costs-and-limits).

```bash
cloudsweep image build                                # builds and checks the toolchain image
cloudsweep try --commands sweep.jsonl                 # runs the sweep locally in that image
cloudsweep deploy --admin-profile <admin profile> --email you@example.org
cloudsweep image publish
cloudsweep submit --on aws --dry-run --commands sweep.jsonl    # prints the cost estimate
cloudsweep submit --on aws --commands sweep.jsonl
cloudsweep fetch <submission> --wait
```

`cloudsweep status --on aws` shows the queue and the month's reported spending. `cloudsweep admin retire --admin-profile
<admin profile> --yes` removes the queue, compute and guard; the bucket and images stay, and are
billed, until you add `--delete-data` (see [retire](docs/admin.md#retire)).

## Agents

cloudsweep is designed to be run by AI coding agents as well as people. Submitting, watching,
fetching and cancelling need no administrator rights, and the identity they use on AWS cannot change
the budget, the spending cutoff or permissions (it does not limit other credentials on your host). When cloudsweep refuses something (cloud use is off, the cutoff
is active, a run would cost more than the per-run ceiling), it says why and what to do next. If it
cannot tell whether a submission went through, it records a receipt and never resubmits on its own.
`cloudsweep machines` shows an agent every machine you described and your note on what each is
for. [For AI agents](docs/agents.md) has instructions to paste into your project.

## When something fails

- `cloudsweep doctor` checks the project and this host; add `--on MACHINE` for a machine, or
  `--on aws` for the AWS deployment.
- `cloudsweep status <submission>` counts commands by state, and `fetch` names each one that
  failed; `cloudsweep cancel <submission>` stops the work.
- Each attempt's `stderr.txt` is in `cloudsweep-results/<submission>/<command>/<attempt>/` after
  `fetch`. On a machine, the job's logs are in `<workdir>/jobs/<submission>/logs/` there
  (`workdir` is `~/cloudsweep` unless you set it).
- cloudsweep keeps receipts in your project's `.cloudsweep/` (untracked) and, on each machine,
  builds and results in its `workdir` (`~/cloudsweep`); remove old ones there by hand.

## Limits

- Independent jobs only, with no multi-node MPI. AWS runs CPU jobs on On-Demand EC2: no GPUs or
  Spot.
- Programs must run on Linux, on the target's architecture: x86_64 or arm64.
- Git submodules are refused; vendor their code into the repository.
- Symbolic links among the outputs are not kept. On AWS, output files over 5 GiB are skipped;
  `fetch` reports both.
- AWS spending controls have gaps; the monthly cutoff is not a hard bill limit. See
  [costs and limits](docs/admin.md#costs-and-limits).

## Documentation

- [Adapting your project](docs/project.md): `cloudsweep.ini`, the Dockerfile, acceptance runs.
- [Running work](docs/running.md): commands, inputs, sizing, results, cancellation.
- [Your own machines](docs/machines.md): this host, ssh machines, Slurm clusters, GPUs, Duo.
- [Administration](docs/admin.md): AWS deployment, costs, identities, cleanup.
- [How it works](docs/how-it-works.md): lifecycle, verification, permissions.
- [For AI agents](docs/agents.md): instructions to copy into your project.

The [changelog](CHANGELOG.md) records tested and untested configurations. See
[Contributing](CONTRIBUTING.md) and [Security](SECURITY.md) for fixes and vulnerability reports.
Licensed under [Apache-2.0](LICENSE).
