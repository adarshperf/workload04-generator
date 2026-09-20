# baseline/

This directory holds the **frozen baseline metadata** written by
`init-baseline` -- never the original 10,000-file Workload04 corpus itself.

- `baseline_manifest.json` -- header facts (entry counts, EICAR count/ratio,
  size statistics, paths the corpus was read from at freeze time).
- `baseline_records.csv` -- one row per unique baseline URI (extension,
  MIME, family, size, weight, existence/validity, EICAR flag).
- `baseline_resource_order.txt` -- the full original resource-list line
  order (with repeats), used to reproduce the exact original interleaving
  during generation.

These files are produced by, and only by:

```
python workload04_generator.py init-baseline \
    --resource-list /path/to/workload04-resources.txt \
    --source-root /path/to/workload04
```

`init-baseline` is strictly read-only against the original corpus -- it
never writes into `--source-root` or `--resource-list`.

The original golden Workload04 corpus (the actual 10,000 physical payload
files + `workload04-resources.txt`) is an EXTERNAL input to this tool. It is
intentionally not bundled in this package -- keep it wherever is convenient
on the target machine (e.g. `/opt/workload04` + `/opt/load/workload04-resources.txt`,
which `init-baseline` also checks automatically if `--source-root`/
`--resource-list` are omitted) and point `init-baseline` at it once per
machine.
