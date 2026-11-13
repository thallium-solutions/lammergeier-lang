"""Front end of one module: parse preprocessing, the Lark parse and the facts
the build reads off the fresh tree, cached by content.

Every module of a build goes through :class:`FrontEnd` (the compiled file,
each imported module, and the modules the semantic checker reads), so a
module is preprocessed and parsed at most once per build however many
passes need it, and not at all when an earlier build already did it:

- **Key**: ``sha256`` of the entry format, the compiler digest
  (``cache.compiler_version()``: every compiler source and the grammar),
  the Lark and Python versions (the value is a pickle of Lark objects), the
  module's resolved path (its export facts name it), whether the parser
  input gets a trailing newline, and the exact text handed to
  ``preprocess_for_parse``.
- **Value**: two pickles: the module's *meta* (the ``ParsePreprocessResult``
  with the preprocessed source and ``go!`` blocks, the ``ModuleFacts`` of
  the module index and a :class:`ModuleScan`) and its parse tree. Every
  request returns fresh objects (later passes rewrite trees in place).
- **Where**: in memory for the build, and on disk under
  ``<cache_dir>/frontend/`` (``LAMC_CACHE_DIR``; written atomically, safe
  for concurrent ``lamc`` processes; ``--no-cache`` skips the disk).

A module that does not parse is not cached: asking for its tree parses it
again in this process, so the syntax error is the one a plain parse raises.

:meth:`FrontEnd.prefetch` hands modules to the build's worker processes
(``--workers``); their results are picked up when the build asks for the
module, in its own order.
"""
from __future__ import annotations

import gc
import hashlib
import os
import pickle
import sys
import tempfile
import time
import zlib
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

FORMAT = 1
_SUFFIX = ".lamfe"


@dataclass
class ModuleScan:
    """Facts of a module's fresh tree used by the import walk."""

    imports: list[str] = field(default_factory=list)
    uses_json: bool = False
    uses_markup: bool = False
    uses_string_methods: bool = False
    symbols: dict[str, str] = field(default_factory=dict)
    bindings: list = field(default_factory=list)
    # For the namespace plans: names bound by assignments / parameters ...
    # and ``alias.member`` accesses of every ``import module [as alias]``.
    locally_bound: set = field(default_factory=set)
    member_accesses: dict = field(default_factory=dict)
    # Static members of the module's classes (``class_static_members``).
    statics: dict = field(default_factory=dict)


@dataclass
class ModuleMeta:
    parse_input: Any  # compiler.parse_input.ParsePreprocessResult
    facts: Any  # compiler.modules.ModuleFacts
    scan: ModuleScan | None  # None when the module does not parse
    parsed: bool


@contextmanager
def _gc_paused():
    # Unpickling a tree allocates hundreds of thousands of objects; the
    # cyclic collector would walk them over and over (5x slower), and again
    # in every later full collection: the trees live until the build ends
    # and hold no cycles, so they are moved out of the collector's
    # generations (``gc.freeze``; ``compile_lam`` unfreezes at the end).
    enabled = gc.isenabled()
    gc.disable()
    try:
        yield
    finally:
        gc.freeze()
        if enabled:
            gc.enable()


def _loads(blob: bytes):
    with _gc_paused():
        return pickle.loads(blob)


def _dumps(value) -> bytes:
    return pickle.dumps(value, protocol=pickle.HIGHEST_PROTOCOL)


_KEY_SALT: bytes | None = None


def _key_salt() -> bytes:
    global _KEY_SALT
    if _KEY_SALT is None:
        from compiler import cache as _cache
        try:
            import lark as _lark
            lark_version = _lark.__version__
        except Exception:  # noqa: BLE001 - no lark: nothing parses anyway
            lark_version = "none"
        _KEY_SALT = (
            f"lam-frontend-v{FORMAT};lark={lark_version};"
            f"py={sys.version_info[0]}.{sys.version_info[1]};"
            f"compiler={_cache.compiler_version()}\0"
        ).encode("utf-8")
    return _KEY_SALT


def module_key(path: Path, text: str, trailing_newline: bool) -> str:
    h = hashlib.sha256(_key_salt())
    h.update(str(path).encode("utf-8", "surrogatepass"))
    h.update(b"\0nl\0" if trailing_newline else b"\0\0")
    h.update(text.encode("utf-8", "surrogatepass"))
    return h.hexdigest()


def _entry_path(key: str) -> Path:
    from compiler import cache as _cache
    return _cache.cache_dir() / "frontend" / key[:2] / f"{key[2:]}{_SUFFIX}"


def _read_disk(key: str) -> tuple[bytes, bytes | None] | None:
    path = _entry_path(key)
    try:
        data = path.read_bytes()
    except OSError:
        return None
    try:
        meta_blob, tree_blob = pickle.loads(zlib.decompress(data))
        if not isinstance(meta_blob, bytes) or not isinstance(tree_blob, bytes):
            return None
        from compiler import cache as _cache
        _cache.touch(path)  # used: the size cap keeps it longer
        return meta_blob, tree_blob
    except Exception:  # noqa: BLE001 - a damaged entry is a miss
        return None


def _write_disk(key: str, meta_blob: bytes, tree_blob: bytes) -> None:
    path = _entry_path(key)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=".lamc-fe-", suffix=".tmp", dir=str(path.parent))
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(zlib.compress(_dumps((meta_blob, tree_blob)), 1))
            os.replace(tmp_name, path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
    except OSError:
        return


def _driver():
    """The compiler module running the build (``lamc`` may run
    ``compiler/lammergeier.py`` as ``__main__``)."""
    main = sys.modules.get("__main__")
    if main is not None and getattr(main, "preprocess_for_parse", None) is not None and getattr(main, "create_parser", None) is not None:
        return main
    from compiler import lammergeier
    return lammergeier


def scan_tree(driver, tree, preprocessed: str) -> ModuleScan:
    from compiler import namespacing

    bindings = namespacing.import_bindings(tree)
    return ModuleScan(
        imports=driver.extract_imports(tree),
        uses_json=driver.uses_native_json(tree),
        uses_markup=driver.uses_native_markup(tree),
        uses_string_methods=bool(driver.STRING_METHOD_RE.search(preprocessed)),
        symbols=namespacing.top_level_symbols(tree),
        bindings=bindings,
        locally_bound=namespacing.locally_bound_names(tree),
        statics=namespacing.class_static_members(tree),
        member_accesses={
            binding.local: namespacing.module_member_accesses(tree, binding.local)
            for binding in bindings
            if binding.imported is None
        },
    )


class LazyTree:
    """A module's tree, loaded from the front end the first time a pass
    needs it (then the same object for every pass, like a tree parsed
    once): a build whose modules are all cached needs few trees."""

    def __init__(self, frontend: "FrontEnd", path: Path, text: str, tree=None) -> None:
        self._frontend = frontend
        self._path = path
        self._text = text
        self._tree = tree
        self._loaded = tree is not None

    def load_tree(self):
        if not self._loaded:
            self._tree = self._frontend.module(self._path, self._text)[1]
            self._loaded = True
        return self._tree


def resolve_tree(value):
    """``value`` itself, or the tree a :class:`LazyTree` stands for."""
    return value.load_tree() if isinstance(value, LazyTree) else value


def compute_module(driver, path: Path, text: str, trailing_newline: bool):
    """Preprocess and parse ``text`` like the build always did: returns
    ``(meta, tree, preprocess_seconds, parse_seconds)``; ``tree`` is
    ``None`` when the parse fails."""
    from compiler.modules import module_facts_from_source

    start = time.perf_counter()
    parse_input = driver.preprocess_for_parse(text)
    pre_seconds = time.perf_counter() - start
    start = time.perf_counter()
    try:
        tree = driver.create_parser().parse(parse_input.source + ("\n" if trailing_newline else ""))
    except Exception:  # noqa: BLE001 - the caller parses again for the error
        tree = None
    parse_seconds = time.perf_counter() - start
    facts = module_facts_from_source(path, text)
    scan = scan_tree(driver, tree, parse_input.source) if tree is not None else None
    return ModuleMeta(parse_input, facts, scan, tree is not None), tree, pre_seconds, parse_seconds


# ── Worker side ──────────────────────────────────────────────────────

_WORKER_USE_DISK = False


def _init_worker(use_disk: bool) -> None:
    global _WORKER_USE_DISK
    _WORKER_USE_DISK = use_disk


def _worker_compute(task: tuple[str, str, str, bool]):
    key, path, text, trailing_newline = task
    sys.setrecursionlimit(max(sys.getrecursionlimit(), 10000))
    meta, tree, pre_s, parse_s = compute_module(_driver(), Path(path), text, trailing_newline)
    meta_blob = _dumps(meta)
    tree_blob = _dumps(tree) if tree is not None else None
    if tree_blob is not None and _WORKER_USE_DISK:
        _write_disk(key, meta_blob, tree_blob)
    return meta_blob, tree_blob, pre_s, parse_s


# ── Build side ───────────────────────────────────────────────────────


class FrontEnd:
    """The front-end results of one build (see the module docstring)."""

    def __init__(self, *, use_disk: bool = True, workers: int = 1, driver=None) -> None:
        self.use_disk = use_disk
        self.workers = workers
        self.driver = driver
        self._memory: dict[str, tuple[bytes, bytes | None]] = {}
        self._pending: dict[str, Any] = {}
        self._pool = None
        self._pool_failed = False

    # -- lifecycle --

    def close(self) -> None:
        pool, self._pool = self._pool, None
        if pool is not None:
            pool.shutdown(wait=True, cancel_futures=True)

    def _get_driver(self):
        return self.driver if self.driver is not None else _driver()

    # -- lookup --

    def _blobs(self, path: Path, text: str, trailing_newline: bool):
        key = module_key(path, text, trailing_newline)
        blobs = self._memory.get(key)
        if blobs is not None:
            return key, blobs
        future = self._pending.pop(key, None)
        if future is not None:
            from compiler import timings
            start = time.perf_counter()
            try:
                meta_blob, tree_blob, pre_s, parse_s = future.result()
            except Exception:  # noqa: BLE001 - a broken worker: do it here
                future = None
            else:
                timings.record("workers", time.perf_counter() - start)
                timings.record_worker("preprocess", pre_s)
                timings.record_worker("parse", parse_s)
                blobs = (meta_blob, tree_blob)
                self._memory[key] = blobs
                return key, blobs
        if self.use_disk:
            from compiler import timings
            start = time.perf_counter()
            blobs = _read_disk(key)
            if blobs is not None:
                timings.record("cache_load", time.perf_counter() - start)
                self._memory[key] = blobs
                return key, blobs
        return key, None

    def _compute(self, key: str, path: Path, text: str, trailing_newline: bool):
        meta, tree, _pre_s, _parse_s = compute_module(self._get_driver(), path, text, trailing_newline)
        meta_blob = _dumps(meta)
        tree_blob = _dumps(tree) if tree is not None else None
        self._memory[key] = (meta_blob, tree_blob)
        if tree_blob is not None and self.use_disk:
            _write_disk(key, meta_blob, tree_blob)
        return meta, tree

    def module(self, path: Path, text: str, *, trailing_newline: bool = True) -> tuple[ModuleMeta, Any]:
        """``(meta, tree)`` of the module at ``path`` with source ``text``,
        fresh objects; ``tree`` is ``None`` when it does not parse (call
        :meth:`raise_parse_error` for the error)."""
        key, blobs = self._blobs(path, text, trailing_newline)
        if blobs is None:
            return self._compute(key, path, text, trailing_newline)
        meta_blob, tree_blob = blobs
        meta = _loads(meta_blob)
        start = time.perf_counter()
        tree = _loads(tree_blob) if tree_blob is not None else None
        from compiler import timings
        timings.record("cache_load", time.perf_counter() - start)
        return meta, tree

    def cached_meta(self, path: Path, text: str, *, trailing_newline: bool = True) -> ModuleMeta | None:
        """The meta of a module this front end (or the disk cache) already
        has, else ``None`` (nothing is computed)."""
        _key, blobs = self._blobs(path, text, trailing_newline)
        return _loads(blobs[0]) if blobs is not None else None

    def meta(self, path: Path, text: str, *, trailing_newline: bool = True) -> ModuleMeta:
        key, blobs = self._blobs(path, text, trailing_newline)
        if blobs is None:
            return self._compute(key, path, text, trailing_newline)[0]
        return _loads(blobs[0])

    def blobs(self, path: Path, text: str, *, trailing_newline: bool = True):
        """The pickled ``(meta, tree)`` of a module already known to this
        front end (in memory), else ``None``: handed to a worker process."""
        return self._memory.get(module_key(path, text, trailing_newline))

    def seed(self, path: Path, text: str, blobs, *, trailing_newline: bool = True) -> None:
        self._memory[module_key(path, text, trailing_newline)] = blobs

    def seed_key(self, key: str, blobs) -> None:
        self._memory[key] = blobs

    def raise_parse_error(self, parse_input, trailing_newline: bool = True) -> None:
        """Parse again in this process: raises the parser's own error."""
        self._get_driver().create_parser().parse(parse_input.source + ("\n" if trailing_newline else ""))

    # -- workers --

    def prefetch(self, items: list[tuple[Path, str]], *, trailing_newline: bool = True) -> None:
        """Start preprocessing and parsing the modules ``items``
        (``(resolved path, text)``) in worker processes when they are not
        cached yet."""
        if self.workers <= 1 or self._pool_failed:
            return
        todo = []
        for path, text in items:
            key = module_key(path, text, trailing_newline)
            if key in self._memory or key in self._pending:
                continue
            if self.use_disk and _entry_path(key).is_file():
                continue
            todo.append((key, str(path), text, trailing_newline))
        if not todo:
            return
        if self._pool is None:
            if len(todo) < 2:
                # One module: parsed here when the walk reaches it (a
                # worker would cost as much). Later batches may start one.
                return
            from compiler import workers as _workers
            try:
                # The import walk discovers modules a few at a time: one
                # pool for the whole build, as large as allowed.
                # Processes start as tasks arrive, up to ``workers``.
                self._pool = _workers.make_pool(
                    self.workers, max(self.workers, len(todo)),
                    initializer=_init_worker, initargs=(self.use_disk,),
                )
            except Exception:  # noqa: BLE001 - no processes: stay in-process
                self._pool = None
            if self._pool is None:
                self._pool_failed = True
                return
        for task in todo:
            try:
                self._pending[task[0]] = self._pool.submit(_worker_compute, task)
            except Exception:  # noqa: BLE001 - broken pool: stay in-process
                self._pool_failed = True
                return


_ACTIVE: FrontEnd | None = None


@contextmanager
def activated(frontend: FrontEnd):
    """Make ``frontend`` the one :func:`active` returns (the semantic
    checker reads imported modules through it) for the duration."""
    global _ACTIVE
    previous = _ACTIVE
    _ACTIVE = frontend
    try:
        yield frontend
    finally:
        _ACTIVE = previous


def active() -> FrontEnd | None:
    return _ACTIVE


def parsed_module_tree(path: Path, text: str):
    """The tree of an imported module for the semantic checker: through the
    build's front end when one is active, else a plain parse (``None`` when
    the module does not parse)."""
    frontend = _ACTIVE
    if frontend is not None:
        return frontend.module(Path(path), text)[1]
    driver = _driver()
    parse_input = driver.preprocess_for_parse(text)
    return driver.create_parser().parse(parse_input.source)
