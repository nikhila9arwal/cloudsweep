# For AI agents

Paste this block into your project's agent instructions. The owner configures
[spending limits](admin.md#costs-and-limits) and [permissions](how-it-works.md#permissions).

```markdown
## Cloud runs (cloudsweep)

Use cloudsweep for long runs or independent sweeps when AWS will finish sooner than local
compute and `.cloudsweep/deploy.env` has `CLOUDSWEEP_ENABLED=1`. AWS runs cost money.
Use `cloudsweep --help` for syntax and `cloudsweep doctor` to diagnose failures.

1. Commit source, then test the command or a few sweep points with `cloudsweep try` locally.
   Repeat after changing the build or command line.
2. Run `cloudsweep submit --dry-run ...`; inspect the instance, estimate and worst case before
   `cloudsweep submit ...`.
3. Fetch with `cloudsweep fetch SUBMISSION DEST --wait`. Use results only when it reports
   `complete`; a finished Batch job is insufficient.
4. Record the submission ID, commit and printed estimate. Report month-to-date spend from
   `cloudsweep status` when relevant.
5. Cancel unneeded work you started with `cloudsweep cancel SUBMISSION`.

If submission has an unknown outcome, inspect its receipt and `cloudsweep status` before
retrying. Never resubmit blindly.

Do not run `deploy` or `admin`, or edit `.cloudsweep/deploy.env` or `~/.aws/config`; these belong
to the owner. If cloud use is off, the cutoff is active, or the ceiling is exceeded, stop and
report the refusal. Do not bypass it; run locally if acceptable.
```
