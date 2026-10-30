# How it works

## Submission lifecycle

```text
your machine                                  AWS
submit
 ├ admission checks
 ├ revision image ──────────────────────────▶ ECR
 ├ image digest ────────────────────────────▶ control Lambda → job definition
 ├ inputs, commands, submission manifest ────▶ S3
 ├ local receipt
 └ SubmitJob ───────────────────────────────▶ Batch queue → EC2
                                               one container per command:
                                                1 verify build and inputs
                                                2 run command in its own process group
                                                3 upload outputs, stdout, stderr
                                                4 upload manifest.json last
fetch ◀───────────────────────────────────── S3 runs/<id>/<child>/<attempt>/
 └ verify downloaded files and report completion
```

See [Running work](running.md) for commands and [Administration](admin.md#costs-and-limits) for
admission controls and their gaps.

## Revision images

A revision image is keyed by the commit, base image digest and build recipe. `submit` reuses a
published match; otherwise it builds and pushes one from the newest published base.

The build starts with a shallow clean checkout at `/work/src`, including `.git` but no local
uncommitted or untracked files. It runs the committed build command as root and records the
commit, binary hash and tracked-file state. Programs can use `git rev-parse HEAD` in the checkout.
See [project configuration](project.md#configuration) for the recipe.

## Verification

| Check | Where |
|---|---|
| Committed source; no tracked working-tree edits | `submit`, `try` |
| Submitted image digest and commit match the build; tracked contents/modes and binary hash match the build record | worker, before execution |
| Downloaded input hashes match the submission | worker |
| File sizes/hashes match the attempt manifest; successful attempts name the submitted commit/image and valid binary provenance | `fetch` |
| Known output reproduced, when acceptance is configured | `image build`, `image publish` |

Hashes check file integrity and recorded provenance; they do not prove program correctness.
An [acceptance run](project.md#acceptance-runs) checks one known result across image changes,
not equivalence for every input. Programs exit 0 to succeed; results count only when
[`fetch` reports `complete`](running.md#fetch).

Each attempt records the kernel, CPU model, online CPUs and cgroup limits in `started.json`
for timing comparisons. Images and manifests are trusted code/data, not independent attestations.

## Retries

Batch retries once for a host failure, not other failures. Each attempt has its own directory.
If its number is already present, the worker adds a suffix (`1-2`). The final manifest is uploaded
last; without it an attempt is incomplete. See [submission receipts](running.md#submit) before
manually retrying an uncertain submission.

## Permissions

The agent role can:

- submit to this queue using this deployment's job definitions;
- read the bucket and upload to `inputs/` and `submissions/`, but not delete objects or write results;
- push/pull repository images and read their sizes;
- read Batch state, deployment job logs and cutoff state;
- invoke the control Lambda.

**Batch reads are account-wide, across regions**, including other jobs' and job definitions'
environment variables. These read permissions cannot be scoped to the queue. Use a separate
account if other Batch workloads store secrets in environment variables.

The agent cannot register job definitions, pass IAM roles, modify IAM or AWS spending limits,
clear the cutoff, terminate jobs directly, or change resources outside the deployment.
The control Lambda cancels only this queue's jobs. For new images it copies the stack's job
template, changing only the image digest to one in this repository: UID 1000, unprivileged,
no host mounts, and the stack's roles.

Jobs obtain task-role credentials from the ECS endpoint. Their shared role reads **all**
`inputs/` and `submissions/` and writes anywhere under `runs/` in this deployment's bucket.
There is no per-job S3 isolation; a job could overwrite another job's results. Bucket versioning
preserves versions, but does not prevent those writes.

Trust your images and submitted programs. A firewall blocks non-root access to EC2 instance
metadata; a process that becomes root inside the container could reach it. This is defense in
depth, not isolation. See [Security](../SECURITY.md) for reporting problems.
