# Changelog

## 0.1.0 (unreleased)

First public version.

- Deploy an AWS Batch queue that scales to zero, with a monthly budget cutoff, a per-run cost
  ceiling and a vCPU cap (`deploy`, `admin reset-cutoff`, `admin retire [--delete-data]`).
- Build a revision image per commit on top of your own toolchain Dockerfile, with an optional
  acceptance run for new base images (`image build`, `image publish`).
- Run commands and sweeps locally (`try`) or on AWS (`submit`), then download and verify every
  result file (`fetch`), check progress and spend (`status`) and stop work (`cancel`).

Tested end to end on AWS in us-east-1, with m7a instances and the assume-role identity:
deploy, publish, a 24-command sweep, cancel and retire. Not yet exercised on AWS: a cutoff that
trips, the roles-anywhere identity, and Graviton (arm64) instances.
