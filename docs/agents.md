# For AI agents

cloudsweep is designed so that an agent can run sweeps without supervision while the owner keeps
control of money and permissions. The agent profile cannot change the budget, the cap, the cutoff
or any IAM setting. Every command refuses with a message that says what to do instead.

## Rules for an agent

- Work only on committed source. Commit before you submit; never try to send a working tree.
- Run a command locally with `cloudsweep try` before you submit it to AWS, especially after
  changing the command line or the build.
- Use `cloudsweep submit --dry-run` to see the instance, the estimate and the worst case before a
  large sweep.
- Report what ran and what it cost: the submission ID, the commit, and the estimate that `submit`
  printed. Report the month-to-date spend from `cloudsweep status` when it matters.
- A submission's results are the files under `fetch`'s directory, and they count as results only
  when `fetch` reports `complete`. A job that Batch shows as finished is not a verified result.
- When `submit` reports that the outcome is unknown, read the receipt and `cloudsweep status`
  before you try again. Never resubmit blind.
- Never edit `.cloudsweep/deploy.env` or `~/.aws/config`, and never run `deploy` or `admin`. Those
  belong to the owner. When cloudsweep refuses because cloud use is off, the cutoff is active, or a
  run exceeds the ceiling, stop and report the refusal; run the work locally if that is acceptable.
- Cancel what you started when it is no longer needed: `cloudsweep cancel <submission>`.

## Paste this into your project's agent instructions

```markdown
## Cloud runs (cloudsweep)

Long runs and sweeps of independent runs can go to AWS Batch through `cloudsweep`
(`cloudsweep --help`; `cloudsweep doctor` when something fails). Use it when that finishes sooner
than local compute and `.cloudsweep/deploy.env` has `CLOUDSWEEP_ENABLED=1`.

1. Commit, then `cloudsweep try` the command (or a few lines of the sweep) locally.
2. `cloudsweep submit --dry-run ...`, then `cloudsweep submit ...`.
3. `cloudsweep fetch <submission> <dest> --wait`; use the results only if it reports `complete`.
4. Record the submission ID, commit and printed estimate with the results.

Do not run `cloudsweep deploy` or `cloudsweep admin`, and do not edit `.cloudsweep/deploy.env`:
those belong to the project owner. If cloudsweep refuses, report the refusal instead of working
around it.
```
