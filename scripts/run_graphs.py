#!/usr/bin/env python3
"""
Generic ATLAS graph runner (replaces run_primevul_graphs.py).

For every dataset split (train / valid / test) and every view (7 ablation views) it:
  * builds the ATLAS graph with RobustAtlasExtractor  (5 min timeout, C <-> C++ switching)
  * writes the SAMPLE + GRAPH as JSON Lines:   output/<name>/<view>/<split>.jsonl
  * writes a light manifest (no graph):        output/<name>/<view>/<split>_manifest.jsonl
  * writes the not-parsed samples as txt:      output/<name>/<view>/not_parsed/*.txt
Resumable: ids already in the manifest are skipped.

One JSONL line =
  {"dataset","split","view","id","lang","label","code","meta":{all other input fields},
   "num_nodes","num_edges","graph":{"nodes":[{"id","attrs"}],"edges":[{"source","target","key","attrs"}]}}

Examples
  # PrimeVul paired, all 7 views, train+valid+test
  python scripts/run_graphs.py --dataset primevul --view all --workers 8

  # quick test
  python scripts/run_graphs.py --dataset primevul --view ast+cfg+sdfg --splits test --limit 20 --workers 2

  # any other dataset with ready-made splits (jsonl / json / csv)
  python scripts/run_graphs.py --dataset devign --code-field func --label-field target --id-field idx \
      --split-files train=data/devign/train.jsonl valid=data/devign/valid.jsonl test=data/devign/test.jsonl

  # a dataset with ONE file and no splits: random 80/10/10 split
  python scripts/run_graphs.py --dataset diversevul --code-field func --label-field target \
      --split-files all=data/diversevul/diversevul.json --split-ratio 0.8,0.1,0.1
"""

import argparse
import csv
import json
import queue
import random
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src" / "graph"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from atlas_graph_extractor import RobustAtlasExtractor, AtlasGraphExtractor  # noqa: E402
from list_not_parsed import write_reports, view_folder  # noqa: E402

ALL_VIEWS = list(AtlasGraphExtractor.SUPPORTED_VIEWS)

PRESETS = {
    "primevul": {
        "files": {"train": "primevul_train_paired.jsonl",
                  "valid": "primevul_valid_paired.jsonl",
                  "test": "primevul_test_paired.jsonl"},
        "id_field": "idx", "code_field": "func", "label_field": "target", "file_field": "file_name",
    },
    "primevul-unpaired": {
        "files": {"train": "primevul_train.jsonl",
                  "valid": "primevul_valid.jsonl",
                  "test": "primevul_test.jsonl"},
        "id_field": "idx", "code_field": "func", "label_field": "target", "file_field": "file_name",
    },
}


# ------------------------------------------------------------------ dataset reading
def load_records(path: Path):
    """Reads .jsonl, .json (array or json-lines) or .csv into a list of dicts."""
    if path.suffix.lower() == ".csv":
        csv.field_size_limit(sys.maxsize)
        with open(path, "r", encoding="utf-8", newline="") as f:
            return list(csv.DictReader(f))
    with open(path, "r", encoding="utf-8") as f:
        first = ""
        while True:
            ch = f.read(1)
            if not ch or not ch.isspace():
                first = ch
                break
        f.seek(0)
        if first == "[":
            return json.load(f)
        return [json.loads(line) for line in f if line.strip()]


def resolve_sources(args):
    """-> (field config, {split: Path})"""
    cfg = dict(PRESETS.get(args.dataset, {}))
    data_dir = Path(args.data_dir) if args.data_dir else ROOT / "data" / args.dataset

    if args.split_files:
        files = {}
        for item in args.split_files:
            if "=" not in item:
                sys.exit(f"--split-files expects name=path, got: {item}")
            name, p = item.split("=", 1)
            p = Path(p)
            files[name.strip()] = p if p.exists() else data_dir / p
    elif "files" in cfg:
        files = {k: data_dir / v for k, v in cfg["files"].items()}
    else:
        sys.exit(f"Unknown dataset '{args.dataset}': give --split-files (and the field options).")

    fields = {
        "id_field": args.id_field or cfg.get("id_field", "idx"),
        "code_field": args.code_field or cfg.get("code_field", "func"),
        "label_field": args.label_field or cfg.get("label_field", "target"),
        "file_field": args.file_field or cfg.get("file_field", "file_name"),
    }
    return fields, files


def build_splits(args, files):
    """-> {split: callable returning list of records}"""
    if "all" in files:
        ratios = [float(x) for x in args.split_ratio.split(",")]
        if len(ratios) != 3 or abs(sum(ratios) - 1) > 1e-6:
            sys.exit("--split-ratio must be three numbers summing to 1, e.g. 0.8,0.1,0.1")
        cache = {}

        def make(name):
            def f():
                if not cache:
                    recs = load_records(files["all"])
                    for i, r in enumerate(recs):
                        r.setdefault("__row", i)
                    random.Random(args.seed).shuffle(recs)
                    a = int(len(recs) * ratios[0])
                    b = a + int(len(recs) * ratios[1])
                    cache.update(train=recs[:a], valid=recs[a:b], test=recs[b:])
                return cache[name]
            return f
        return {n: make(n) for n in ("train", "valid", "test")}
    return {n: (lambda p=p: load_records(p)) for n, p in files.items()}


# ------------------------------------------------------------------ helpers
def read_done(manifest: Path, retry_unparsed: bool):
    """ids already processed (last manifest row per id wins)."""
    last = {}
    if manifest.exists():
        with open(manifest, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    r = json.loads(line)
                    last[str(r["id"])] = r.get("status")
    if retry_unparsed:
        return {k for k, s in last.items() if s == "ok"}
    return set(last)


# ------------------------------------------------------------------ main
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dataset", default="primevul", help="primevul | primevul-unpaired | any name (needs --split-files)")
    ap.add_argument("--name", default=None, help="output folder name (default: dataset)")
    ap.add_argument("--data-dir", default=None, help="default: data/<dataset>")
    ap.add_argument("--out-dir", default=str(ROOT / "output"))
    ap.add_argument("--split-files", nargs="+", default=None, help="name=path pairs, e.g. train=a.jsonl valid=b.jsonl test=c.jsonl")
    ap.add_argument("--split-ratio", default="0.8,0.1,0.1", help="used only with all=<file>")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--id-field", default=None)
    ap.add_argument("--code-field", default=None)
    ap.add_argument("--label-field", default=None)
    ap.add_argument("--file-field", default=None, help="field holding the source file name (used to guess C vs C++)")
    ap.add_argument("--meta-fields", default=None, help="comma list of input fields to keep in 'meta' (default: all)")
    ap.add_argument("--splits", nargs="+", default=["train", "valid", "test"])
    ap.add_argument("--view", default="all", help="'all' or comma list of: %s" % ALL_VIEWS)
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--timeout", type=int, default=300, help="seconds per parse attempt (default 300 = 5 min)")
    ap.add_argument("--rounds", type=int, default=1, help="cycles of [guessed language, other language]")
    ap.add_argument("--limit", type=int, default=None, help="first N samples per split (testing)")
    ap.add_argument("--recursion-limit", type=int, default=None)
    ap.add_argument("--include-unparsed", action="store_true",
                    help="also write fallback/failed samples to <split>.jsonl with graph=null")
    ap.add_argument("--retry-unparsed", action="store_true",
                    help="re-process samples that were fallback/failed in an earlier run")
    ap.add_argument("--only-failed", action="store_true", help="txt reports: list only status=failed")
    args = ap.parse_args()

    views = ALL_VIEWS if args.view == "all" else [v.strip().lower() for v in args.view.split(",")]
    for v in views:
        if v not in ALL_VIEWS:
            sys.exit(f"Unsupported view '{v}'. Choose from {ALL_VIEWS}")

    fields, files = resolve_sources(args)
    sources = build_splits(args, files)
    name = args.name or args.dataset
    meta_keep = [m.strip() for m in args.meta_fields.split(",")] if args.meta_fields else None

    pool = queue.Queue()
    extractors = [RobustAtlasExtractor(timeout=args.timeout, rounds=args.rounds,
                                       recursion_limit=args.recursion_limit) for _ in range(args.workers)]
    for e in extractors:
        pool.put(e)

    try:
        for view in views:
            view_dir = Path(args.out_dir) / name / view_folder(view)
            view_dir.mkdir(parents=True, exist_ok=True)
            for split in args.splits:
                if split not in sources:
                    print(f"[skip] split '{split}' not configured for {name}")
                    continue
                path_hint = files.get(split)
                if path_hint is not None and "all" not in files and not Path(path_hint).exists():
                    print(f"[skip] missing file {path_hint}")
                    continue

                records = sources[split]()
                if args.limit:
                    records = records[: args.limit]
                jsonl_path = view_dir / f"{split}.jsonl"
                manifest_path = view_dir / f"{split}_manifest.jsonl"
                done = read_done(manifest_path, args.retry_unparsed)

                todo = []
                for i, rec in enumerate(records):
                    sid = rec.get(fields["id_field"], rec.get("__row", i))
                    if str(sid) not in done:
                        todo.append((sid, rec))
                print(f"[{name}/{view}/{split}] total={len(records)} todo={len(todo)} workers={args.workers}")

                counts = {"ok": 0, "fallback": 0, "failed": 0}
                lock = threading.Lock()
                try:
                    from tqdm import tqdm
                    bar = tqdm(total=len(todo), desc=f"{view}/{split}")
                except ImportError:
                    bar = None

                with open(jsonl_path, "a", encoding="utf-8") as jf, \
                     open(manifest_path, "a", encoding="utf-8") as mf:

                    def work(item):
                        sid, rec = item
                        code = rec.get(fields["code_field"])
                        fname = rec.get(fields["file_field"])
                        ex = pool.get()
                        try:
                            res = ex.extract_robust(code, view=view, file_name=fname)
                        except Exception as e:  # keep the run alive
                            res = {"status": "failed", "lang_guess": None, "lang_used": None, "graph": None,
                                   "total_sec": 0, "attempts": [{"result": "runner_error", "detail": repr(e)[:200]}]}
                        finally:
                            pool.put(ex)

                        label = rec.get(fields["label_field"])
                        meta_all = {k: v for k, v in rec.items() if k not in (fields["code_field"], "__row")}
                        meta = {k: meta_all.get(k) for k in meta_keep} if meta_keep else meta_all
                        g = res["graph"]
                        out_line = None
                        if res["status"] == "ok" or args.include_unparsed:
                            out_line = json.dumps(default=str, obj={
                                "dataset": name, "split": split, "view": view, "id": sid,
                                "status": res["status"], "lang": res["lang_used"], "label": label,
                                "code": code, "meta": meta,
                                "num_nodes": g["num_nodes"] if g else 0,
                                "num_edges": g["num_edges"] if g else 0,
                                "graph": g if res["status"] == "ok" else None,
                            })
                        brief = {k: meta_all.get(k) for k in ("file_name", "project", "cve", "cwe")}
                        brief["file_name"] = brief["file_name"] or fname
                        mrow = json.dumps(default=str, obj={
                            "id": sid, "label": label, "status": res["status"],
                            "lang_guess": res["lang_guess"], "lang_used": res["lang_used"],
                            "attempts": res["attempts"], "total_sec": res["total_sec"],
                            "num_nodes": g["num_nodes"] if g else 0,
                            "num_edges": g["num_edges"] if g else 0, "meta": brief,
                        })
                        with lock:
                            if out_line:
                                jf.write(out_line + "\n")
                                jf.flush()
                            mf.write(mrow + "\n")
                            mf.flush()
                            counts[res["status"]] += 1
                            if bar is not None:
                                bar.update(1)
                            elif sum(counts.values()) % 50 == 0:
                                print(f"  {sum(counts.values())}/{len(todo)} {counts}")

                    with ThreadPoolExecutor(args.workers) as tp:
                        list(tp.map(work, todo))
                if bar is not None:
                    bar.close()
                print(f"[{name}/{view}/{split}] done {counts}")
                write_reports(view_dir, only_failed=args.only_failed, quiet=True)
            print(f"[{name}/{view}] not-parsed txt files -> {view_dir / 'not_parsed'}")
    except KeyboardInterrupt:
        print("\nInterrupted. Run the same command again to resume.")
    finally:
        for e in extractors:
            e.close()


if __name__ == "__main__":
    main()