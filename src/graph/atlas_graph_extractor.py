"""
CANON-SCT: ATLAS Multi-View Graph Extractor
Wraps ATLAS to generate individual, paired, and full multi-view graphs from C/C++ code.

Supports Ablation Configurations:
- 1-View: 'ast', 'cfg', 'sdfg'
- 2-View: 'ast+cfg', 'ast+sdfg', 'cfg+sdfg'
- 3-View: 'ast+cfg+sdfg' (Full CANON-SCT)
"""

import os
import sys
from typing import Dict, List, Optional, Tuple
import networkx as nx

# Add ATLAS source to sys.path
ATLAS_SRC = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "ATLAS-multi-view-code-representation-tool", "src")
)
if ATLAS_SRC not in sys.path:
    sys.path.insert(0, ATLAS_SRC)

try:
    from atlas.codeviews.combined_graph.combined_driver import CombinedDriver
except ImportError as e:
    raise ImportError(f"Failed to import ATLAS from {ATLAS_SRC}: {e}")


class AtlasGraphExtractor:
    SUPPORTED_VIEWS = [
        "ast", "cfg", "sdfg",
        "ast+cfg", "ast+sdfg", "cfg+sdfg",
        "ast+cfg+sdfg"
    ]

    def __init__(self, default_lang: str = "c"):
        self.default_lang = default_lang

    def extract_graph(
        self,
        code: str,
        lang: Optional[str] = None,
        view: str = "ast+cfg+sdfg"
    ) -> nx.MultiDiGraph:
        """
        Generates a customized graph for the given code snippet according to the specified ablation view.
        Args:
            code: Source code string
            lang: 'c' or 'cpp'
            view: One of ['ast', 'cfg', 'sdfg', 'ast+cfg', 'ast+sdfg', 'cfg+sdfg', 'ast+cfg+sdfg']
        """
        src_lang = (lang or self.default_lang).lower()
        if src_lang not in ["c", "cpp"]:
            src_lang = "c"

        view_lower = view.lower().strip()
        include_ast = "ast" in view_lower
        include_cfg = "cfg" in view_lower
        include_sdfg = "sdfg" in view_lower or "dfg" in view_lower

        # At least one must be enabled
        if not (include_ast or include_cfg or include_sdfg):
            include_ast, include_cfg, include_sdfg = True, True, True

        codeviews = {
            "AST": {
                "exists": include_ast,
                "collapsed": False,
                "minimized": False,
                "blacklisted": []
            },
            "CFG": {
                "exists": include_cfg
            },
            "DFG": {
                "exists": include_sdfg,
                "collapsed": False,
                "minimized": False,
                "statements": True,
                "last_def": False,
                "last_use": False
            },
        }

        try:
            driver = CombinedDriver(
                src_language=src_lang,
                src_code=code,
                codeviews=codeviews
            )
            graph = driver.get_graph()
            self._annotate_line_numbers(graph, code)
            return graph
        except Exception:
            # Fallback: create a structured sequential fallback graph
            fallback_g = nx.MultiDiGraph()
            lines = code.split("\n")
            for idx, line in enumerate(lines, start=1):
                fallback_g.add_node(
                    idx,
                    label=line.strip(),
                    line_no=idx,
                    node_type="statement"
                )
                if idx > 1:
                    edge_type = "CFG_edge" if include_cfg else ("DFG_edge" if include_sdfg else "AST_edge")
                    fallback_g.add_edge(idx - 1, idx, edge_type=edge_type)
            return fallback_g

    def _annotate_line_numbers(self, graph: nx.MultiDiGraph, code: str):
        """Annotates nodes with 1-indexed source line numbers."""
        lines = code.split("\n")
        for node_id, data in graph.nodes(data=True):
            label = str(data.get("label", ""))
            line_no = None

            # ATLAS prefixes statement nodes with line numbers like '4_ if (x)'
            if "_" in label:
                prefix = label.split("_")[0]
                if prefix.isdigit():
                    line_no = int(prefix)

            if line_no is None:
                stripped_label = label.strip()
                if stripped_label:
                    for idx, line in enumerate(lines, start=1):
                        if stripped_label in line:
                            line_no = idx
                            break

            data["line_no"] = line_no or 1


# =============================================================================
# ADDED SECTION  (everything above this line is the original code, unchanged)
#
#   * per-attempt TIMEOUT (default 300 s = 5 min) using a killable worker process
#   * C <-> C++ switching: guess language, try it, then try the other one
#   * detection of the extractor's plain "fallback" graph (treated as a failed attempt)
#   * JSON export of graphs (graph_to_dict)
#
# Main entry point:  RobustAtlasExtractor.extract_robust(...)
# =============================================================================
import json
import multiprocessing as _mp
from multiprocessing.connection import Connection
import time

DEFAULT_TIMEOUT_SEC = 300          # 5 minutes per attempt
C_EXTS = (".c",)
CPP_EXTS = (".cc", ".cpp", ".cxx", ".c++", ".hpp", ".hh", ".hxx", ".ipp", ".tcc")
_CPP_HINTS = (
    "::", "template<", "template <", "namespace ", "std::", "nullptr",
    "static_cast", "dynamic_cast", "reinterpret_cast", "const_cast",
    "public:", "private:", "protected:", "catch (", "catch(",
)

_spawn = _mp.get_context("spawn")  # same behaviour on macOS / Linux / Windows


def guess_language(file_name: Optional[str] = None, code: str = "") -> str:
    """'c' or 'cpp' from the file extension; falls back to simple C++ hints in the code."""
    name = (file_name or "").lower()
    if name.endswith(C_EXTS):
        return "c"
    if name.endswith(CPP_EXTS):
        return "cpp"
    # '.h', unknown extension or no file name -> look at the code itself
    return "cpp" if any(h in (code or "") for h in _CPP_HINTS) else "c"


def other_language(lang: str) -> str:
    return "cpp" if lang == "c" else "c"


def is_fallback_graph(graph: nx.MultiDiGraph) -> bool:
    """
    True if `graph` is the plain line-by-line graph that extract_graph() returns when ATLAS
    raised an error: every node only has label / line_no / node_type and node_type == 'statement'.
    An empty graph also counts as a failure.
    """
    if graph.number_of_nodes() == 0:
        return True
    keys = set()
    for _, d in graph.nodes(data=True):
        if d.get("node_type") != "statement":
            return False
        keys |= set(d.keys())
    return keys == {"label", "line_no", "node_type"}


def _jsonable(v):
    if v is None or isinstance(v, (str, int, float, bool)):
        return v
    if isinstance(v, (list, tuple, set, frozenset)):
        return [_jsonable(x) for x in v]
    if isinstance(v, dict):
        return {str(k): _jsonable(x) for k, x in v.items()}
    return str(v)


def graph_to_dict(graph: nx.MultiDiGraph) -> dict:
    """JSON-serialisable form of a graph. All node / edge attributes are kept under 'attrs'."""
    return {
        "num_nodes": graph.number_of_nodes(),
        "num_edges": graph.number_of_edges(),
        "nodes": [{"id": _jsonable(n), "attrs": _jsonable(d)} for n, d in graph.nodes(data=True)],
        "edges": [
            {"source": _jsonable(u), "target": _jsonable(v), "key": _jsonable(k), "attrs": _jsonable(d)}
            for u, v, k, d in graph.edges(keys=True, data=True)
        ],
    }


def _worker_main(conn, recursion_limit=None):
    """Runs in a child process: receives (code, lang, view), sends back a JSON-able graph."""
    if recursion_limit:
        sys.setrecursionlimit(recursion_limit)
    try:
        extractor = AtlasGraphExtractor()
        conn.send(("ready", None))
    except BaseException as e:  # noqa
        conn.send(("init_error", repr(e)))
        return
    while True:
        try:
            msg = conn.recv()
        except EOFError:
            return
        if msg is None:
            return
        code, lang, view = msg
        try:
            g = extractor.extract_graph(code, lang=lang, view=view)   # original method
            conn.send(("ok", {"graph": graph_to_dict(g), "fallback": is_fallback_graph(g)}))
        except BaseException as e:  # noqa
            conn.send(("error", repr(e)))


class GraphWorker:
    """One long-lived worker process. The parent enforces the timeout and kills/restarts the worker."""

    def __init__(self, recursion_limit: Optional[int] = None):
        self.recursion_limit = recursion_limit
        self.proc = None
        self.conn: Optional[Connection] = None

    def _start(self):
        self.conn, child = _spawn.Pipe()
        self.proc = _spawn.Process(target=_worker_main, args=(child, self.recursion_limit), daemon=True)
        self.proc.start()
        child.close()
        if not self.conn.poll(180):
            self.kill()
            raise RuntimeError("worker did not start within 180 s")
        try:
            tag, val = self.conn.recv()
        except EOFError:
            self.kill()
            raise RuntimeError("worker died while starting (is ATLAS importable?)")
        if tag != "ready":
            self.kill()
            raise RuntimeError(f"worker init error: {val}")

    def kill(self):
        try:
            if self.proc is not None and self.proc.is_alive():
                self.proc.kill()
                self.proc.join(5)
        except Exception:
            pass
        try:
            if self.conn is not None:
                self.conn.close()
        except Exception:
            pass
        self.proc = None
        self.conn = None

    def run(self, code: str, lang: str, view: str, timeout: float):
        """Returns (tag, value); tag in ok | error | timeout | crash."""
        if self.proc is None or not self.proc.is_alive():
            self.kill()
            self._start()
        conn = self.conn
        if conn is None:
            raise RuntimeError("worker connection is not available")
        try:
            conn.send((code, lang, view))
            if not conn.poll(timeout):
                self.kill()
                return "timeout", None
            return conn.recv()
        except (EOFError, BrokenPipeError, OSError):
            self.kill()
            return "crash", None

    def close(self):
        try:
            if self.conn is not None:
                self.conn.send(None)
        except Exception:
            pass
        self.kill()


class RobustAtlasExtractor(AtlasGraphExtractor):
    """
    AtlasGraphExtractor + timeout + C/C++ switching + JSON output.
    extract_graph() is inherited and behaves exactly as before.
    """

    def __init__(self, default_lang: str = "c", timeout: float = DEFAULT_TIMEOUT_SEC,
                 rounds: int = 1, recursion_limit: Optional[int] = None):
        super().__init__(default_lang)
        self.timeout = timeout
        self.rounds = max(1, rounds)
        self._worker = GraphWorker(recursion_limit)

    def extract_robust(self, code: str, view: str = "ast+cfg+sdfg",
                       file_name: Optional[str] = None, lang: Optional[str] = None) -> dict:
        """
        Logic:
          1. guess language (argument `lang`, else file extension / code hints)
          2. try it (max `timeout` seconds); then try the other language; repeat `rounds` times
          3. real graph  -> status 'ok'            (stop at the first one)
             only fallback graphs seen -> 'fallback'  (graph is NOT returned, it is not a real graph)
             nothing usable (timeout / crash / error) -> 'failed'
        Returns {status, lang_guess, lang_used, attempts, total_sec, graph (JSON dict or None)}.
        """
        t0 = time.time()
        guess = (lang or guess_language(file_name, code)).lower()
        if guess not in ("c", "cpp"):
            guess = "c"
        result = {"status": "failed", "lang_guess": guess, "lang_used": None,
                  "attempts": [], "total_sec": 0.0, "graph": None}

        if not isinstance(code, str) or not code.strip():
            result["attempts"].append({"result": "empty_code"})
            return result

        saw_fallback_lang = None
        for l in [guess, other_language(guess)] * self.rounds:
            ta = time.time()
            tag, val = self._worker.run(code, l, view, self.timeout)
            dt = round(time.time() - ta, 2)
            if tag == "ok" and val is not None and not val["fallback"]:
                result.update(status="ok", lang_used=l, graph=val["graph"])
                result["attempts"].append({"lang": l, "result": "ok", "sec": dt})
                break
            if tag == "ok" and val is not None:
                saw_fallback_lang = saw_fallback_lang or l
                result["attempts"].append({"lang": l, "result": "fallback_graph", "sec": dt})
            else:
                result["attempts"].append({"lang": l, "result": tag,
                                           "detail": (str(val)[:200] if val else None), "sec": dt})

        if result["status"] != "ok" and saw_fallback_lang is not None:
            result["status"] = "fallback"
            result["lang_used"] = saw_fallback_lang
        result["total_sec"] = round(time.time() - t0, 2)
        return result

    def close(self):
        self._worker.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()