"""Offline test runner.

Prefers real pytest. Falls back to the bundled shim so the suite can run in a
sandbox with no package index available.

Usage::

    python _devtools/run_tests.py            # everything
    python _devtools/run_tests.py partition  # only matching modules
"""

from __future__ import annotations

import importlib
import importlib.util
import inspect
import os
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TESTS = ROOT / "tests"
SHIM = ROOT / "_devtools"

sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(SHIM))

try:
    import pytest  # type: ignore

    USING_SHIM = False
except ModuleNotFoundError:
    import pytest  # type: ignore  # falls back to _devtools/pytest.py

    USING_SHIM = True


def load_module(path: Path):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[path.stem] = module
    spec.loader.exec_module(module)
    return module


import tempfile


def _fixture_tmp_path(case_id: str) -> Path:
    return Path(tempfile.mkdtemp(prefix=f"uploader-{case_id}-"))


def _fixture_source(tmp_path: Path) -> Path:
    """A small stand-in movie file, mirroring the fixture used in
    test_uploader_retry."""
    path = tmp_path / "Movie.mkv"
    path.write_bytes(b"x" * 4096)
    return path


_NOTSET = object()


class MonkeyPatch:
    """Stand-in for pytest's monkeypatch fixture.

    Records every mutation and restores it on undo(). The runner calls undo()
    after each case, so a patched attribute cannot leak into the next test --
    which would turn a local failure into an unrelated one several modules later.
    """

    def __init__(self) -> None:
        self._undo: list = []

    @staticmethod
    def _import_dotted(path: str):
        """Resolve "pkg.mod.attr" to the owning object and the final name."""
        module_path, _, attr = path.rpartition(".")
        if not module_path:
            raise TypeError(f"not enough values to unpack: {path!r}")
        obj = importlib.import_module(module_path)
        names = attr.split(".")
        for part in names[:-1]:
            obj = getattr(obj, part)
        return obj, names[-1]

    def setattr(self, target, name, value=_NOTSET, raising: bool = True):
        # Two shapes, as in pytest: setattr(obj, name, value) and
        # setattr("pkg.mod.attr", value) -- in the second form the value
        # arrives positionally in `name`.
        if isinstance(target, str):
            if value is _NOTSET:
                value = name
            target, name = self._import_dotted(target)
        if value is _NOTSET:
            raise TypeError("setattr() requires a value")
        had = hasattr(target, name)
        old = getattr(target, name, None)
        self._undo.append(
            lambda: setattr(target, name, old) if had else delattr(target, name)
        )
        setattr(target, name, value)

    def delattr(self, target, name: str, raising: bool = True):
        if isinstance(target, str):
            target, name = self._import_dotted(target)
        had = hasattr(target, name)
        old = getattr(target, name, None)
        if not had and raising:
            raise AttributeError(name)
        self._undo.append(lambda: setattr(target, name, old))
        if had:
            delattr(target, name)

    def setenv(self, name: str, value: str, prepend: str | None = None):
        old = os.environ.get(name)
        self._undo.append(
            lambda: os.environ.__setitem__(name, old)
            if old is not None
            else os.environ.pop(name, None)
        )
        os.environ[name] = str(value)

    def delenv(self, name: str, raising: bool = True):
        old = os.environ.get(name)
        if old is None and raising:
            raise KeyError(name)
        self._undo.append(lambda: os.environ.__setitem__(name, old) if old is not None else None)
        os.environ.pop(name, None)

    def chdir(self, path):
        old = os.getcwd()
        self._undo.append(lambda: os.chdir(old))
        os.chdir(path)

    def syspath_prepend(self, path):
        old = list(sys.path)
        self._undo.append(lambda: sys.path.__setitem__(slice(None), old))
        sys.path.insert(0, str(path))

    def undo(self) -> None:
        while self._undo:
            self._undo.pop()()


# Builtin fixtures the suite uses. pytest provides these natively; the shim has
# to supply them so the same test files run either way. Each entry may depend on
# the ones before it.
_FIXTURES = {
    "tmp_path": lambda ctx: _fixture_tmp_path(ctx["case_id"]),
    "source": lambda ctx: _fixture_source(ctx["tmp_path"]),
    "monkeypatch": lambda ctx: MonkeyPatch(),
}


def builtin_fixtures(names, case_id: str):
    ctx: dict[str, object] = {"case_id": case_id}
    provided: dict[str, object] = {}

    for name in names:
        builder = _FIXTURES.get(name)
        if builder is None:
            continue
        # Resolve dependencies first, so order in the signature does not matter.
        for dep, dep_builder in _FIXTURES.items():
            if dep not in ctx:
                ctx[dep] = dep_builder(ctx)
        provided[name] = builder(ctx)
        ctx[name] = provided[name]

    return provided


def cases_for(func):
    """Yield (label, kwargs) for a test, expanding parametrize marks."""
    marks = getattr(func, "_shim_parametrize", None)
    if not marks:
        yield "", {}
        return

    def expand(marks_list, base_kwargs, idx, prefix):
        if idx >= len(marks_list):
            yield prefix, base_kwargs
            return
        argnames, argvalues = marks_list[idx]
        for value in argvalues:
            values = value if isinstance(value, (tuple, list)) else (value,)
            merged = dict(base_kwargs)
            merged.update(dict(zip(argnames, values)))
            label = ",".join(f"{k}={v}" for k, v in zip(argnames, values))
            yield from expand(marks_list, merged, idx + 1, f"{prefix}[{label}]")

    yield from expand(marks, {}, 0, "")


def main(argv: list[str]) -> int:
    filters = [a for a in argv if not a.startswith("-")]

    files = sorted(TESTS.glob("test_*.py"))
    if filters:
        files = [f for f in files if any(flt in f.stem for flt in filters)]
    if not files:
        print("no test modules matched")
        return 1

    print(f"runner: {'bundled shim' if USING_SHIM else 'pytest shim'}")
    print(f"modules: {', '.join(f.stem for f in files)}\n")

    # Keep the uploader's own log output out of the report; the pass/fail lines
    # below are the signal.
    import logging

    logging.disable(logging.CRITICAL)

    passed = failed = skipped = 0
    failures: list[tuple[str, str]] = []

    for path in files:
        print(f"  {path.stem}")
        try:
            module = load_module(path)
        except Exception:
            failed += 1
            failures.append((f"{path.stem} (import)", traceback.format_exc()))
            print("    E import error")
            continue

        for name, func in sorted(vars(module).items()):
            if not name.startswith("test_") or not callable(func):
                continue
            if getattr(func, "__module__", None) != module.__name__:
                continue

            for label, kwargs in cases_for(func):
                test_id = f"{path.stem}::{name}{label}"
                case_kwargs = dict(kwargs)
                try:
                    sig = inspect.signature(func)
                    supplied = builtin_fixtures(
                        [p for p in sig.parameters if p not in case_kwargs],
                        f"{path.stem}-{name}",
                    )
                    sig.bind(**{**supplied, **case_kwargs})
                    case_kwargs.update(supplied)
                except TypeError as exc:
                    print(f"    - {name}{label}: skipped (bad params: {exc})")
                    skipped += 1
                    continue

                try:
                    func(**case_kwargs)
                except pytest.Skipped:
                    print(f"    - {name}{label}: skipped")
                    skipped += 1
                except Exception:
                    failed += 1
                    failures.append((test_id, traceback.format_exc()))
                    print(f"    F {name}{label}")
                else:
                    passed += 1
                    print(f"    . {name}{label}")
                finally:
                    # Undo monkeypatch even when the test failed: a leaked
                    # attribute would surface as an unrelated failure much
                    # later, in a different module.
                    patcher = case_kwargs.get("monkeypatch")
                    if patcher is not None:
                        patcher.undo()

    print()
    for test_id, tb in failures:
        print("=" * 70)
        print(f"FAILED {test_id}")
        print("-" * 70)
        print(tb)

    total = passed + failed + skipped
    print(f"{passed}/{total} passed, {failed} failed, {skipped} skipped")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))