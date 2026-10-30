# Running work

Run commands inside your project's Git checkout. Worktrees share the deployment settings in
`.cloudsweep/` of the main checkout. `cloudsweep doctor` checks prerequisites and reports fixes.

## Commands and inputs

Commands are argument lists, executed without a shell in the checkout. Use `["sh", "-c", "..."]`
for shell syntax. A sweep file contains one JSON argument list per line; blank lines and lines
starting with `#` are ignored. Up to 10,000 commands are supported. Multiple commands become one
Batch array job, with one child per command. See the [sweep generator](../examples/cache-sim/configs/make_sweep.py).

| Placeholder | Value |
|---|---|
| `{bin}` | `/work/src/<[build] binary>` |
| `{out}` | the attempt's output directory; write results here |
| `{src}` | `/work/src`, the checkout and working directory |
| `{index}` | command index, starting at 0; useful as a seed |
| `{input:NAME}` | path of the input named NAME |
| `{{`, `}}` | literal braces |

Pass external files with repeatable `--input PATH[=NAME]`. Without NAME, PATH must be inside the
checkout and keeps its repository-relative location: `--input traces/a.txt` becomes
`/work/src/traces/a.txt`. Use NAME for files outside the checkout. Inputs are uploaded by content
hash and reused; they cannot replace tracked files or files already in the revision image.

## try

```bash
cloudsweep try -- {bin} --sets 64 --out {out}
cloudsweep try --commands sweep.jsonl --input traces/a.txt --keep trial-results
```

`try` builds the revision and runs commands sequentially through the worker, without container
network access, then verifies results as `fetch` does. It uses the locally built base image,
otherwise pulls the newest published base. The time limit applies locally; AWS CPU and memory
reservations do not. Use it to catch missing files and libraries before paying for AWS runs.

## submit

```bash
cloudsweep submit --hours 2 -- {bin} --sets 64 --out {out}
cloudsweep submit --dry-run --commands sweep.jsonl --input traces/a.txt
cloudsweep submit --commands sweep.jsonl --input traces/a.txt
```

**AWS runs cost money.** `--dry-run` checks and estimates without building, uploading or submitting.
See [costs and limits](admin.md#costs-and-limits) for the estimate, ceiling and billing gaps.

| Option | Meaning |
|---|---|
| `--hours H` | per-command time limit: 60 seconds to 14 days; defaults to `[run] hours` |
| `--vcpus N`, `--memory-mib M` | per-command reservations; defaults from `[run]`, else 1 and 3500 |
| `--revision REV` | committed revision; default `HEAD` |
| `--isolated` | reserve the largest deployed instance's CPU and usable memory per command |
| `--label TEXT` | label saved with the submission |

Commit tracked changes and `cloudsweep.ini` first. Unknown placeholders, disabled cloud use,
an active cutoff, requests that do not fit the instance types or cap, and costs above the
per-run ceiling are refused.

Submission prints its ID, planned instance type and cost estimates, and writes a receipt under
`.cloudsweep/receipts/`. **If the outcome is unknown, inspect that receipt and `cloudsweep status`
before retrying.** cloudsweep does not automatically resubmit; rerunning `submit` can duplicate work.

### Sizing

Batch packs commands by their CPU and memory reservations. Two 1-vCPU commands can share a 2-vCPU
instance's caches and memory bandwidth. For timing measurements, reserve all an instance's vCPUs
or use `--isolated`. cloudsweep assumes 90% of memory is usable: 3,500 MiB fits on an 8 GiB instance;
7,500 MiB needs a larger one. Packing and shorter runtimes can reduce costs.

## status

```bash
cloudsweep status                     # submissions in the queue's history
cloudsweep status SUBMISSION
cloudsweep status --storage           # retained bucket objects and registry images
```

Desired capacity is what Batch requested, not the number of running instances. Spend comes from
the guard's [budget snapshot](admin.md#costs-and-limits) and lags usage. Status remains available
when cloud use is off or the cutoff is active.

## fetch

```bash
cloudsweep fetch SUBMISSION --wait     # wait up to a day
cloudsweep fetch SUBMISSION results/   # download into results/SUBMISSION/
```

**Results count only when `fetch` reports `complete`** (exit status 0): every command must have a
verified successful attempt. Batch finishing is insufficient. See the
[verification checks](how-it-works.md#verification).

The default destination is `cloudsweep-results/SUBMISSION/`:

```text
SUBMISSION/
  submission.json       commit, image, recipe, inputs, resources
  commands.jsonl        submitted commands
  fetch.json            verdicts and verification problems
  0/                    command index
    1/                  attempt number
      manifest.json     status, exit code, timing, provenance, file hashes
      started.json      start and host details
      stdout.txt
      stderr.txt
      out/              files written to {out}
```

An attempt without `manifest.json` is incomplete. `fetch` downloads available files and reports
missing, skipped or invalid outputs. Fetching again never overwrites a local file with different
content. See [output limits](../README.md#limits) and [retry directories](how-it-works.md#retries).

## cancel

```bash
cloudsweep cancel SUBMISSION
cloudsweep cancel --all --wait 30      # wait up to 30 minutes for capacity to reach zero
```

Waiting jobs are cancelled; active jobs are stopped. The command gets SIGTERM and 20 seconds to
exit before being killed. The worker then attempts to upload outputs, but Batch kills the
container after 30 seconds: partial results are best effort. Write them incrementally and expect
some to be missing. Cancellation reports remaining capacity and works when cloud use is off
or the cutoff is active.
