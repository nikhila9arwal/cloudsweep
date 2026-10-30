# Security

cloudsweep creates IAM roles and policies in your AWS account and runs your code on EC2 instances.
A flaw in either can cost money or expose data, so please report suspected problems privately.

## Reporting a vulnerability

Use GitHub's private vulnerability reporting: the **Security** tab of this repository, then
**Report a vulnerability**. Please do not open a public issue. Include what an attacker needs (for
example, the agent profile, or the ability to commit to the project) and what they gain.

## What is in scope

- The IAM permissions in `src/cloudsweep/stack.yaml`: anything that lets the agent profile or a job
  do more than [How it works](docs/how-it-works.md#permissions) says.
- Ways around the cost controls: the vCPU cap, the per-run ceiling, or the budget cutoff.
- Ways for a job to read or change another job's inputs or results, or to use the instance's
  credentials.
- Result verification that `fetch` accepts when it should not.

The documented gaps in [How it works](docs/how-it-works.md#cost-controls-and-their-gaps) are known
limits, not vulnerabilities. Reports about them are still welcome if you see a way to close one.
