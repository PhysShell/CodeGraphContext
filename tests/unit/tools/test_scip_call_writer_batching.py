"""write_scip_call_edges writes SCIP CALLS with one relationship-only UNWIND per label pair and chunk: every caller /
callee label is tried, a Function callee is the one at the row's callee_line, rows that differ only by ref_line stay
separate, identical rows merge, a failing row does not drop the rest, and Windows row paths match the stored form.
"""
from pathlib import Path

import pytest

from codegraphcontext.core.database_kuzu import KuzuDBManager
import codegraphcontext.core.database_embedded_kuzu as embedded
from codegraphcontext.tools.indexing.persistence.writer import GraphWriter

kuzu = pytest.importorskip("kuzu")
A, B = "C:/fx/a.cs", "C:/fx/b.cs"


class _DBM:
    def get_backend_type(self):
        return "kuzudb"


@pytest.fixture()
def graph(tmp_path: Path):
    manager = KuzuDBManager(str(tmp_path / "db"))
    driver = manager.get_driver()
    with driver.session() as s:
        for p in (A, B):
            s.run("CREATE (:File {path: $p, name: $n})", p=p, n=p.rsplit("/", 1)[-1])
        for i, (label, name, path, line) in enumerate([
            ("Function", "A", A, 1), ("Function", "B", B, 5), ("Function", "B", B, 9),
            ("Class", "K", B, 30), ("Variable", "V", A, 60),
        ]):
            s.run(f"CREATE (:`{label}` {{uid: $u, name: $n, path: $p, line_number: $l}})", u=f"u{i}", n=name, p=path, l=line)
    yield driver
    manager.close_driver()


def _fn(caller, line, callee, ref, callee_line=5, caller_file=A, callee_file=B):
    return {"caller_symbol": caller, "caller_file": caller_file, "caller_line": line, "callee_name": callee,
            "callee_file": callee_file, "callee_line": callee_line, "ref_line": ref}


def _mod(callee, ref, callee_line=5, caller_file=A, callee_file=B):
    return {"caller_file": caller_file, "callee_name": callee, "callee_file": callee_file, "callee_line": callee_line,
            "ref_line": ref}


def _edges(driver):
    with driver.session() as s:
        rows = s.run("MATCH (a)-[r:CALLS]->(b) RETURN label(a) AS la, a.name AS an, label(b) AS lb, b.name AS bn, "
                     "b.line_number AS bl, r.line_number AS l, r.source AS src").data()
    return sorted((r["la"], r["an"], r["lb"], r["bn"], r["bl"], r["l"], r["src"]) for r in rows)


def _write(driver, fn_rows=(), mod_rows=()):
    GraphWriter(driver, _DBM()).write_scip_call_edges(
        {A: {"function_calls_scip": list(fn_rows), "module_level_calls_scip": list(mod_rows)}}, lambda s: s)


def test_edge_set_matches_the_per_edge_semantics(graph):
    _write(graph,
           fn_rows=[_fn("A", 1, "B", 10), _fn("A", 1, "B", 20, callee_line=9), _fn("A", 1, "B", 10), _fn("V", 60, "B", 13),
                    _fn("A", 1, "K", 11, callee_line=30), _fn("Nope", 99, "B", 15), _fn("A", 1, "Ghost", 16)],
           mod_rows=[_mod("B", 21), _mod("B", 21), _mod("K", 22, callee_line=30), _mod("B", 24, caller_file="C:/fx/zz.cs")])
    assert _edges(graph) == sorted([
        ("File", "a.cs", "Class", "K", 30, 22, "scip"),
        ("File", "a.cs", "Function", "B", 5, 21, "scip"),
        ("Function", "A", "Class", "K", 30, 11, "scip"),
        ("Function", "A", "Function", "B", 5, 10, "scip"), ("Function", "A", "Function", "B", 9, 20, "scip"),
        ("Variable", "V", "Function", "B", 5, 13, "scip"),
    ])


def test_a_failing_row_does_not_drop_the_others(graph):
    _write(graph, fn_rows=[_fn("A", 1, "B", 30), _fn("A", "not-a-line", "B", 31), _fn("A", 1, "K", 32, callee_line=30)],
           mod_rows=[_mod("B", 33, callee_line=9), _mod("B", "not-a-line"), _mod("K", 34, callee_line=30)])
    assert _edges(graph) == sorted([
        ("File", "a.cs", "Class", "K", 30, 34, "scip"),
        ("File", "a.cs", "Function", "B", 9, 33, "scip"),
        ("Function", "A", "Class", "K", 30, 32, "scip"),
        ("Function", "A", "Function", "B", 5, 30, "scip"),
    ])


def test_windows_row_paths_match_the_stored_form(graph):
    _write(graph, mod_rows=[_mod("B", 40, caller_file="C:\\fx\\a.cs", callee_file="C:\\fx\\b.cs")])
    assert _edges(graph) == [("File", "a.cs", "Function", "B", 5, 40, "scip")]


def test_query_count_does_not_grow_with_rows_inside_a_batch(graph, monkeypatch):
    calls = []
    original = embedded.EmbeddedSessionWrapper.run
    monkeypatch.setattr(embedded.EmbeddedSessionWrapper, "run", lambda self, q, **p: (calls.append(q), original(self, q, **p))[1])
    _write(graph, mod_rows=[_mod("B", i) for i in range(1, 400)])
    assert len(calls) == 10                                  # one per callee label, not one per row and label
    assert len([e for e in _edges(graph) if e[3] == "B"]) == 399
