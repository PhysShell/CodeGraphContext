"""SCIP CALLS contract on Windows and for producers without enclosing_range (scip-dotnet):
  A  row paths are matched in the stored _normalize_path form;
  B  a module-level reference strictly inside exactly one function span gets that Function as caller; anything else
     (outside every span, between functions, inside two spans) stays on the File - no nearest-preceding guess;
  C  a Function callee is the one at the row's callee_line, so same-named overloads in one file are not all targets.
Spans end at the Tree-sitter end recorded by the SCIP pipeline (ts_function_ends), else at line + source line count.
"""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from codegraphcontext.core.database_kuzu import KuzuDBManager
from codegraphcontext.tools.indexing.persistence.writer import GraphWriter, _normalize_path
from codegraphcontext.tools.indexing.scip_pipeline import run_scip_index_async

kuzu = pytest.importorskip("kuzu")


class _DBM:
    def get_backend_type(self):
        return "kuzudb"


def _src(lines):
    return "\n".join(["x"] * lines)


@pytest.fixture()
def env(tmp_path: Path):
    real = tmp_path / "Foo.cs"
    real.write_text("", encoding="utf-8")
    stored = _normalize_path(real)
    raw = str(real.resolve())                                   # the scip_indexer row form (backslashes on Windows)
    manager = KuzuDBManager(str(tmp_path / "db"))
    driver = manager.get_driver()
    nodes = [("Caller", 10), ("Caller", 40), ("Target", 60), ("Over", 70), ("Over", 80)]
    with driver.session() as s:
        s.run("CREATE (:File {path: $p, name: 'Foo.cs'})", p=stored)
        for i, (name, line) in enumerate(nodes):
            s.run("CREATE (:Function {uid: $u, name: $n, path: $p, line_number: $l})", u=f"u{i}", n=name, p=stored, l=line)
    yield SimpleNamespace(driver=driver, raw=raw, stored=stored)
    manager.close_driver()


def _row(env, callee, callee_line, ref, path=None):
    p = path or env.raw
    return {"caller_symbol": None, "caller_line": 0, "caller_file": p, "callee_symbol": f"s {callee}", "callee_file": p,
            "callee_line": callee_line, "callee_name": callee, "callee_kind": 26, "ref_line": ref}


def _functions(**extra):
    fns = [{"name": "Caller", "line_number": 10, "end_line": 10, "source": _src(5)},      # 10..14
           {"name": "Caller", "line_number": 40, "end_line": 40, "source": _src(5)},      # 40..44
           {"name": "Target", "line_number": 60, "end_line": 60, "source": _src(3)},
           {"name": "Over", "line_number": 70, "end_line": 70, "source": _src(1)},
           {"name": "Over", "line_number": 80, "end_line": 80, "source": _src(1)}]
    return fns


def _write(env, module_rows=(), fn_rows=(), functions=None, ts_ends=None):
    fd = {"path": env.raw, "functions": _functions() if functions is None else functions,
          "function_calls_scip": list(fn_rows), "module_level_calls_scip": list(module_rows)}
    if ts_ends is not None:
        fd["ts_function_ends"] = ts_ends
    GraphWriter(env.driver, _DBM()).write_scip_call_edges({env.raw: fd}, lambda s: s)
    with env.driver.session() as s:
        rows = s.run("MATCH (a)-[r:CALLS]->(b) RETURN label(a) AS la, a.name AS an, a.line_number AS al, "
                     "b.name AS bn, b.line_number AS bl, r.line_number AS l").data()
    return sorted((r["la"], r["an"], r["al"] if r["la"] == "Function" else None, r["bn"], r["bl"], r["l"]) for r in rows)


def test_windows_row_path_matches_the_stored_node(env):                                 # 1 (A)
    assert _write(env, [_row(env, "Target", 60, 2)]) == [("File", "Foo.cs", None, "Target", 60, 2)]


def test_reference_inside_a_method_span_gets_the_method_as_caller(env):                   # 2 (B)
    assert _write(env, [_row(env, "Target", 60, 12)]) == [("Function", "Caller", 10, "Target", 60, 12)]


def test_reference_outside_every_callable_stays_on_the_file(env):                         # 3 (B)
    assert _write(env, [_row(env, "Target", 60, 2)]) == [("File", "Foo.cs", None, "Target", 60, 2)]


def test_same_name_overload_targets_resolve_to_exactly_one(env):                          # 4 (C)
    assert _write(env, [_row(env, "Over", 80, 12)]) == [("Function", "Caller", 10, "Over", 80, 12)]


def test_two_same_name_callers_pick_the_enclosing_one(env):                               # 5 (B)
    assert _write(env, [_row(env, "Target", 60, 12), _row(env, "Target", 60, 42)]) == [
        ("Function", "Caller", 10, "Target", 60, 12), ("Function", "Caller", 40, "Target", 60, 42)]


def test_reference_between_functions_is_not_given_to_the_preceding_one(env):              # 6 (B, no fallback)
    assert _write(env, [_row(env, "Target", 60, 20)]) == [("File", "Foo.cs", None, "Target", 60, 20)]


def test_reference_inside_two_spans_stays_on_the_file(env):                               # 6b (B, ambiguity)
    fns = _functions()
    fns[0]["source"] = _src(40)                                                          # 10..49 overlaps 40..44
    assert _write(env, [_row(env, "Target", 60, 42)], functions=fns) == [("File", "Foo.cs", None, "Target", 60, 42)]


def test_already_normalized_paths_are_unchanged(env):                                    # 7 (A idempotent / Unix form)
    assert _normalize_path(env.stored) == env.stored
    assert _write(env, [_row(env, "Target", 60, 12, path=env.stored)]) == [("Function", "Caller", 10, "Target", 60, 12)]


def test_producer_enclosing_range_keeps_its_direct_caller(env):                          # 8
    fn_row = {"caller_symbol": "Caller", "caller_file": env.raw, "caller_line": 40, "callee_name": "Target",
              "callee_file": env.raw, "callee_line": 60, "ref_line": 12}                 # span would say Caller@10
    assert _write(env, fn_rows=[fn_row]) == [("Function", "Caller", 40, "Target", 60, 12)]


def test_tree_sitter_end_wins_over_the_source_line_count(env):
    fns = _functions()
    fns[0]["source"] = _src(8)                                  # attribute lines inflate the count: 10..17
    rows = [_row(env, "Target", 60, 16)]                        # after the real end (14): not Caller's
    assert _write(env, rows, functions=fns, ts_ends=[["Caller", 10, 14]]) == [("File", "Foo.cs", None, "Target", 60, 16)]


class _Indexer:
    def run(self, _p, _lang, output_dir):
        f = output_dir / "index.scip"
        f.write_bytes(b"fake")
        return f


@pytest.mark.asyncio
async def test_pipeline_records_tree_sitter_ends_without_touching_end_line(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    cs = repo / "a.cs"
    cs.write_text("class A {\n void M()\n {\n }\n}\n", encoding="utf-8")
    key = str(cs.resolve())
    scip_files = {key: {"path": key, "functions": [{"name": "M", "line_number": 2, "end_line": 2}], "classes": [],
                        "imports": [], "function_calls_scip": [], "module_level_calls_scip": []}}
    parser = MagicMock()
    parser.parse.return_value = {"path": key, "functions": [{"name": "M", "line_number": 2, "end_line": 4, "source": "void M()\n {\n }"}],
                                 "classes": [], "imports": [], "variables": [], "function_calls": []}
    writer = MagicMock()
    mod = SimpleNamespace(ScipIndexer=_Indexer, ScipIndexParser=lambda: SimpleNamespace(parse=lambda *_: {"files": scip_files}))
    with patch("codegraphcontext.tools.indexing.scip_pipeline.pre_scan_for_imports", return_value={}), \
         patch("codegraphcontext.tools.indexing.scip_pipeline.build_function_call_groups", return_value=([],) * 10):
        await run_scip_index_async(repo, is_dependency=False, job_id=None, lang="c_sharp", writer=writer,
                                   job_manager=MagicMock(), parsers_keys={".cs"}, get_parser=lambda _s: parser,
                                   scip_indexer_mod=mod)
    files_data = writer.write_scip_call_edges.call_args[0][0]
    assert files_data[key]["ts_function_ends"] == [["M", 2, 4]]
    assert files_data[key]["functions"][0]["end_line"] == 2      # the node property stays as SCIP gave it
