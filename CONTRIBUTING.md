# Contributing

Bug reports, fixes and documentation improvements are welcome. For a larger change, open an issue
first to agree on the approach.

## Development setup

```bash
git clone https://github.com/nikhila9arwal/cloudsweep && cd cloudsweep
python3 -m venv .venv && . .venv/bin/activate
pip install -e . pyyaml cfn-lint
python3 -m unittest discover -s tests      # offline; no AWS account needed
cfn-lint src/cloudsweep/stack.yaml         # after any change to the template
```

The tests replace AWS with stubs. The worker tests run the real worker against a local store, and
the S3 client's signatures are checked against known-good values.

To try a change end to end without AWS, use the [example project](examples/cache-sim/):
`cloudsweep image build` and `cloudsweep try` run everything locally in Docker or Podman.

## Code

- No runtime dependencies. The CLI uses the Python standard library and the AWS CLI.
- `core.py`, `worker.py` and `s3lite.py` are copied into users' images. They must run on
  Python 3.8 with the standard library only.
- Every command refuses with a message that says what to do instead. Keep that true for new
  failure cases.
- A change to what the agent role or the jobs can do belongs in `stack.yaml`, with a test in
  `tests/test_cli.py` and an update to [How it works](docs/how-it-works.md#permissions).
- Add a test with each fix. Update the documentation in the same change when behavior changes.

## Testing against AWS

A change to deployment, submission or the guard should also be run against a real account before
release: `deploy` with a new `--prefix`, the example sweep, `cancel`, and
`admin retire --yes --delete-data`. That cycle costs well under a dollar. Remove account IDs and
resource names from any output you paste into an issue or pull request.
