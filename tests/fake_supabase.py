"""
fake_supabase.py: a minimal in-memory stand-in for the real Supabase
client, just enough to support the exact chain calls this codebase
uses (`.table(x).select().eq().single().execute()`, `.insert()`,
`.update()`, `.in_()`, `.lt()`), so tests don't need a live database
or network access.

Not a general-purpose fake, only implements what's actually called.
"""

import copy
import uuid


class _Result:
    def __init__(self, data):
        self.data = data


class _Query:
    def __init__(self, store, table_name, mode, payload=None):
        self.store = store
        self.table_name = table_name
        self.mode = mode  # "select" | "insert" | "update"
        self.payload = payload
        self.filters = []  # list of (op, key, value)
        self._single = False
        self._order_key = None
        self._order_desc = False

    def eq(self, key, value):
        self.filters.append(("eq", key, value))
        return self

    def lt(self, key, value):
        self.filters.append(("lt", key, value))
        return self

    def in_(self, key, values):
        self.filters.append(("in", key, values))
        return self

    def order(self, key, desc=False):
        self._order_key = key
        self._order_desc = desc
        return self

    def single(self):
        self._single = True
        return self

    def _matches(self, row):
        for op, key, value in self.filters:
            if op == "eq" and row.get(key) != value:
                return False
            if op == "lt" and not (row.get(key) is not None and row.get(key) < value):
                return False
            if op == "in" and row.get(key) not in value:
                return False
        return True

    def execute(self):
        rows = self.store.setdefault(self.table_name, {})

        if self.mode == "select":
            matched = [copy.deepcopy(r) for r in rows.values() if self._matches(r)]
            if self._order_key:
                matched.sort(key=lambda r: (r.get(self._order_key) is None, r.get(self._order_key)),
                             reverse=self._order_desc)
            if self._single:
                if not matched:
                    raise Exception(f"No rows found in {self.table_name} for given filters (.single()).")
                return _Result(matched[0])
            return _Result(matched)

        if self.mode == "insert":
            new_row = dict(self.payload)
            new_row.setdefault("id", str(uuid.uuid4()))
            rows[new_row["id"]] = new_row
            return _Result([new_row])

        if self.mode == "update":
            updated = []
            for row_id, row in rows.items():
                if self._matches(row):
                    row.update(self.payload)
                    updated.append(copy.deepcopy(row))
            return _Result(updated)

        raise Exception(f"Unsupported mode: {self.mode}")


class _Table:
    def __init__(self, store, name):
        self.store = store
        self.name = name

    def select(self, *_args, **_kwargs):
        return _Query(self.store, self.name, "select")

    def insert(self, payload):
        return _Query(self.store, self.name, "insert", payload)

    def update(self, payload):
        return _Query(self.store, self.name, "update", payload)


class FakeSupabase:
    """Usage: fake.seed('appointments', {...}); fake.table('appointments').select(...)"""

    def __init__(self):
        self.store = {}

    def table(self, name):
        return _Table(self.store, name)

    def seed(self, table_name, row):
        row = dict(row)
        row.setdefault("id", str(uuid.uuid4()))
        self.store.setdefault(table_name, {})[row["id"]] = row
        return row["id"]

    def rows(self, table_name):
        return list(self.store.get(table_name, {}).values())
