# Administration

Use an administrator AWS CLI profile for `deploy` and `admin`: pass `--admin-profile`, or set
`CLOUDSWEEP_ADMIN_PROFILE` (default profile: `default`). Only the administrator can change AWS
limits, lift a cutoff or remove the deployment.

## deploy

```bash
cloudsweep deploy --admin-profile admin --email you@example.org
```

Deployment creates billable resources and takes about five minutes initially. Prerequisites:

- At least 101 unreserved Lambda executions: the guard reserves one and AWS keeps 100 unreserved.
  `deploy` checks this; new accounts may need a quota increase.
- Public subnets with public IPv4 assignment and an internet-gateway route for image pulls and S3.
  Workers accept no inbound connections. Use the default VPC or pass `--vpc` and `--subnets`.

`deploy` reads instance CPU, memory, architecture and On-Demand prices; creates the
`<name>-cloud` stack; writes and checks the `<name>-agent` profile in `~/.aws/config`; and saves
local settings in `.cloudsweep/deploy.env`. Confirm the AWS subscription email to receive
guard warnings.

Reruns preserve omitted settings. They target the recorded deployment unless overridden:

| Option | Initial default |
|---|---|
| `--prefix` | `[project] name`; on reruns, recorded name |
| `--region` | recorded region for that name, else admin profile's region, else `us-east-1` |
| `--instance-types` | `m7a.large,m7a.xlarge,m7a.2xlarge,m7a.4xlarge` |
| `--disk-gib` | 60 per instance: root disk for image layers, checkout and outputs |
| `--identity` | `assume-role` |
| `--price TYPE=USD` | Price List API; override only if that lookup fails |

All types must share an architecture. Include small and large sizes so small jobs can use small
instances. m7a/m8a have 4 GiB per vCPU, r7a/r8a have 8, and c7a has 2; each vCPU is a physical
core on these AMD types. On m7i/c7i, a vCPU is a hyperthread. Graviton needs arm64 images;
building them on x86 needs emulation such as QEMU.

## Resources

| Resource | Purpose |
|---|---|
| Versioned S3 bucket `<name>-<account>-<region>` | inputs, submissions, results |
| ECR repository `<name>-worker` | base/revision images; immutable tags |
| Batch compute environment, queue, job definition | On-Demand jobs; scales to zero after work ends |
| Agent role | [routine command permissions](how-it-works.md#permissions) |
| Guard Lambda | checks spend, cost tag and jobs every five minutes |
| Control Lambda | queue cancellation and image-pinned job definitions |
| Budget `<name>-monthly`, two health alarms | spending emails and guard failure/missing-invocation alerts |

The guard stops jobs observed in STARTING for over 30 minutes. It emails after three hours
without job progress, or if capacity remains 30 minutes after work ends.

## Costs and limits

You pay for On-Demand instances, storage (including disks, results, images and logs), data
transfer, [public IPv4 addresses](https://aws.amazon.com/vpc/pricing/), guard invocations and
alarms. Compute scales down after jobs finish; retained storage, the guard and alarms still
cost money. Inspect storage with
`cloudsweep status --storage`; the administrator can delete unneeded data.

`deploy` records current instance prices. `submit` prints an **estimate** (cheapest fitting
instance × time limit) and **worst case** (most expensive deployed fitting instance × time limit).
Both price a whole instance per command. Sharing or finishing early reduces cost; host-failure
retries, startup, idle time and non-compute charges add to these figures. Neither is a bill cap.

| Control | Default | Enforcement and gap |
|---|---|---|
| `--max-vcpus` | 32 | Batch cap; may exceed it by one instance |
| `CLOUDSWEEP_MAX_RUN_USD` | $10 | local `submit` check against one command's worst case; excludes the additional costs above and does not limit the whole sweep |
| `--cutoff` | $100/month | guard stops work at reported spend; billing lags usage by hours, so the bill can exceed it |
| `--alerts A1,A2` | $50,$80 | monthly email alerts |

The budget counts gross costs tagged `Project=<name>`, without subtracting credits/refunds,
and only after tag activation. **An inactive tag means the cutoff sees no spend.** Tags can take
up to 24 hours to appear for activation, then up to another 24 hours to activate
([AWS documentation](https://docs.aws.amazon.com/awsaccountbilling/latest/aboutv2/activating-tags.html)):

- Standalone account: `deploy` tries to activate `Project`; the guard retries daily.
- Organization member: ask the management account to activate `Project` in Billing → Cost
  allocation tags.

`status` and `doctor` report the last observed tag state. The vCPU cap and local ceiling still
apply while the tag is inactive.

Administrators can edit the untracked `.cloudsweep/deploy.env`, which records deployment names,
types, prices and local limits. Initially `CLOUDSWEEP_ENABLED=1` and `CLOUDSWEEP_MAX_RUN_USD=10`.
`CLOUDSWEEP_ENABLED=0` blocks local `submit` and `image publish`; existing jobs continue until
cancelled. These local controls are not AWS enforcement. If the ceiling blocks long runs,
deploy without larger types or raise the local ceiling.

## Reset a cutoff

```bash
cloudsweep deploy --admin-profile admin --cutoff 200   # if a higher limit is needed
cloudsweep admin reset-cutoff --admin-profile admin
```

Check actual spending first. A tripped cutoff is recorded in SSM, denies the agent submissions,
S3 uploads and image pushes, disables the queue and compute environment, and stops waiting/running
jobs. Reading results and cancellation remain available. AWS emails you. Neither deployment nor
a new month clears the cutoff; an administrator must reset it.

## Identities

Both methods grant the same [agent permissions](how-it-works.md#permissions).

- **assume-role** (default): uses the administrator profile's credentials, including SSO, to
  assume the agent role. Those credentials must remain available on the machine.
- **roles-anywhere**: uses an X.509 certificate without AWS keys on the submitting
  machine. Deployment creates a private CA and one-year certificate, retains the certificate/key
  under `~/.aws/rolesanywhere/<name>-agent/`, and installs the signing helper (x86_64 Linux only).
  `status` warns within 30 days of expiry; rerun `deploy` to renew. Use
  `--rotate-certificate` to renew earlier. The CA private key is not retained.

`CLOUDSWEEP_IDENTITY_DIR` and `CLOUDSWEEP_SIGNING_HELPER` override certificate and helper locations.

## retire

```bash
cloudsweep admin retire --admin-profile admin --yes
```

Retirement disables local cloud use and submissions, stops jobs, waits up to 30 minutes for
instances to disappear, then deletes the stack and job definitions. It usually takes about ten
minutes. The bucket and image repository remain **billable**; the cutoff parameter, agent profile
and local deployment settings also remain.

To remove retained data and settings:

```bash
cloudsweep admin retire --admin-profile admin --yes --delete-data
```

**This cannot be undone. Fetch needed results first.** It deletes every bucket object version,
the bucket, image repository, cutoff parameter, agent profile/certificate and `deploy.env`.
Local receipts, acceptance results and container images remain. If retirement stops partway,
rerun the same command to continue.

## Another machine

Rerun `deploy` there with the same administrator profile, `--prefix` and `--region` to write local
settings and reconcile the stack. With roles-anywhere, issuing a certificate on the second
machine invalidates the first machine's certificate. Use assume-role when both have administrator
credentials. `CLOUDSWEEP_ENV` selects an existing `deploy.env` from another directory.
