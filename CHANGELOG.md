# Changelog

## 0.1.0 (unreleased)

First public version.

- AWS Batch deployment with scaling to zero, budget cutoff, per-run ceiling, vCPU cap and cleanup.
- Revision images built from committed source and a toolchain Dockerfile; optional base-image
  acceptance runs.
- Local and AWS sweeps, verified downloads, progress/spending reports and cancellation.

Tested end to end on AWS in us-east-1, with m7a instances and the assume-role identity:
deploy, publish, a 24-command sweep, cancel and retire.

Not yet exercised on AWS: a cutoff that trips, roles-anywhere identity, or Graviton (arm64)
instances.
