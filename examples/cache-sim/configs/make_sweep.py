"""Write sweep.jsonl: one command per (sets, ways, seed) point, for `cloudsweep submit --commands`."""
import itertools
import json

with open("sweep.jsonl", "w") as stream:
    for sets, ways, seed in itertools.product((64, 256, 1024), (1, 2, 4, 8), (1, 2)):
        stream.write(json.dumps(["{bin}", "--sets", str(sets), "--ways", str(ways), "--seed", str(seed),
                                 "--trace", "{input:traces/footprint.txt}", "--out", "{out}"]) + "\n")
print("wrote sweep.jsonl")
