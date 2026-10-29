# How it works

## One submission, from commit to verified files

```
your machine                                   AWS
────────────                                   ───
submit
 ├ checks: committed? enabled? cutoff clear?
 │   fits an instance? under the cap and ceiling?
 ├ revision image for (commit, base, recipe) ──▶ ECR   (built and pushed once per commit)
 ├ job definition pinned to the image digest ──▶ control Lambda copies the stack's template
 ├ inputs by SHA-256, sweep, submission.json ──▶ S3 inputs/, submissions/<id>/
 ├ receipt written locally, then
 └ SubmitJob (array of N children) ───────────▶ Batch queue ─▶ EC2 instances (0 → cap)
                                                  each child, in a fresh container:
                                                   1 verify image digest, commit, binary hash
                                                   2 place inputs, check their hashes
                                                   3 run its command, in its own process group
                                                   4 upload {out}, stdout, stderr ─▶ S3 runs/<id>/<child>/<attempt>/
                                                   5 upload manifest.json last
fetch ◀──────────────────────────────────────── S3
 └ checks every file's size and SHA-256 against the manifest, and the provenance of each success
```

The worker is one Python file using only the standard library, plus a small S3 client. AWS
signature version 4 is implemented with `hmac` and `hashlib`, and credentials come from the ECS
task-role endpoint that Batch provides. This is why your image needs only `python3` and `git`.

## What is verified, and what is not

| Check | Where |
|---|---|
| The source is a commit, not a working tree with edits | `submit`, `try` |
| The image the job runs is the one submitted (digest) | worker, before running |
| The checkout is that commit, and its tracked files are as built | worker, before running |
| The binary has the SHA-256 recorded when it was built | worker, before running |
| Every input has the SHA-256 recorded at submission | worker, after download |
| Every output file has the size and SHA-256 that the worker recorded | `fetch` |
| A success names the submitted commit, image and binary | `fetch` |
| A new base image reproduces a known result (if configured) | `image build`, `image publish` |

The hashes show that the files you hold are the files that were produced, by that commit in that
image. They do not show that the program is correct. They also do not show that two different
images give the same results, unless you configure an acceptance run.

Host details are recorded with each attempt in `started.json`: kernel, CPU model, online CPUs,
cgroup limits. Use them when you compare timings across runs.

## Retries never overwrite evidence

The job definition retries a command once if its host failed, and never retries for any other
reason. Each attempt writes to its own directory. If a directory with the same attempt number
already has objects in it, the worker uses a suffixed name (`1-2`) instead of overwriting.
`manifest.json` is written last, so an attempt without one is known to be incomplete.

## Permissions

Day-to-day commands use the agent role. It can:

- submit jobs to this deployment's queue, using this deployment's job definition;
- read and write this deployment's bucket, except that it cannot delete objects or write under
  `runs/` (only jobs write results);
- push and pull images in this deployment's repository;
- read Batch state, this deployment's job logs, and the cutoff parameter;
- invoke the control Lambda.

It cannot register job definitions, pass IAM roles, change the budget, the cap or the cutoff,
terminate jobs directly, or touch anything outside the deployment. A job definition is registered
only by the control Lambda. The Lambda copies the stack's own template and changes nothing except
the image digest, which must be in this deployment's repository. A job therefore always runs as UID
1000, unprivileged, without host mounts, with the stack's roles.

Jobs run with a role that can read `inputs/` and `submissions/` and write `runs/`. On the
instances, a firewall rule stops non-root processes from reaching the EC2 instance metadata
service, so a job cannot use the instance's credentials. This rule is defense in depth, not
isolation. Treat your images as trusted code: a process that becomes root inside the container
could reach that service.

## Cost controls and their gaps

- **The vCPU cap** is a Batch setting. With the default allocation strategy, Batch may go over the
  cap by one instance.
- **The per-run ceiling** is checked by `submit` on your machine, against the price that `deploy`
  read.
- **The cutoff** acts on AWS Budgets' *reported* spend, which is hours behind real usage. Spending
  between the last report and the cutoff is not prevented. The budget counts only costs tagged
  `Project=<name>`, and only once that tag is active (see
  [Administration](admin.md#the-budget-counts-only-an-active-cost-allocation-tag)). It counts gross
  costs: credits and refunds are not subtracted.
- **The guard** emails you if it fails or stops running. It runs as a Lambda with one reserved
  concurrent execution.

## Files and settings

| Where | What |
|---|---|
| `cloudsweep.ini` (committed) | build recipe, image, run defaults, acceptance |
| `.cloudsweep/deploy.env` (untracked) | the deployment's names, instance types and prices, and your two local limits |
| `.cloudsweep/receipts/` | one JSON file per submission |
| `.cloudsweep/acceptance/` | acceptance results, by base image ID |
| `~/.aws/config` | the `<name>-agent` profile |

| Environment variable | Effect |
|---|---|
| `CLOUDSWEEP_ADMIN_PROFILE` | administrator profile for `deploy` and `admin` |
| `CLOUDSWEEP_ENV` | path of `deploy.env`, to use a deployment from another directory |
| `CLOUDSWEEP_ENGINE` | `docker` or `podman` (default: Podman if installed) |
| `CLOUDSWEEP_PODMAN_ROOT` | Podman storage on local disk, for network home directories |
| `CLOUDSWEEP_IDENTITY_DIR`, `CLOUDSWEEP_SIGNING_HELPER` | roles-anywhere certificate and helper locations |
