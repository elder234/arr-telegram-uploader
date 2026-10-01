"""Minimal stand-in for pytest, used only when pytest is not installed.

Supports the small subset of the API this test suite actually uses:
``raises``, ``mark.parametrize``, ``skip``, and ``fail``. Shipping this keeps the
suite runnable offline; when the real pytest is present it takes precedence
because the runner imports it first.
"""

from __future__ import annotations

import traceback


class Skipped(Exception):
    """Raised by skip() to abort a test without failing it."""


class Failed(AssertionError):
    """Raised by fail()."""


def skip(reason: str = "") -> None:
    raise Skipped(reason)


def fail(reason: str = "") -> None:
    raise Failed(reason)


class _RaisesContext:
    def __init__(self, expected, match: str | None = None) -> None:
        self.expected = expected
        self.match = match
        self.value: BaseException | None = None

    def __enter__(self) -> "_RaisesContext":
        return self

    def __exit__(self, exc_type, exc, _tb) -> bool:
        if exc_type is None:
            names = getattr(self.expected, "__name__", str(self.expected))
            raise Failed(f"DID NOT RAISE {names}")
        if not issubclass(exc_type, self.expected):
            return False
        if self.match is not None:
            import re

            if not re.search(self.match, str(exc)):
                raise Failed(f"pattern {self.match!r} not found in {str(exc)!r}")
        self.value = exc
        return True


def raises(expected, match: str | None = None) -> _RaisesContext:
    return _RaisesContext(expected, match)


class _Parametrize:
    def __init__(self, argnames, argvalues, ids=None) -> None:
        self.argnames = [n.strip() for n in argnames.split(",")] if isinstance(argnames, str) else list(argnames)
        self.argvalues = list(argvalues)
        self.ids = ids

    def __call__(self, func):
        existing = getattr(func, "_shim_parametrize", [])
        func._shim_parametrize = [*existing, (self.argnames, self.argvalues)]
        return func


class _Mark:
    parametrize = _Parametrize


mark = _Mark()


def fixture(func=None, **_kwargs):
    """No-op decorator; the suite avoids fixtures deliberately."""
    if func is None:
        return lambda f: f
    return func


def approx(expected, rel: float = 1e-6):
    class _Approx:
        def __eq__(self, other) -> bool:
            try:
                return abs(float(other) - float(expected)) <= rel * max(abs(float(expected)), 1.0)
            except (TypeError, ValueError):
                return False

    return _Approx()


__all__ = ["raises", "mark", "skip", "fail", "fixture", "approx", "Skipped", "Failed", "traceback"]