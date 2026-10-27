#!/usr/bin/env python3
"""
Writes the samples that could NOT be parsed into txt files, separately for every
dataset / view / split, using the <split>_manifest.jsonl files written by run_graphs.py.

Not parsed =
  status "failed"   : every attempt (C and C++) timed out / crashed / errored, no graph
  status "fallback" : every attempt only produced ATLAS's plain line-by-line fallback graph

Per view folder  output/<dataset>/<view>/not_parsed/  it writes:
  not_parsed_<split>.txt       tab-separated details
  not_parsed_<split>_ids.txt   only the sample ids
  summary.txt                  ok / fallback / failed counts per split

Usage:
  python scripts/list_not_parsed.py                       # dataset primevul, all views
  python scripts/list_not_parsed.py --dataset devign --view ast+cfg
  python scripts/list_not_parsed.py --only-failed
(run_graphs.py also calls this automatically after every split.)
"""

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ALL_VIEWS = ["ast", "cfg", "sdfg", "ast+cfg", "ast+sdfg", "cfg+sdfg", "ast+cfg+sdfg"]
COLUMNS = ["id", "label", "status", "lang_guess", "file_name", "project", "cve", "cwe", "attempts"]


def view_folder(view: str) -> str:
    return view.replace("+", "_")


def summarize(attempts):
    parts = []
    for a in attempts or []:
        parts.append(f"{a.get('lang', '-')}:{a.get('result', '?')}({a.get('sec', '?')}s)")
    return " | ".join(parts)


def _sort_key(sid):
    s = str(sid)
    return (0, int(s), s) if s.lstrip("-").isdigit() else (1, 0, s)


def read_manifest(path: Path) -> dict:
    """manifest is append-only (re-runs add rows): keep the LAST row per id."""
    last = {}
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                row = json.loads(line)
                row_id = row.get("id", row.get("idx"))
                if row_id is not None:
                    last[str(row_id)] = row
    return last


def write_reports(view_dir: Path, only_failed: bool = False, quiet: bool = False) -> dict:
    """Writes the txt files for one view folder. Returns {split: counts}."""
    view_dir = Path(view_dir)
    bad_status = {"failed"} if only_failed else {"failed", "fallback"}
    report_dir = view_dir / "not_parsed"
    summary, lines = {}, []

    manifests = [
        (manifest, manifest.name[: -len("_manifest.jsonl")])
        for manifest in sorted(view_dir.glob("*_manifest.jsonl"))
    ]
    if not manifests:
        manifests = [
            (manifest, manifest.parent.name)
            for manifest in sorted(view_dir.glob("*/manifest.jsonl"))
        ]

    for manifest, split in manifests:
        rows = read_manifest(manifest)
        counts = {"ok": 0, "fallback": 0, "failed": 0}
        for r in rows.values():
            counts[r.get("status", "failed")] = counts.get(r.get("status", "failed"), 0) + 1
        summary[split] = counts

        bad = sorted(
            (r for r in rows.values() if r.get("status") in bad_status),
            key=lambda r: _sort_key(r.get("id", r.get("idx"))),
        )
        report_dir.mkdir(parents=True, exist_ok=True)
        with open(report_dir / f"not_parsed_{split}.txt", "w", encoding="utf-8") as fd, \
             open(report_dir / f"not_parsed_{split}_ids.txt", "w", encoding="utf-8") as fi:
            fd.write("\t".join(COLUMNS) + "\n")
            for r in bad:
                m = r.get("meta") or {}
                row_id = r.get("id", r.get("idx"))
                vals = [row_id, r.get("label", r.get("target")), r.get("status"),
                        r.get("lang_guess"),
                        m.get("file_name"), m.get("project"), m.get("cve"), m.get("cwe"),
                        summarize(r.get("attempts"))]
                fd.write("\t".join("" if v is None else str(v).replace("\t", " ").replace("\n", " ")
                                   for v in vals) + "\n")
                fi.write(f"{row_id}\n")
        lines.append(f"{split}: total={len(rows)} ok={counts['ok']} fallback={counts['fallback']} "
                     f"failed={counts['failed']} -> not_parsed_listed={len(bad)}")

    if lines:
        with open(report_dir / "summary.txt", "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        if not quiet:
            for l in lines:
                print(f"  [{view_dir.name}] {l}")
    return summary


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-dir", default=str(ROOT / "output"))
    ap.add_argument("--dataset", default="primevul", help="output sub-folder name used by run_graphs.py (--name)")
    ap.add_argument("--view", default="all", help="'all', or one/more of %s (comma separated)" % ALL_VIEWS)
    ap.add_argument("--only-failed", action="store_true", help="list only status=failed (skip fallback)")
    args = ap.parse_args()

    views = ALL_VIEWS if args.view == "all" else [v.strip().lower() for v in args.view.split(",")]
    base = Path(args.out_dir) / args.dataset
    if not base.exists() and args.dataset == "primevul":
        legacy_base = Path(args.out_dir) / "graphs"
        if legacy_base.exists():
            base = legacy_base
    found = False
    for v in views:
        d = base / view_folder(v)
        if d.exists():
            found = True
            write_reports(d, only_failed=args.only_failed)
    if not found:
        print(f"No view folders found under {base}")


if __name__ == "__main__":
    main()