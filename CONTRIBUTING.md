# Contributing

Bug reports, fixes and documentation improvements are welcome. Open an issue before larger
changes to agree on the approach. Contributions use [Apache-2.0](LICENSE).

## Development setup

```bash
git clone https://github.com/nikhila9arwal/cloudsweep
cd cloudsweep
python3 -m venv .venv
. .venv/bin/activate
pip install -e . pyyaml cfn-lint
python3 -m unittest discover -s tests
cfn-lint src/cloudsweep/stack.yaml
```

Tests run offline: they stub AWS, exercise the worker against a local store, and check S3
signatures against known values. Run `cfn-lint` after template changes. The
[example project](examples/cache-sim/) exercises image building and local execution with Docker
or Podman, without AWS.

## Code

- Keep runtime Python dependencies to the standard library; the CLI also uses the AWS CLI.
- `core.py`, `worker.py` and `s3lite.py` go into users' images and must support Python 3.8.
- Refusals should explain what to do next.
- Permission changes belong in `stack.yaml`, with tests in `tests/test_cli.py` and matching
  [permissions documentation](docs/how-it-works.md#permissions).
- Include a test with each fix and update documentation when behavior changes.

## AWS validation

Before releasing deployment, submission or guard changes, use a separate `--prefix`: deploy,
run the example sweep, cancel, then [retire](docs/admin.md#retire). This costs money; see
[costs and limits](docs/admin.md#costs-and-limits). `retire --yes --delete-data` permanently deletes
test results. Remove personal information and account/resource identifiers from shared output.
