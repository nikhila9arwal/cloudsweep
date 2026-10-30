# Security

cloudsweep creates IAM policies and runs code in your AWS account. Flaws can expose data or cost
money. Treat images and submitted programs as trusted code; see the
[permissions and isolation limits](docs/how-it-works.md#permissions).

## Reporting a vulnerability

Use this repository's **Security → Report a vulnerability** to report privately, not a public
issue. Include the access an attacker needs (such as the agent profile or project commit access)
and what they can do with it.

## Scope

- Access beyond the documented agent and job permissions in `src/cloudsweep/stack.yaml`.
- Bypasses of the intended vCPU cap, local per-run ceiling or budget cutoff.
- Access to instance credentials beyond the documented metadata-service limitation.
- Invalid results accepted by `fetch`.

Jobs share a role with deployment-wide input reads and result writes; per-job S3 isolation is
not provided. This and the [spending-control gaps](docs/admin.md#costs-and-limits) are known
limitations. Suggestions to close them are welcome.
