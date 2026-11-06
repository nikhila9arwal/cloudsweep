# Changelog

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
