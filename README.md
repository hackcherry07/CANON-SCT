# CANON-SCT: ATLAS graphs -> JSON (all 7 views, train/valid/test, any dataset)

| File | Purpose |
|---|---|
| `src/graph/atlas_graph_extractor.py` | Original extractor (unchanged) + added section: 5 min timeout, C<->C++ switching, fallback detection, JSON export |
| `scripts/run_graphs.py` | Generic runner (replaces `run_primevul_graphs.py`) |
| `scripts/list_not_parsed.py` | Writes not-parsed samples to txt, per dataset / view / split |

## Output layout

```
output/<dataset>/<view>/            view = ast | cfg | sdfg | ast_cfg | ast_sdfg | cfg_sdfg | ast_cfg_sdfg
├── train.jsonl   valid.jsonl   test.jsonl            one JSON object per line = sample + graph (parsed OK only)
├── train_manifest.jsonl ...                          status / language / attempts / timing (no graph)
└── not_parsed/
    ├── not_parsed_train.txt  not_parsed_train_ids.txt
    ├── not_parsed_valid.txt  not_parsed_valid_ids.txt
    ├── not_parsed_test.txt   not_parsed_test_ids.txt
    └── summary.txt
```

One line of `<split>.jsonl`:

```json
{"dataset":"primevul","split":"train","view":"ast+cfg+sdfg","id":123,"status":"ok","lang":"c",
 "label":1,"code":"...function source...",
 "meta":{"idx":123,"project":"...","commit_id":"...","cve":"...","cwe":[...],"file_name":"...", "...all other input fields..."},
 "num_nodes":2962,"num_edges":3643,
 "graph":{"num_nodes":..,"num_edges":..,
          "nodes":[{"id":1,"attrs":{"label":"1_ int x","node_type":"...","line_no":1}}],
          "edges":[{"source":1,"target":2,"key":0,"attrs":{"edge_type":"CFG_edge"}}]}}
```

Line order inside a file is not sorted (parallel workers); use `id`.

## Commands (VS Code integrated terminal, zsh, env `canon-sct`)

```bash
cd ~/CANON-SCT && conda activate canon-sct

# 20-sample test
python scripts/run_graphs.py --dataset primevul --view ast+cfg+sdfg --splits test --limit 20 --workers 2

# PrimeVul paired: all 7 views, train+valid+test
caffeinate -i python scripts/run_graphs.py --dataset primevul --view all --workers 8

# only some views
python scripts/run_graphs.py --dataset primevul --view ast,cfg,ast+cfg+sdfg --workers 8

# not-parsed txt files again / only failed
python scripts/list_not_parsed.py --dataset primevul --view all
python scripts/list_not_parsed.py --dataset primevul --only-failed
```

Stop with Ctrl+C, run the same command to resume. `--retry-unparsed` re-processes earlier fallback/failed samples.
`--include-unparsed` also writes them to the jsonl with `"graph": null`.

## Other datasets

Any `.jsonl`, `.json` (array or json-lines) or `.csv` file. Tell the runner the field names:

```bash
# ready-made splits
python scripts/run_graphs.py --dataset devign --code-field func --label-field target --id-field idx \
  --split-files train=data/devign/train.jsonl valid=data/devign/valid.jsonl test=data/devign/test.jsonl

# one file, random 80/10/10 split (seeded)
python scripts/run_graphs.py --dataset diversevul --code-field func --label-field target \
  --split-files all=data/diversevul/diversevul.json --split-ratio 0.8,0.1,0.1 --seed 42
```

`--file-field` names the column with the source file name (used to guess C vs C++; without it the code is inspected).
`--meta-fields a,b,c` keeps only those input fields in `meta` (default: all except the code).
`--name` changes the output folder name. Built-in presets: `primevul`, `primevul-unpaired`.

## Logic per sample

1. Guess language: `.c` -> C, `.cc/.cpp/...` -> C++, `.h`/unknown -> look for C++ syntax in the code.
2. Try it (max `--timeout` seconds, default 300). If it times out, crashes or only gives ATLAS's plain fallback graph, try the other language (`--rounds` repeats the cycle).
3. Real graph -> `ok` (saved in jsonl). Only fallback graphs -> `fallback`. Nothing usable -> `failed`. Both go to the not-parsed txt files.

Worst case per sample per view is about 10 min (two timeouts).