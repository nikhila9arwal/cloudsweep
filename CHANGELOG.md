# Changelog

## Unreleased

- `status` on a Slurm machine shows why each pending task waits (Slurm's reason), Slurm's estimated
  start or `no start estimate`, and each running task's node and start; `--json` carries them as
  `queue`. Times are UTC: `squeue` and `sacct` run with `TZ=UTC` and `SLURM_TIME_FORMAT=standard`,
  since clusters print their local time.
- `status` and `fetch` on a Slurm machine report `sacct`'s accounting once a task has started:
  each task's state, start, end, elapsed time, node, allocated GPUs and billing units (for a
  requeued task, each execution's), with the GPU-hours and billing unit-hours summed (`accounting` in `status --json` and in `fetch.json`).
  The final record is kept on the machine. A cluster without accounting still shows the queue.
- A dry run of a Slurm submission reads the partition's `TRESBillingWeights` and `PriorityFlags`
  with `scontrol` and shows each task's billing units (also in the receipt), with a warning when a
  GPU request's CPUs or memory exceed its GPUs' share. Tested read-only on the ECE cluster (no
  weights: allocated CPUs) and DeltaAI (`ghx4` 1000 units per GPU, `ghx4-interactive` 2000).
- **Changed:** a dry run on a machine whose section names an image already there
  (`container = PATH.sif`) now reads that image, as a submission does, so its build and image
  identity match the submission's; a missing image is refused. Earlier it said the image is
  built when you submit.
- `submit --json` on a machine prints one JSON object: the receipt, which has every field of the
  dry run, with its path. `submit --dry-run --json` prints the plan with the batch script in it.
- `submit --no-build` on a machine runs the commands in the checkout as committed without
  `[build] command`; a command that uses `{bin}` is refused, and the receipt records it.
- **Changed:** a container on a machine starts with `--no-mount cwd --pwd /work`, so a build or
  command sees the same paths wherever it was started: `/work`, `/store`, `/opt/cloudsweep` (with
  an image already on the machine), `/tmp`, `/var/tmp` and the site's binds. It no longer sees
  `workdir`, which Apptainer mounted because a job starts there, nor the home directory a command
  started from by hand would see. This needs Apptainer 1.0 or SingularityCE 3.7 or newer; an older one is refused, and
  `doctor --on` reports it. Tested on the ECE cluster (Apptainer 1.5.5); DeltaAI's 1.4.2 offers
  the option.
- `--machines FILE` on `submit`, `status`, `cancel`, `fetch`, `doctor` and `machines` searches FILE
  before the project's and the user's machines.ini. A submission's receipt now records the file and
  the resolved section (`machines_file`, `section`), and `status`, `cancel` and `fetch` of it use
  them, as do `status` and `cancel --all` without `--on` for this host's submissions; `--on MACHINE`
  uses the current files instead. Submissions are grouped by where they run
  (host, workdir, setup), no longer by section name.
- A machine section's `vars = NAME=VALUE, ...` gives its commands `{var:NAME}`, and `{machine}` its
  name, both replaced at submission, so one job can run on machines whose paths differ. A value is
  text (its braces are not placeholders); an undefined variable refuses the submission. The receipt
  records the variables used, the template's sha256 and the resolved commands.
- A command's container sees only its own submission in `/store`: the inputs and its submission's
  records read only, and its submission's results; before, it saw every submission on the machine.
- `binds = SRC[:DEST[:ro|rw]], ...` in a section with a container makes host paths visible to its
  commands (not to the build). `submit`, also in a dry run, tests each source on the machine in the
  call it makes for the image, and refuses a missing one; `doctor --on` reports them; the receipt
  records them. A destination covering cloudsweep's `/work`, `/store` or `/opt/cloudsweep` is refused.
- **Changed:** a build uses at most `build_cpus` CPUs of the machine, a new setting, by default 8 on
  Slurm machines (whose builds run on a shared login node) and all on ssh machines. The build's CPU
  affinity is set, so `nproc`, and with it `make -j"$(nproc)"`, sees that many without a change to
  the project. A Slurm build that used every core of the login node is now slower.
- `submit --partition NAME` on a Slurm machine sends one submission to that partition, after the
  section's `sbatch` options, and records it in the receipt, so a short partition needs no section.
- `status` of an ssh pool reports its `started` and `finished` times. A pool whose supervisor died
  reports how many of its commands still run (`orphans`), and `cancel` stops them; before, it said
  "not running" while they ran on.
- `cloudsweep wait SUBMISSION [--until running|done]` waits on a machine until a submission runs
  or ends. `cloudsweep race A B ... --keep first-start|first-success` keeps one of several copies
  of one job: the first to start, or the first to succeed, running one copy at a time. It records
  every decision, in UTC, in `.cloudsweep/races/` and in the copies' receipts before acting on it,
  cancels the other copies and records what each used, and resumes when run again. `status` shows a
  copy's race.
- `cloudsweep exec --on MACHINE -- CMD` runs one quick check on a cluster's login node or a machine,
  in the environment a job's command gets there (image, setup lines, binds, starting folder, worker
  variables), with no GPU and no checkout, within a time limit enforced on the machine; output
  streams and its exit code is the command's. A missing bind refuses it. A submission with
  `container = apptainer` now records its image's key, platform and recipe hash, which `exec` uses.
- The dry run and `submit` on a Slurm machine warn when `--hours` exceeds the partition's `MaxTime`
  (2 h on DeltaAI's `ghx4-interactive`), where Slurm would leave the job pending with reason
  `PartitionTimeLimit`, and record it beside the billing in the receipt. It is not a refusal.

## 0.2.2 (2026-11-09)

- An image for a machine of another architecture is built here only when containers here can run
  that architecture: binfmt_misc on, and QEMU's entry registered with the F flag, as
  qemu-user-static does. 0.2.1 accepted any enabled entry, including Ubuntu's qemu-user-binfmt,
  under which the build fails; such a host now leaves the build to the machine.

## 0.2.1 (2026-11-06)

- An image already on a machine (`container = PATH.sif`) is identified with the machine's Python
  rather than `stat`, whose output differs between coreutils implementations: 0.2.0 refused every
  such image on a machine with Rust coreutils, such as Ubuntu 26.04.

## 0.2.0 (2026-11-06)

- **Changed:** without `--on`, `submit`, `status`, `cancel`, `fetch` and `doctor` use this host
  (`local`); `--on aws` selects the AWS deployment. A submission's receipt still decides where
  `status`, `cancel` and `fetch` look, so existing AWS submissions need no flag. Scripts that
  submit to AWS must add `--on aws`.
- `--on MACHINE` runs a sweep on your own machines, in their own environment, through the system
  ssh and rsync: a Slurm cluster as one `sbatch --array` job (`kind = slurm`), or a machine you ssh
  into as a pool of slots (`kind = ssh`). This host, `local`, needs no settings. Machines are described in `~/.config/cloudsweep/machines.ini` or a project's
  `.cloudsweep/machines.ini`. `status`, `cancel` and `fetch` find a submission's machine in its
  receipt; `doctor --on MACHINE` checks a machine.
- `[run] gpus` and `--gpus N`: GPUs per command, on machines (Slurm `--gpus-per-node`, or one pool
  slot per GPU set). Slurm machines can send CPU and GPU jobs to different partitions and request a
  GPU type. The AWS queue refuses GPU commands.
- `container = apptainer` runs a machine's commands in the project's toolchain image, as on AWS. The
  image is built on the submitting host with podman or Docker (under QEMU for another architecture),
  converted to a SIF there when Apptainer is installed, and uploaded once; the machine converts or
  builds it only when the submitting host cannot. `container = PATH.sif` uses an image already on
  the machine instead.
- `cloudsweep machines` lists every machine with its `use =` note, for people and agents choosing
  where to run.
- The file store copies files in blocks instead of reading them whole.
- Image builds work with podman 3 (Ubuntu 22.04): no `--pull=missing`, and the auth file is created.
- Each command's copy of the built checkout keeps contents, permission bits and links, not extended
  attributes, which local disk refuses from a Lustre filesystem such as DeltaAI's. A failure to
  prepare a command is recorded in its attempt.

Tested end to end, with fetch reporting complete: a 24-command sweep as an ssh pool on an x86_64
machine (its system Python 3.6), on this host without `--on`, and as a Slurm array on the ECE
cluster's CPU partition (built with its login node's compiler); results were byte-identical across
the three. In a container, the sweep ran on the ECE cluster's CPU partition from an image built and
converted on the submitting host, and three of its commands in an image already on the cluster
(`container = PATH.sif`); results were identical to the runs without one. A GPU command on the ECE
cluster's GPU partition saw exactly the one GPU it asked for. On DeltaAI (aarch64, through a
ControlMaster connection) a GPU command saw exactly one GH200, both in the machine's own environment
and in an Apptainer image built under QEMU on an x86 host. Not yet exercised: `systemd = yes` with
lingering on, Slurm requeues.

## 0.1.0 (2026-11-04)

First public version.

- AWS Batch deployment with scaling to zero, budget cutoff, per-run ceiling, vCPU cap and cleanup.
- Revision images built from committed source and a toolchain Dockerfile; optional base-image
  acceptance runs.
- Local and AWS sweeps, verified downloads, progress/spending reports and cancellation.

Tested end to end on AWS in us-east-1, with m7a instances and the assume-role identity:
deploy, publish, a 24-command sweep, cancel and retire.

Not yet exercised on AWS: a cutoff that trips, roles-anywhere identity, or Graviton (arm64)
instances.
