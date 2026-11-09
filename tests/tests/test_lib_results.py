#!/usr/bin/env python3
"""Library results kept as deltas against the build's base tables
(``compiler/lib_results.py``) merge exactly like the full tables did:
``dict.update`` in module order (values and key order), set unions,
``setdefault(...).update`` of sets and ``list.extend``, for random bases and
random library tables (keys kept, changed, removed, added, reordered), in
both the Python form (a fresh transpile) and the JSON form (a cache entry).
"""
from __future__ import annotations

import json
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from compiler import lib_results as LR  # noqa: E402


def _rand_value(rng: random.Random, field: str):
    if field == "func_defaults":
        return [(rng.randint(0, 3), rng.choice(["0", '""', "nil"])) for _ in range(rng.randint(1, 2))]
    if field in ("func_param_counts",):
        return rng.randint(0, 4)
    if field in ("method_return_types", "result_payload_go_types"):
        return rng.choice(["int", "str", "*Row", "Result[int]"])
    if field in ("func_param_names", "func_param_go_types"):
        return [rng.choice("abcde") for _ in range(rng.randint(0, 3))]
    if field == "static_methods":
        return {rng.choice(["get", "put", "len", "new"]) for _ in range(rng.randint(0, 3))}
    if field == "overload_variants":
        return [
            {"arity": rng.randint(0, 3), "required": 0, "sig": tuple(rng.choice("xyz") for _ in range(2)),
             "return": "int", "return_key": None, "suffix": "", "meta_key": f"m{rng.randint(0, 9)}"}
            for _ in range(rng.randint(1, 2))
        ]
    raise AssertionError(field)


def _rand_base(rng: random.Random) -> dict:
    base = {}
    for field in LR.DICT_FIELDS:
        base[field] = {f"k{i}": _rand_value(rng, field) for i in rng.sample(range(40), rng.randint(0, 25))}
    for field in LR.UNION_SETS:
        base[field] = {f"s{i}" for i in rng.sample(range(40), rng.randint(0, 20))}
    base["static_vars"] = {}
    return base


def _rand_final(rng: random.Random, base: dict) -> dict:
    final = {}
    mode = rng.random()
    for field in LR.DICT_FIELDS:
        table = {}
        for k, v in base[field].items():
            r = rng.random()
            if mode < 0.3 or r < 0.8:  # mostly the injected base, untouched
                table[k] = v
            elif r < 0.9:
                table[k] = _rand_value(rng, field)
            # else removed
        for _ in range(rng.randint(0, 4)):
            table[f"n{rng.randint(0, 60)}"] = _rand_value(rng, field)
        if rng.random() < 0.15:  # a removed key put back at the end, etc.
            items = list(table.items())
            rng.shuffle(items)
            table = dict(items)
        final[field] = table
    for field in LR.UNION_SETS:
        values = {v for v in base[field] if mode < 0.3 or rng.random() < 0.9}
        values.update(f"t{rng.randint(0, 50)}" for _ in range(rng.randint(0, 3)))
        final[field] = values
    final["static_vars"] = {f"C{rng.randint(0, 5)}": {f"v{rng.randint(0, 5)}": rng.random() < 0.5}
                            for _ in range(rng.randint(0, 3))}
    return final


def _fold_full(fulls: list[dict]) -> dict:
    """The merge of the full tables, as the build always did it."""
    out = {field: {} for field in LR.DICT_FIELDS}
    for field in LR.UNION_SETS:
        out[field] = set()
    out["static_vars"] = {}
    for full in fulls:
        for field in LR.UPDATE_DICTS:
            out[field].update(full[field])
        for field in LR.UNION_DICT_SETS:
            for k, v in full[field].items():
                out[field].setdefault(k, set()).update(v)
        for field in LR.EXTEND_DICT_LISTS:
            for k, v in full[field].items():
                out[field].setdefault(k, []).extend(v)
        for field in LR.UNION_SETS:
            out[field].update(full[field])
        for k, v in full["static_vars"].items():
            out["static_vars"].setdefault(k, {}).update(v)
    return out


def _same(a: dict, b: dict) -> None:
    for field in LR.FIELDS:
        assert a[field] == b[field], (field, a[field], b[field])
        if isinstance(a[field], dict):
            assert list(a[field]) == list(b[field]), (field, "key order", list(a[field]), list(b[field]))


def check(seed: int) -> None:
    rng = random.Random(seed)
    base_raw = _rand_base(rng)
    base = LR.Base(base_raw)
    finals = [_rand_final(rng, base.tables) for _ in range(rng.randint(1, 8))]
    # Python form (a fresh library transpile).
    merger = LR.Merger(base)
    for final in finals:
        delta = LR.delta_from_raw(final, base)
        for field in LR.FIELDS:
            assert LR.materialize(field, delta[field], base) == final[field], field
        merger.add(delta)
    _same(merger.tables(), _fold_full(finals))
    # JSON form (a cache entry, decoded): the deltas of the JSON tables, and
    # the JSON form of the Python deltas (what a library transpile stores).
    for stored_form in ("json-delta", "delta-json"):
        merger = LR.Merger(base)
        decoded_fulls = []
        for final in finals:
            ser = {field: LR.ser_table(field, final[field]) for field in LR.FIELDS}
            if stored_form == "json-delta":
                payload = LR.entry_payload(ser, base)
            else:
                delta = LR.delta_from_raw(final, base)
                payload = {field: LR.ser_delta(field, delta[field]) for field in LR.FIELDS}
            delta = LR.delta_from_entry(json.loads(json.dumps(payload)))
            assert delta is not None
            decoded_fulls.append({field: LR.deser_table(field, json.loads(json.dumps(ser[field]))) for field in LR.FIELDS})
            merger.add(delta)
        _same(merger.tables(), _fold_full(decoded_fulls))


def main() -> int:
    for seed in range(400):
        check(seed)
    print("lib results: 400 random merges match the full-table merge")
    return 0


if __name__ == "__main__":
    sys.exit(main())
