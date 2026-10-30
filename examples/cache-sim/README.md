# Example: a toy cache simulator

`sim.c` models an LRU set-associative cache on a random address stream and writes `result.json`
to its `--out` directory.

- [`cloudsweep.ini`](cloudsweep.ini) builds with `make`; `{bin}` is `build/cachesim`. Acceptance
  requires an exact match with [`tests/reference.json`](tests/reference.json).
- [`cloud/Dockerfile`](cloud/Dockerfile) uses Debian with gcc, make and the
  [worker prerequisites](../../docs/project.md#toolchain-image).
- [`configs/make_sweep.py`](configs/make_sweep.py) generates 24 commands: 3 set counts × 4
  associativities × 2 seeds, using one input file.

[Install cloudsweep](../../README.md#install), then from its checkout:

```bash
cp -R examples/cache-sim ../cache-sim
cd ../cache-sim
git init
git add .
git commit -m "Cache simulator"
mkdir -p traces
printf '6000\n' > traces/footprint.txt
python3 configs/make_sweep.py
cloudsweep image build
cloudsweep try --commands sweep.jsonl --input traces/footprint.txt
```

The untracked input sets the address footprint. Each command has a 15-minute time limit.
To run on AWS, configure your default administrator profile and review
[costs and limits](../../docs/admin.md#costs-and-limits):

```bash
read -r -p "Budget alert email: " CLOUDSWEEP_ALERT_EMAIL
cloudsweep deploy --email "$CLOUDSWEEP_ALERT_EMAIL"
cloudsweep image publish
cloudsweep submit --commands sweep.jsonl --input traces/footprint.txt
read -r -p "Submission ID printed above: " CLOUDSWEEP_SUBMISSION
cloudsweep fetch "$CLOUDSWEEP_SUBMISSION" --wait
```

Use results only after `complete`. AWS resources cost money; follow the
[cleanup instructions](../../docs/admin.md#retire) when finished.
