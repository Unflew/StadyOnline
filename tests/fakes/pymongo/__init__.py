"""Заглушка pymongo в памяти — только для офлайн-тестов (см. README.md рядом).
Поддерживает ровно то, что использует mongo.py: insert_one, find().sort().limit(), count_documents,
update_many, delete_many, create_index(unique=True), command('ping') и фильтры $in/$gte/$lte."""
from . import errors  # noqa: F401
from .errors import DuplicateKeyError

_STORE = {}


def _match(doc, flt):
    for k, cond in flt.items():
        v = doc.get(k)
        if isinstance(cond, dict):
            for op, x in cond.items():
                if op == "$in" and v not in x: return False
                if op == "$gte" and not (v is not None and v >= x): return False
                if op == "$lte" and not (v is not None and v <= x): return False
        elif v != cond:
            return False
    return True


class _Cursor:
    def __init__(self, docs, proj):
        self._docs, self._proj = docs, proj

    def sort(self, keys):
        for k, d in reversed(keys):
            self._docs.sort(key=lambda x: (x.get(k) is None, x.get(k)), reverse=d < 0)
        return self

    def limit(self, n):
        self._docs = self._docs[:n]
        return self

    def __iter__(self):
        for d in self._docs:
            d = dict(d)
            if self._proj and self._proj.get("_id") == 0:
                d.pop("_id", None)
            yield d


class _Collection:
    def __init__(self):
        self.docs, self.unique, self.seq = [], [], 0

    def create_index(self, keys, unique=False):
        if unique:
            fields = tuple(k for k, _ in keys)
            if fields not in self.unique:
                self.unique.append(fields)

    def insert_one(self, doc):
        for fields in self.unique:
            if any(all(d.get(f) == doc.get(f) for f in fields) for d in self.docs):
                raise DuplicateKeyError("duplicate key")
        self.seq += 1
        self.docs.append(dict(doc, _id=self.seq))

    def find(self, flt=None, proj=None):
        return _Cursor([d for d in self.docs if _match(d, flt or {})], proj)

    def count_documents(self, flt):
        return sum(1 for d in self.docs if _match(d, flt))

    def update_many(self, flt, upd):
        for d in self.docs:
            if _match(d, flt):
                d.update(upd["$set"])

    def delete_many(self, flt):
        self.docs[:] = [d for d in self.docs if not _match(d, flt)]


class _Database:
    def __init__(self):
        self._c = {}

    def __getattr__(self, name):
        return self._c.setdefault(name, _Collection())

    def __getitem__(self, name):
        return getattr(self, name)

    def command(self, *a, **k):
        return {"ok": 1}


class MongoClient:
    def __init__(self, *a, **k):
        pass

    def __getitem__(self, name):
        return _STORE.setdefault(name, _Database())
