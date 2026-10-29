# Example: a toy cache simulator

`sim.c` simulates an LRU set-associative cache on a random address stream and writes
`result.json` to its `--out` directory. The files cloudsweep needs:

- [`cloudsweep.ini`](cloudsweep.ini): build with `make`; `{bin}` is `build/cachesim`; the
  acceptance run must reproduce [`tests/reference.json`](tests/reference.json) exactly.
- [`cloud/Dockerfile`](cloud/Dockerfile): Debian with gcc, make, git and python3.
- [`configs/make_sweep.py`](configs/make_sweep.py): writes a 24-point sweep (3 set counts × 4
  associativities × 2 seeds) that reads one trace file as an input.

Copy this directory somewhere outside the cloudsweep checkout and make it a repository:

```bash
cp -r examples/cache-sim ~/cache-sim && cd ~/cache-sim
git init && git add -A && git commit -m "Cache simulator"
mkdir -p traces && echo 6000 > traces/footprint.txt   # the "trace": untracked data, passed with --input
python3 configs/make_sweep.py                          # writes sweep.jsonl

cloudsweep image build        # builds the image, checks it, runs the acceptance comparison
cloudsweep try --commands sweep.jsonl --input traces/footprint.txt

# with a deployment (cloudsweep deploy --email ...):
cloudsweep image publish
cloudsweep submit --commands sweep.jsonl --input traces/footprint.txt
cloudsweep fetch <submission> --wait
```

Each command takes well under a second, so on AWS this sweep costs almost nothing beyond instance
start-up. The `[run] hours = 0.25` limit makes each command's worst-case estimate a quarter of an
instance-hour.
