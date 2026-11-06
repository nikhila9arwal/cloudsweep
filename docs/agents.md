# For AI agents

Paste this block into your project's agent instructions. The owner configures
[spending limits](admin.md#costs-and-limits) and [permissions](how-it-works.md#permissions).

```markdown
## Runs (cloudsweep)

Run builds and sweeps with cloudsweep. Without `--on` it runs on this host. `--on MACHINE` runs the
same commands on one of the owner's machines: `cloudsweep machines` lists them with the owner's
note on what each is for and when to ask first. `--on aws` runs them on AWS Batch, which costs
money and needs `CLOUDSWEEP_ENABLED=1` in `.cloudsweep/deploy.env`; use it for long runs or large
sweeps when AWS will finish sooner. Use `cloudsweep --help` for syntax and
`cloudsweep doctor [--on MACHINE]` to diagnose failures.

1. Commit source, then test the command or a few sweep points on this host (`cloudsweep submit`;
   `cloudsweep try` checks them in the AWS image). Repeat after changing the build or command line.
2. For AWS, run `cloudsweep submit --on aws --dry-run ...`; inspect the instance, estimate and
   worst case before `cloudsweep submit --on aws ...`.
3. Fetch with `cloudsweep fetch SUBMISSION DEST --wait`. Use results only when it reports
   `complete`; a finished job is insufficient.
4. Record the submission ID, commit and machine, and on AWS the printed estimate. Report
   month-to-date spend from `cloudsweep status --on aws` when relevant.
5. Cancel unneeded work you started with `cloudsweep cancel SUBMISSION`.

If submission has an unknown outcome, inspect its receipt and `cloudsweep status SUBMISSION`
before retrying. Never resubmit blindly.

Do not run `deploy` or `admin`, or edit `.cloudsweep/deploy.env` or `~/.aws/config`; these belong
to the owner. If cloud use is off, the cutoff is active, or the ceiling is exceeded, stop and
report the refusal. Do not bypass it; run locally if acceptable.
```
