"""Semantic checks of a build's imported modules in worker processes
(``lamc build --workers N``).

Each imported module is checked on its own (``semantic.check_source`` with
``imported_module=True``) against the trees of the modules it imports, so the
checks are independent: a worker gets the module index and the parsed trees
of the build (as the front end's pickles), checks the modules it is given and
returns their diagnostics; the build prints them in module order, exactly as
if it had checked them itself. When the build records its inputs
(``lamc dev``), the module lookups a worker makes are replayed into the
recorder.
"""
from __future__ import annotations

import copy
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_MISSING = object()


@dataclass
class CheckStage:
    module_index: Any  # compiler.modules.WorkspaceIndex (a copy: picklable)
    tree_texts: dict  # path -> text of every tree the build's checks share
    blobs: dict  # front-end key -> pickled (meta, tree)
    use_disk: bool
    record: bool = False
    _frontend: Any = field(default=None, repr=False)
    _shared: Any = field(default=None, repr=False)


def picklable_index(module_index):
    """A copy of the index without instance-level patches (the inputs
    recorder wraps ``resolve_module``)."""
    clone = copy.copy(module_index)
    clone.__dict__.pop("resolve_module", None)
    clone.facts_provider = None
    return clone


class LazyTrees(dict):
    """``ProgramCheckCache.trees`` of a worker: a module's tree is loaded
    from the build's pickles the first time a check asks for it."""

    def __init__(self, loader) -> None:
        super().__init__()
        self._loader = loader

    def __contains__(self, path) -> bool:
        if dict.__contains__(self, path):
            return True
        tree = self._loader(path)
        if tree is _MISSING:
            return False
        dict.__setitem__(self, path, tree)
        return True


_STAGE: CheckStage | None = None


def _init_worker(stage: CheckStage) -> None:
    global _STAGE
    from compiler import frontend as _frontend

    _STAGE = stage
    frontend = _frontend.FrontEnd(use_disk=stage.use_disk, workers=1)
    for key, blobs in stage.blobs.items():
        frontend.seed_key(key, blobs)
    stage._frontend = frontend
    # The checker reads modules it has no tree for through the active front end.
    _frontend._ACTIVE = frontend

    from compiler import semantic as _semantic

    def load(path):
        source = stage.tree_texts.get(path)
        if source is None:
            return _MISSING
        try:
            return frontend.module(path, source)[1]
        except Exception:  # noqa: BLE001 - like a module that does not parse
            return None

    # One per worker, like the build's: trees and class tables are shared by
    # the checks this worker runs.
    shared = _semantic.ProgramCheckCache()
    shared.trees = LazyTrees(load)
    stage._shared = shared


def _worker_check(task: tuple[str, str, dict]):
    """``(path, text, go_blocks)`` -> ``(diagnostics or None, lookups,
    cpu seconds)``; ``None``: the checker crashed (the build ignores it)."""
    import sys

    from compiler import semantic as _semantic

    sys.setrecursionlimit(max(sys.getrecursionlimit(), 10000))
    stage = _STAGE
    assert stage is not None
    frontend = stage._frontend
    path_text, text, go_blocks = task
    mod_file = Path(path_text)
    module_index = stage.module_index
    lookups: list = []
    if stage.record:
        resolve = type(module_index).resolve_module.__get__(module_index)

        def recording_resolve(from_path, module):
            found = resolve(from_path, module)
            lookups.append((str(from_path), module, str(found) if found is not None else None))
            return found

        module_index.resolve_module = recording_resolve
    shared = stage._shared
    start = time.process_time()
    try:
        tree = frontend.module(mod_file, text)[1]
        diags = _semantic.check_source(
            tree,
            go_blocks=go_blocks,
            module_index=module_index,
            source_path=mod_file,
            shared=shared,
            imported_module=True,
        )
    except Exception:  # noqa: BLE001 - a checker crash must not block the build
        diags = None
    finally:
        if stage.record:
            module_index.__dict__.pop("resolve_module", None)
    return diags, lookups, time.process_time() - start


def _worker_check_chunk(tasks: list) -> list:
    """Several modules, next to each other in the import walk (they share
    most imports: the worker's trees and class tables serve them all)."""
    return [_worker_check(task) for task in tasks]


def chunked(items: list, workers: int) -> list[list]:
    """Contiguous chunks, about three per worker."""
    if not items:
        return []
    size = max(1, -(-len(items) // (workers * 3)))
    return [items[i:i + size] for i in range(0, len(items), size)]
