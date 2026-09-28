"""scip-dotnet reports static methods (StaticMethod 80) and constructors (Constructor 9) with their own
SymbolInformation.kind. They are callables: the parser must put them in `functions` exactly as it does for the
same symbols when a producer reports kind 0 and the symbol shape decides.
"""

from pathlib import Path

import pytest

from codegraphcontext.tools import scip_pb2
from codegraphcontext.tools.scip_indexer import ScipIndexParser

Kind = scip_pb2.SymbolInformation

SOURCE = "class C {\n  static void Make(int n) {}\n  C() {}\n  void Use() { Make(1); }\n}\n"
STATIC = "scip-dotnet nuget . . sample/C#Make()."
CTOR = "scip-dotnet nuget . . sample/C#`.ctor`()."
CTOR_OVERLOAD = "scip-dotnet nuget . . sample/C#`.ctor`(+1)."
CCTOR = "scip-dotnet nuget . . sample/C#`.cctor`()."


def _parse(tmp_path: Path, defs: list, refs: list = ()) -> dict:
    """defs: (symbol, kind, line, display_name); refs: (symbol, line, start, end)"""
    index = scip_pb2.Index()
    doc = index.documents.add()
    doc.relative_path = "C.cs"
    for symbol, kind, line, display in defs:
        occ = doc.occurrences.add()
        occ.symbol, occ.symbol_roles = symbol, 1
        occ.range.extend([line, 2, 6])
        info = doc.symbols.add()
        info.symbol, info.kind, info.display_name = symbol, kind, display
    for symbol, line, start, end in refs:
        occ = doc.occurrences.add()
        occ.symbol = symbol
        occ.range.extend([line, start, end])
    scip_path = tmp_path / "index.scip"
    scip_path.write_bytes(index.SerializeToString())
    (tmp_path / "C.cs").write_text(SOURCE)
    return ScipIndexParser().parse(scip_path, tmp_path)["files"][str((tmp_path / "C.cs").resolve())]


@pytest.mark.parametrize(
    "symbol, kind, name",
    [(STATIC, Kind.StaticMethod, "Make"), (CTOR, Kind.Constructor, ".ctor"),
     (CTOR_OVERLOAD, Kind.Constructor, ".ctor"), (CCTOR, Kind.Constructor, ".cctor")],
)
def test_static_method_and_constructor_are_functions(tmp_path: Path, symbol: str, kind: int, name: str) -> None:
    file_data = _parse(tmp_path, [(symbol, kind, 1, "void X(int n)")])
    assert [f["name"] for f in file_data["functions"]] == [name]
    assert not file_data["classes"] and not file_data["variables"]


@pytest.mark.parametrize("symbol, kind", [(STATIC, Kind.StaticMethod), (CTOR, Kind.Constructor), (CTOR_OVERLOAD, Kind.Constructor)])
def test_explicit_kind_matches_the_kind_zero_symbol_shape(tmp_path: Path, symbol: str, kind: int) -> None:
    (tmp_path / "old").mkdir()
    (tmp_path / "new").mkdir()
    old = _parse(tmp_path / "old", [(symbol, 0, 1, "void X(int n)")])
    new = _parse(tmp_path / "new", [(symbol, kind, 1, "void X(int n)")])
    strip = lambda f: {k: v for k, v in f.items() if k != "path"}
    assert [strip(f) for f in new["functions"]] == [strip(f) for f in old["functions"]]


def test_call_to_static_method_resolves_to_its_function(tmp_path: Path) -> None:
    file_data = _parse(tmp_path, [(STATIC, Kind.StaticMethod, 1, "void Make(int n)")], refs=[(STATIC, 3, 15, 19)])
    calls = file_data["function_calls_scip"] + file_data["module_level_calls_scip"]
    assert [(c["callee_name"], c["callee_line"]) for c in calls] == [("Make", 2)]
    assert [f["name"] for f in file_data["functions"]] == ["Make"]
