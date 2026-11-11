# Your own machines

Run a sweep on this host, on a machine you reach over ssh, or on a Slurm cluster, with the same
commands as on AWS. Each command runs in the machine's own environment: its compilers, modules,
Python and GPUs. No container is needed.

```bash
cloudsweep machines                                            # where you can run, and what each is for
cloudsweep submit --commands sweep.jsonl                       # this host (local); needs no settings
cloudsweep submit --on deltaai --commands sweep.jsonl --gpus 1
cloudsweep status <submission>
cloudsweep fetch <submission> --wait
cloudsweep cancel <submission>
```

`status`, `cancel` and `fetch` find the machine in the submission's receipt; `--on MACHINE` names
it explicitly. Without `--on`, `submit` runs on this host, and `--on aws` sends the sweep to the
project's AWS deployment. `fetch` gives the same verified `cloudsweep-results/<submission>/` as from AWS: use
results only when it reports **`complete`**.

## Describe a machine

Put machines you use in every project in `~/.config/cloudsweep/machines.ini`. A project's own
`.cloudsweep/machines.ini` (untracked) adds sections or overrides them. The section name is what
`--on` takes, and also the ssh alias, so your `~/.ssh/config` decides how to reach the machine.

First check that the alias works, for example `ssh ece-gpu hostname`. If you usually connect as
`user@host`, set `host = user@host` in the section instead (for a cluster, its login node). Then:

```bash
mkdir -p ~/.config/cloudsweep
$EDITOR ~/.config/cloudsweep/machines.ini
cloudsweep doctor --on pembroke2
```

`--machines FILE`, on `submit` and every other command that reads machine settings, puts one more
file first, so a set of sections for one task needs neither an edit to your files nor a moved
`XDG_CONFIG_HOME` (which also moves git's settings). A submission's receipt records the file and the
settings it was made with (`machines_file`, `section`); `status`, `cancel`, `fetch`, `wait` and
`race` of that submission use those, as do `status` and `cancel --all` without `--on` for this
host's submissions, so they need no flag and are not affected when the file changes. An explicit `--on MACHINE` uses the files as they are now instead, for example after
repairing a section's `setup` line.

These sections describe the machines this project was tested on. The accounts, partitions, GPU
types and paths are examples: use your own. A `setup` venv must already exist on the machine; leave
`setup` out when the machine's own Python and tools are enough.

```ini
[pembroke2]                 # a machine you ssh into and run on directly
kind = ssh
setup = source ~/venvs/sim/bin/activate

[ece-gpu]                   # CMU ECE's GPU cluster: one partition for CPU work, one for GPUs
kind = slurm
cpu_sbatch = --partition=defq
gpu_sbatch = --partition=HGPU
gpu_type = h100

[deltaai]                   # NCSA DeltaAI: GH200 (aarch64) nodes
kind = slurm
sbatch = --account=bbxx-dtai-gh --partition=ghx4
workdir = /projects/bbxx/me/cloudsweep
```

There are two kinds. **`slurm`** submits one `sbatch --array` job per submission, one task per
command, from the cluster's login node; cloudsweep writes the batch script. **`ssh`** runs a pool
on the machine itself, one command per slot. `local`, the default, is a built-in `ssh` machine for this host
(Linux only) that works without ssh; a `[local]` section changes its settings. `aws` names the AWS
deployment and cannot be a section.

| Key | Kinds | Meaning (default) |
|---|---|---|
| `kind` | both | `slurm` or `ssh` |
| `host` | both | ssh alias or host name, or `local` for this host (the section name) |
| `workdir` | both | directory on the machine for builds, jobs and results; relative paths are under its home directory; on a cluster it must be shared with the compute nodes (`cloudsweep`) |
| `setup` | both | shell lines run first, in your home directory, one per line: module loads, a venv (none) |
| `container` | both | `apptainer` runs commands in your project's toolchain image, or the path of a `.sif` image already on the machine, such as `~/mirage.sif` ([below](#containers)) (none) |
| `use` | both | a note on what the machine is for and when to ask before using it; `cloudsweep machines` shows it (none) |
| `vars` | both | `NAME=VALUE` pairs, comma-separated or one per line, that commands use as `{var:NAME}` ([below](#variables)) (none) |
| `sbatch`, `cpu_sbatch`, `gpu_sbatch` | slurm | extra `sbatch` options for every job, CPU jobs and GPU jobs: account, partition (none) |
| `gpu_type` | slurm | GPU type to request, such as `h100` (any) |
| `slots` | ssh | CPU commands at once (the machine's CPUs ÷ `[run] vcpus`) |
| `gpus` | ssh | how many GPUs to hand out (those in `CUDA_VISIBLE_DEVICES` if set, else `nvidia-smi`'s count) |
| `systemd` | ssh | `yes` starts the pool with `systemd-run --user`, for machines that stop your processes at logout (`no`) |

`cloudsweep doctor --on MACHINE` checks that cloudsweep can reach the machine and finds the commands
it needs and a writable `workdir`; it reports the CPUs of the host it reached and, for an ssh pool,
its GPUs. It does not submit a job, so it cannot check a cluster's compute nodes, accounts or
partitions: those show up at `submit`.

## Variables

One job often needs a path or a name that differs between machines: where a venv or a model lives,
or which GPU to expect. Give each machine its values, and write the command once:

```ini
[ece-gpu]
kind = slurm
vars = venv=/home/me/venvs/serve, gpu=H100

[deltaai]
kind = slurm
vars = venv=/projects/bbxx/me/venv, gpu=GH200
```

```json
["{var:venv}/bin/python", "check.py", "--gpu", "{var:gpu}", "--machine", "{machine}", "--out", "{out}"]
```

`{var:NAME}` and `{machine}` (the section's name) are replaced at submission, for the machine the
submission goes to; the other placeholders stay for the run. A value is used as text: braces in it
are not placeholders, and `{{var:x}}` stays literal. A command that names a variable its machine
does not define is refused. The receipt records the variables used (`vars`), the sha256 of the
commands as written (`template_sha256`) and the commands as resolved (`commands`).

## Machines that need a password and Duo

DeltaAI refuses ssh keys. Let ssh keep one signed-in connection open and share it, with an entry
like this in `~/.ssh/config` (keep the socket on a local disk, not a network home directory):

```sshconfig
Host deltaai
    HostName dtai-login.delta.ncsa.illinois.edu
    User YOUR_NCSA_USER
    ControlMaster auto
    ControlPath /tmp/ssh-%u-%C
    ControlPersist yes
```

Then sign in once with `ssh deltaai` (password and Duo) and log out. `ssh -O check deltaai` confirms
that the connection is open; cloudsweep's ssh and rsync calls reuse it until it drops, and
`ssh -O exit deltaai` closes it.

## CPU and GPU commands

Commands need no GPU unless `[run] gpus` in `cloudsweep.ini`, or `--gpus N`, asks for N per command.
On Slurm, a GPU job adds `gpu_sbatch` and `--gpus-per-node=[gpu_type:]N`, and a CPU job adds
`cpu_sbatch`. In an ssh pool each GPU command gets its own N GPUs through `CUDA_VISIBLE_DEVICES`.
The AWS queue has no GPUs and refuses GPU commands.

## Watching a job on Slurm

```text
$ cloudsweep status 20261111T120000Z-1a2b3c 20261111T130000Z-4d5e6f     # illustrative output
ece-gpu (slurm, ece-gpu:cloudsweep)
  20261111T120000Z-1a2b3c PENDING children {"waiting": 2} (Slurm job 1001: 2 pending (JobHeldUser), no start estimate)
  20261111T130000Z-4d5e6f RUNNING children {"running": 1} (Slurm job 1002: 1 running on node07 since 2026-11-11T13:05:40Z)
    Slurm accounting: 1 RUNNING; elapsed 0:10:00 (2026-11-11T13:05:40Z to now on node07); 0.167 GPU-hours; billing 8 unit(s) per task, 1.333 unit-hours
```

For a job in Slurm's queue, `status` shows each state's tasks: a pending task's reason (`Priority`,
`Resources`, `JobHeldUser`, ...) and Slurm's estimated start, or `no start estimate` when Slurm
gives none (`N/A`, as on clusters without backfill estimates), and a running task's node and start.
Once a task has started, `sacct`'s accounting follows: the tasks' states, their elapsed time, the
GPU-hours (allocated GPUs times elapsed time) and the billing unit-hours (Slurm's billing units
times elapsed time). `fetch` prints the same line and keeps the accounting in `fetch.json`. Once
every task has ended, the record is final and kept in `workdir/jobs/<submission>/accounting.json`.

Times are UTC. Slurm prints the cluster's local time, so cloudsweep runs `squeue` and `sacct` with
`TZ=UTC` and `SLURM_TIME_FORMAT=standard`. `status --json` gives them as `queue` (task, state,
reason, start, nodes; for a pending task its start is the estimate and its nodes those Slurm
expects to use) and `accounting` (`tasks`, one entry per execution of a task, each with state,
start, end, `elapsed_s`, nodes, partition, `gpus`, `billing` and `exit_code`, and their `totals`). A
task Slurm requeued has an entry for each execution (`sacct --duplicates`), so the time an earlier
execution used is counted. A cluster without accounting
still shows the queue, with the accounting reported as unavailable.

## Billing on Slurm

A dry run of a Slurm submission reads the partition's settings with `scontrol` (read only) and
shows what each task bills there, also in `submit --json` and in the receipt (`billing`):

```text
$ cloudsweep submit --on deltaai --dry-run --gpus 1 --vcpus 8 --memory-mib 131072 --commands sweep.jsonl
billing on ghx4: 1154.21 unit(s) per task, the largest term (MAX_TRES) of cpu 111.11, gres/gpu 1000, mem 1154.21 (TRESBillingWeights CPU=13.888888,Mem=9.01726G,GRES/gpu=1000); worth 1.154 GPU(s) at 1000 units per GPU
warning: on ghx4 each task bills 1154.21 units, worth 1.154 GPU(s): its 128 GiB of memory exceed 1 GPU(s)' share (110.9 GiB)
```

The units follow Slurm's rule for the partition's `TRESBillingWeights`: each weight times the amount
requested (memory per MiB, or per unit of the weight's suffix, as `G`), added up, or with
`PriorityFlags=MAX_TRES` the largest of them. A partition without weights bills the allocated CPUs,
which can be more than requested on a node that allocates whole cores. The request is the batch
script's, with the section's `sbatch` options for the partition, `--cpus-per-task` and `--mem`
applied; other options that change it (`--exclusive`, `--mem-per-gpu`, ...) are named as not counted.
A GPU request is warned about when its CPUs or memory bill more than its GPUs. What an allocation
charges per unit is the site's rule, which Slurm does not state; the GPU-hours `status` reports are
counted from what Slurm allocated, not from the charge. For a rule cloudsweep does not apply (a
variant of `MAX_TRES` in `PriorityFlags` other than `MAX_TRES` itself), it reports the units as unknown.

## Containers

By default commands use the machine's own software. Add `container = apptainer` to a machine's
section to run them in your project's toolchain image instead: the one `[image]` in
`cloudsweep.ini` describes for AWS. That pins the compiler and libraries, so a result can match
across machines and AWS.

The image is built where AWS's is: on this host, with podman or Docker, for the machine's
architecture. When the architectures differ, as for aarch64 DeltaAI from an x86 workstation, the
build runs under QEMU emulation. On Ubuntu, `sudo apt install qemu-user-static` provides it;
`qemu-user-binfmt` does not, because containers cannot find the emulator it registers. With
Apptainer installed here, cloudsweep also converts the image to a SIF here; otherwise the machine
converts the archive it receives. Either way it is uploaded once per image. Only when this host has
no podman or Docker, or cannot emulate the machine's architecture, does the machine build the image
itself, with its own podman or Docker. One `[image]` recipe serves every machine with
`container = apptainer`, so it must build for each of their architectures.

To use an image that already exists on the machine instead, give its path, absolute or starting
with `~/` (without spaces): `container = ~/mirage.sif`. cloudsweep mounts its worker into it, so the
image needs only `python3` 3.6 or newer and git; nothing is built or uploaded. Replacing the file
starts new builds, and a queued command refuses to run in a file replaced after its submission.

On the machine, the revision is built inside the image (on a cluster's login node) with the
checkout at `/work/src`, as on AWS, and each command runs with `apptainer exec`: its copy of the
checkout at `/work/src` (`/work` is its home directory), the store at `/store`, and the machine's GPUs
(`--nv`) for GPU commands. The machine needs Apptainer 1.0 or newer (or SingularityCE 3.7 or newer),
on a cluster's compute nodes too. If your home directory here is on a network filesystem, set
`CLOUDSWEEP_PODMAN_ROOT` to a local directory for this host's podman; a machine that builds the image
uses its own temporary directory.

What a container sees does not depend on where it was started. cloudsweep starts it with
`--no-mount cwd --pwd /work`, so the directory it starts from is not mounted; without that,
Apptainer mounts it, which is `workdir` in a job and often the home directory in a test by hand.
A build or a command sees only:

- `/work`, its scratch directory and home, with the checkout at `/work/src`;
- `/store`, for a command only, holding what its worker uses: the inputs and its own submission's
  records, read only, and its own submission's results (other submissions' are not visible);
- `/opt/cloudsweep`, cloudsweep's worker, with an image already on the machine;
- `/tmp` and `/var/tmp`;
- the paths the site's `apptainer.conf` binds, such as `/etc/localtime` and `/etc/hosts`.

Your home directory, `workdir` and the site's other file systems are not visible. A program or data
there must be bound in by a `setup` line, for example `export APPTAINER_BIND=/projects/me/models`.

## What happens

1. `submit` copies a clean checkout of the commit, the inputs (by content hash) and the submission
   into `workdir` with rsync. It builds the checkout there with `[build] command`, on the cluster's
   login node or on the ssh machine, once per commit, build recipe, host and `setup`. Later
   submissions reuse the build; one that arrives while the same build runs waits for it. With
   `--no-build`, the commands run in the checkout as committed and `[build] command` is not run; a
   command that uses `{bin}` is then refused, and the receipt records `"no_build": true`.
2. It starts the work. Slurm gets `--cpus-per-task` (`[run] vcpus`), `--mem` (`memory_mib`) and
   `--time` (`hours`), plus `--signal`, so a command stopped at its time limit gets up to two minutes
   to finish uploading what it wrote (best effort). The `sbatch` options in machines.ini override
   these. The ssh pool enforces the time limit itself, but not `vcpus` or `memory_mib`.
3. Each command runs the worker in a fresh copy of the built checkout under `$TMPDIR` and records its
   attempt in `workdir/store`, as on S3. Logs are in `workdir/jobs/<submission>/logs/`.
4. `fetch` copies the results with rsync, never replacing a local file, and verifies them against
   the machine's records as it does against S3.

## Requirements and limits

- The machine's login environment must provide bash and rsync before any `setup` line; after the
  `setup` lines, git and `python3` 3.6 or newer. A cluster also needs `sbatch`, `squeue` and
  `scancel` on its login node.
- cloudsweep uses the system `ssh` and `rsync`, so your ssh configuration applies. The remote shell
  must print nothing for non-interactive logins, since rsync needs a clean channel.
- A cluster builds on its login node, so the program must run on its compute nodes, as it does on
  DeltaAI and the ECE cluster. The build uses whatever compiler the login environment provides (on
  the ECE cluster `cc` is NVIDIA's `nvc`); choose one in `setup` or in your build command.
- Nothing pins the software as an image does on AWS: the submission records the `setup` lines, not
  what they load. Check a known result on a new machine before comparing it with others.
- `systemd = yes` keeps the pool past logout only while lingering is on (`loginctl enable-linger`);
  `submit` warns when it is off. Without `systemd`, the pool survives logout on most machines.
- An ssh machine must be one host, not a round-robin alias, since its pool is a process there.
  Each submission runs its own pool.
- There are no dollar estimates or spending limits off AWS, and no `--isolated`; on Slurm, the
  [billing units](#billing-on-slurm) are shown. `workdir` keeps every build and submission; remove
  old ones there by hand.
