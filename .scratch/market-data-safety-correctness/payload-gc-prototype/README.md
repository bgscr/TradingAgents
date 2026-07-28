# PROTOTYPE ONLY — payload GC concurrency

This throwaway logic prototype answers one question: which minimal protocol can
preserve `live payload row exists => canonical payload file exists` when a
publisher and one or two garbage collectors use independent SQLite
connections and may republish the same digest at every boundary between row
deletion and file unlink?

It does not import or modify production code. Every scenario creates its own
temporary SQLite databases and temporary payload directory, coordinates
interleavings with `threading.Event` barriers (never sleeps), records the full
state, and deletes the temporary state when the scenario ends.

Run from the repository root:

```text
python .scratch/market-data-safety-correctness/payload-gc-prototype/run_prototype.py
```

The command writes `RESULTS.md` and `results.json` beside this README.

