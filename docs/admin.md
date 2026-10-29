# Administration

The administrator deploys the AWS resources once, sets the limits, and is the only person who can
lift a budget cutoff or remove the deployment. All of this runs through `cloudsweep deploy` and
`cloudsweep admin`, with an AWS CLI profile that has administrator rights. Give it with
`--admin-profile`, or set `CLOUDSWEEP_ADMIN_PROFILE`; the default is the profile named `default`.

## deploy creates everything, and reruns change settings

```bash
cloudsweep deploy --admin-profile admin --email you@example.org
```

The first deploy takes about five minutes. It:

1. finds the region's default VPC and its subnets (or uses `--vpc` and `--subnets`) and checks that
   the subnets give instances public addresses and a route to the internet: instances need it to
   pull images and reach S3, and they accept no inbound connections;
2. reads each instance type's vCPUs, memory, architecture and On-Demand price from AWS;
3. creates a CloudFormation stack named `<name>-cloud` (listed below);
4. adds an AWS CLI profile `<name>-agent` to `~/.aws/config` for day-to-day commands;
5. writes `.cloudsweep/deploy.env` in your project, with `CLOUDSWEEP_ENABLED=1` and
   `CLOUDSWEEP_MAX_RUN_USD=10`;
6. checks that the agent profile works.

Rerun `deploy` with only the options you want to change; every other setting keeps its current
value:

| Option | Default |
|---|---|
| `--region` | the admin profile's region, else `us-east-1` |
| `--instance-types` | `m7a.large,m7a.xlarge,m7a.2xlarge,m7a.4xlarge` |
| `--max-vcpus` | 32 |
| `--alerts A1,A2` and `--cutoff C` | 50,80 and 100 (USD per month) |
| `--disk-gib` | 60 (root disk per instance: image layers, checkout, outputs) |
| `--identity` | `assume-role` (see below) |
| `--price TYPE=USD` | read from the AWS Price List API; give it only if that read fails |

Choose instance types by what your commands need per vCPU. m7a and m8a have 4 GiB per vCPU, r7a and
r8a have 8 GiB, and c7a has 2 GiB. On these AMD types, one vCPU is one physical core. On Intel types
(m7i, c7i, ...) a vCPU is a hyperthread. Graviton types (m7g, c8g, ...) need an arm64 image:
cloudsweep builds for the deployment's architecture, which needs QEMU emulation on an x86 machine.
All listed types must share one architecture. Include a range of sizes so that small commands get
small instances.

## What the stack contains

| Resource | Purpose |
|---|---|
| S3 bucket `<name>-<account>-<region>` | inputs, sweep files, results; versioned; kept after `retire` |
| ECR repository `<name>-worker` | base and revision images; tags cannot be overwritten; kept after `retire` |
| Batch compute environment and queue | On-Demand EC2, minimum 0 vCPUs, maximum the cap |
| Run job definition | the only container settings jobs can use: UID 1000, not privileged, no host mounts |
| Agent role and profile | what day-to-day commands can do (see [How it works](how-it-works.md#permissions)) |
| Guard Lambda, every 5 minutes | enforces the cutoff, stops stuck jobs, reports spend, checks the cost tag |
| Control Lambda | cancels jobs in this queue; registers job definitions for new image digests |
| AWS Budget `<name>-monthly` and two alarms | the alert emails, the cutoff trigger, and an email if the guard fails or stops running |

Nothing runs between jobs. With no work, what you pay for is storage (results, images, logs), the
guard's invocations and the alarms. Check `cloudsweep status --storage` from time to time, and
delete old results from the bucket when you no longer need them.

## The budget counts only an active cost-allocation tag

Every resource is tagged `Project=<name>`, and the budget counts only costs with that tag. AWS
Billing counts a tag only after the tag is *activated*, and it can be activated only after AWS has
seen it on a bill, up to 24 hours after first use. Until then the budget, and so the cutoff, sees
no spend. The vCPU cap and the per-run ceiling still limit spending in that period.

- **A standalone account**: deploy tries to activate the tag, and the guard tries again once a day
  until it succeeds. No action is needed.
- **A member account of an AWS Organization**: only the organization's management account can
  activate tags. Ask whoever manages it to activate `Project` under Billing and Cost Management →
  Cost allocation tags.

`cloudsweep status` and `cloudsweep doctor` show the tag's state as the guard last saw it.

## A tripped cutoff stays tripped until an administrator lifts it

When reported monthly spend reaches the cutoff, the guard immediately:

- records the cutoff in an SSM parameter;
- attaches a policy to the agent role that denies all job submission;
- disables the queue and the compute environment;
- stops every waiting and running job.

AWS emails you. To resume, check the actual spend, raise the cutoff if needed, then lift it:

```bash
cloudsweep deploy --admin-profile admin --cutoff 200     # only if spend is still above the cutoff
cloudsweep admin reset-cutoff --admin-profile admin
```

`deploy` never clears a cutoff. A new month does not clear it either.

The guard also stops jobs stuck in STARTING for more than 30 minutes. It emails you if jobs wait for
three hours without progress, or if capacity has not returned to zero 30 minutes after work ended.

## Two identities: assume-role (default) and roles-anywhere

The agent profile uses one of two methods to get credentials:

- **assume-role** (default): the profile assumes the agent role using your administrator profile's
  credentials, so it works only on a machine that also has those credentials. This works on any
  operating system and with SSO profiles. It needs no maintenance.
- **roles-anywhere**: the profile signs in with an X.509 certificate through IAM Roles Anywhere, so
  the machine needs no AWS keys. Use it on a shared machine where your administrator credentials
  should not be stored. `deploy` makes a private CA and a one-year certificate, keeps only the
  certificate and its key (in `~/.aws/rolesanywhere/<name>-agent/`), and installs AWS's signing
  helper (x86_64 Linux only). Rerun `deploy` within 30 days of expiry to renew it;
  `cloudsweep status` warns you then.

Either way, the agent role can only do what [How it works](how-it-works.md#permissions) lists.

## The flag and the ceiling are local settings

`.cloudsweep/deploy.env` is yours to edit:

- `CLOUDSWEEP_ENABLED=0` makes `submit` and `image publish` refuse new work on this machine.
  Existing work keeps running; `cloudsweep cancel --all` stops it.
- `CLOUDSWEEP_MAX_RUN_USD` is the most that one command may be estimated to cost.

These limits are enforced by cloudsweep on the machine where it runs, not by AWS. The cap and the
cutoff are enforced by AWS.

## retire removes the deployment and keeps your results

```bash
cloudsweep admin retire --admin-profile admin --yes
```

This sets cloud use off, blocks submission, stops all jobs, waits until no instance is left (up to
30 minutes), and deletes the stack and every job definition. The bucket and the image repository
are kept, and you keep paying for their storage until you delete them in the S3 and ECR consoles.
Also left in place: the cutoff parameter, the agent profile in `~/.aws/config`, and
`.cloudsweep/deploy.env`.

## Using one deployment from another machine

The deployment's settings are files on the machine where you ran `deploy`. To use the deployment
from a second machine, run `deploy` there too, with the same admin profile and `--prefix`. The rerun
changes nothing in AWS and writes the local files. With roles-anywhere, the rerun issues a new
certificate if the second machine has none, and that makes the first machine's certificate
invalid. Use assume-role if both machines have administrator credentials.
