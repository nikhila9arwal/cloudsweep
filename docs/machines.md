# Your own machines

Run a sweep on this host, on a machine you reach over ssh, or on a Slurm cluster, with the same
commands as on AWS. Each command runs in the machine's own environment: its compilers, modules,
Python and GPUs. No container is needed.

```bash
cloudsweep submit --on local --commands sweep.jsonl            # this host; needs no settings
cloudsweep submit --on deltaai --commands sweep.jsonl --gpus 1
cloudsweep status <submission>
cloudsweep fetch <submission> --wait
cloudsweep cancel <submission>
```

`status`, `cancel` and `fetch` find the machine in the submission's receipt; `--on MACHINE` names
it explicitly. `fetch` gives the same verified `cloudsweep-results/<submission>/` as from AWS: use
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
on the machine itself, one command per slot. `--on local` is a built-in `ssh` machine for this host
(Linux only) that works without ssh; a `[local]` section changes its settings.

| Key | Kinds | Meaning (default) |
|---|---|---|
| `kind` | both | `slurm` or `ssh` |
| `host` | both | ssh alias or host name, or `local` for this host (the section name) |
| `workdir` | both | directory on the machine for builds, jobs and results; relative paths are under its home directory; on a cluster it must be shared with the compute nodes (`cloudsweep`) |
| `setup` | both | shell lines run first, in your home directory, one per line: module loads, a venv (none) |
| `sbatch`, `cpu_sbatch`, `gpu_sbatch` | slurm | extra `sbatch` options for every job, CPU jobs and GPU jobs: account, partition (none) |
| `gpu_type` | slurm | GPU type to request, such as `h100` (any) |
| `slots` | ssh | CPU commands at once (the machine's CPUs ÷ `[run] vcpus`) |
| `gpus` | ssh | how many GPUs to hand out (those in `CUDA_VISIBLE_DEVICES` if set, else `nvidia-smi`'s count) |
| `systemd` | ssh | `yes` starts the pool with `systemd-run --user`, for machines that stop your processes at logout (`no`) |

`cloudsweep doctor --on MACHINE` checks that cloudsweep can reach the machine and finds the commands
it needs and a writable `workdir`; it reports the CPUs of the host it reached and, for an ssh pool,
its GPUs. It does not submit a job, so it cannot check a cluster's compute nodes, accounts or
partitions: those show up at `submit`.

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

## What happens

1. `submit` copies a clean checkout of the commit, the inputs (by content hash) and the submission
   into `workdir` with rsync. It builds the checkout there with `[build] command`, on the cluster's
   login node or on the ssh machine, once per commit, build recipe, host and `setup`. Later
   submissions reuse the build; one that arrives while the same build runs waits for it.
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
- There are no cost estimates or spending limits off AWS, and no `--isolated`. `workdir` keeps every
  build and submission; remove old ones there by hand.
