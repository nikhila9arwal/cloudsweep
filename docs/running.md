# Running work

Run every command from inside your project's checkout. Git worktrees of the same repository share
one deployment: the settings live in `.cloudsweep/` of the main checkout.

## try runs commands locally, in the container that AWS would use

```bash
cloudsweep try -- {bin} --sets 64 --out {out}
cloudsweep try --commands sweep.jsonl --input traces/a.txt --keep /tmp/trial
```

`try` builds the revision image for the commit and runs each command through the worker on your
machine, one after another, with no network inside the container. It then checks the results the
same way `fetch` does. It uses the local base image from `cloudsweep image build`, or else pulls the
newest published one. Use `try` to find a wrong path or a missing library before you pay for it.

## submit runs a commit's commands on AWS Batch

```bash
cloudsweep submit --hours 2 -- {bin} --sets 64 --out {out}           # one command
cloudsweep submit --hours 2 --commands sweep.jsonl --input traces/a.txt
cloudsweep submit --dry-run --commands sweep.jsonl                    # plan and estimate only
```

| Option | Meaning |
|---|---|
| `--hours H` | time limit for each command; a command that runs longer is stopped (60 s to 14 days) |
| `--vcpus N`, `--memory-mib M` | what each command reserves (defaults from `[run]`, else 1 and 3500) |
| `--revision REV` | the commit to run (default `HEAD`) |
| `--input PATH[=NAME]` | a file to place in the checkout; repeatable |
| `--isolated` | one command per largest instance, nothing beside it: for timing measurements |
| `--label TEXT` | free text kept with the submission |

A sweep file holds one command per line, each a JSON list. Lines starting with `#` are ignored. A
sweep becomes one Batch array job, with one child per line. A sweep can have at most 10,000 lines.
A short script that writes the file is usually the clearest way to define a sweep
([example](../examples/cache-sim/configs/make_sweep.py)).

`submit` refuses, and says why, when:

- tracked files have uncommitted changes (a run uses committed source only);
- the commit has no `cloudsweep.ini`, or a command names an unknown placeholder;
- cloud use is off (`CLOUDSWEEP_ENABLED=0` in `.cloudsweep/deploy.env`), or the budget cutoff is
  active;
- no deployed instance type can hold the requested vCPUs and memory, or the instance it needs is
  larger than the vCPU cap;
- the worst-case cost of one command is above `CLOUDSWEEP_MAX_RUN_USD`.

When it succeeds, it prints the submission ID (for example `20261029T205712Z-1fad4b`), the instance
type, and a cost estimate. It also writes a receipt to `.cloudsweep/receipts/`. If the connection
drops during submission, the receipt records what was intended. cloudsweep never resubmits
automatically, so a retry cannot start the work twice.

### Sizing: commands share instances unless they ask for a whole one

Batch packs commands onto instances by their vCPU and memory reservations. Two 1-vCPU commands can
run on one 2-vCPU instance and share its memory bandwidth and caches. Packing is cheapest. If
run-to-run timing matters, reserve all of an instance's vCPUs (`--vcpus 2` on a 2-vCPU type) or use
`--isolated`. About 90% of an instance's memory is assumed usable by jobs, so a 3,500 MiB command
fits on an 8 GiB instance and a 7,500 MiB one needs the next size up.

The per-run estimate is the price of the whole cheapest instance that fits the command, multiplied
by its time limit. It is an upper bound for one attempt. Commands that share an instance, or finish
early, cost less. A retry after a host failure, instance start-up time, and idle time before an
instance stops add to the cost.

## status shows jobs, capacity and reported spend

```bash
cloudsweep status                    # every submission still in the queue's history
cloudsweep status <submission>
cloudsweep status --storage          # bytes kept in the bucket and registry
```

Desired capacity is what Batch has asked EC2 for. It is not a count of running instances. Reported
spend comes from AWS Budgets through the guard, which reads it every five minutes, and it lags real
usage by hours.

## fetch downloads results and verifies every file

```bash
cloudsweep fetch <submission> --wait          # wait until finished (up to a day), then fetch
cloudsweep fetch <submission> results/        # into results/<submission>/
```

Results go to `cloudsweep-results/<submission>/` unless you name another directory:

```
<submission>/
  submission.json            what ran: commit, image digest, recipe, inputs, resources
  commands.jsonl             the sweep, exactly as submitted
  fetch.json                 the verdict for every command
  0/                         command 0
    1/                       attempt 1 (a host-failure retry is 2; a rerun of the same attempt is 1-2)
      manifest.json          status, exit code, timing, host, and SHA-256 of every file
      started.json
      stdout.txt  stderr.txt
      out/                   what the command wrote to {out}
  1/ ...
```

Each file is checked against the size and SHA-256 that the worker recorded. A successful attempt
must also name the submitted commit, image and binary. `fetch` exits with status 0 only if every
command has a verified successful attempt. An attempt without `manifest.json` is incomplete: the
container was stopped before it finished uploading. `fetch` downloads what exists and reports it
as incomplete. Running `fetch` again is safe. It never overwrites a local file with different
content.

## cancel stops a submission, or everything

```bash
cloudsweep cancel <submission>
cloudsweep cancel --all --wait 30        # and wait until no instance is left
```

Waiting commands are cancelled, and running ones are stopped. A command receives SIGTERM and has
20 seconds to exit before it is killed. Whatever it has written to `{out}` is still uploaded.
Cancelling works when cloud use is off and when the cutoff is active.
