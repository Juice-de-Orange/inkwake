"""Guards against state that outlives the test that set it.

Two globals in this backend are process-wide by design, and both are resolved
*late* - which is what makes them dangerous. A test that poisons one of them
does not fail; the next test to touch that global fails instead, in a
different file, and the blame lands on whoever owns that file.

  ``app.config.settings``   a frozen dataclass singleton. Because it is
                            frozen, every test that re-points it has to go
                            through ``object.__setattr__`` and undo that by
                            hand - and a test that dies between the two leaves
                            ``data_dir`` pointing at a deleted tmp_path for the
                            rest of the session.

  ``sys.modules``           ``assemble._load()`` and ``layout``'s local
                            ``from .dither import dither_photo`` both import at
                            call time, so they see whatever is in the table at
                            that moment. A stub left behind is invisible until
                            some unrelated test renders a frame.

Both fixtures restore rather than assert. Failing the offending test would be
tidier in theory, but the whole point is that the offender has already failed
or errored by the time we get here: what is left to save is the session.
"""

from __future__ import annotations

import dataclasses
import sys
from typing import Any, Iterator

import pytest

from app.config import settings

#: Distinguishes "the key was absent" from "the key held None". A failed
#: import legitimately parks None in sys.modules, so None is a real value.
_ABSENT = object()


def _app_modules() -> dict[str, Any]:
    """The ``app`` package's entries in sys.modules, as a snapshot."""
    return {
        name: module
        for name, module in list(sys.modules.items())
        if name == "app" or name.startswith("app.")
    }


def _is_stub(module: Any) -> bool:
    """True for a hand-built ``types.ModuleType``, false for a real import.

    Import machinery always leaves both a spec and a file behind; a stub
    assembled in a test has neither. Removing only stubs matters: dropping a
    genuinely imported module would force a re-import later and hand out a
    *second* module object, so a test monkeypatching the one it holds would
    silently patch the wrong copy.
    """
    return getattr(module, "__spec__", None) is None and not getattr(module, "__file__", None)


@pytest.fixture(autouse=True)
def _settings_are_put_back() -> Iterator[None]:
    before = {field.name: getattr(settings, field.name) for field in dataclasses.fields(settings)}
    try:
        yield
    finally:
        for name, value in before.items():
            # Identity, not equality: Path and ZoneInfo compare by value, and
            # a test that swapped in an equal-but-different object has still
            # handed the next test something it did not ask for.
            if getattr(settings, name) is not value:
                object.__setattr__(settings, name, value)


@pytest.fixture(autouse=True)
def _module_table_is_put_back() -> Iterator[None]:
    before = _app_modules()
    try:
        yield
    finally:
        for name, module in before.items():
            if sys.modules.get(name, _ABSENT) is not module:
                sys.modules[name] = module
        for name, module in _app_modules().items():
            if name not in before and _is_stub(module):
                del sys.modules[name]
