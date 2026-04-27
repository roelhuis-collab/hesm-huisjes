"""
Minimal in-memory Firestore fake.

Implements only the surface used by ``state/firestore.py``:

  * ``client.collection(name).document(id).get() / .set(data, merge=...)``
  * ``client.collection(name).add(data)``  → auto-generated id
  * ``client.collection(name).where(field, op, value)`` (chainable)
  * ``client.collection(name).order_by(field).limit(n).stream()``
  * ``query.stream()`` and ``query.count().get()``
  * ``DocumentSnapshot.exists / .to_dict() / .id``

Comparison rules cover the operators we actually use: ``==`` and ``>=``.
Both are evaluated with Python's natural comparison, which works for our
ISO-string timestamps and primitive fields.
"""

from __future__ import annotations

import itertools
from typing import Any


class _Snapshot:
    def __init__(self, doc_id: str, data: dict[str, Any] | None) -> None:
        self.id = doc_id
        self._data = data

    @property
    def exists(self) -> bool:
        return self._data is not None

    def to_dict(self) -> dict[str, Any] | None:
        return None if self._data is None else dict(self._data)


class _AggregateResult:
    def __init__(self, value: int) -> None:
        self.value = value


class _CountAggregation:
    def __init__(self, count: int) -> None:
        self._count = count

    def get(self) -> list[list[_AggregateResult]]:
        # Mirrors firestore.AggregateQuery.get(): nested list with one
        # AggregationResult per stage.
        return [[_AggregateResult(self._count)]]


class _Query:
    def __init__(self, collection: _Collection, filters: list[tuple[str, str, Any]] | None = None,
                 order_field: str | None = None, limit_n: int | None = None) -> None:
        self._collection = collection
        self._filters = list(filters or [])
        self._order_field = order_field
        self._limit_n = limit_n

    def where(self, field: str, op: str, value: Any) -> _Query:
        return _Query(self._collection, [*self._filters, (field, op, value)],
                      self._order_field, self._limit_n)

    def order_by(self, field: str) -> _Query:
        return _Query(self._collection, self._filters, field, self._limit_n)

    def limit(self, n: int) -> _Query:
        return _Query(self._collection, self._filters, self._order_field, n)

    def _matches(self, data: dict[str, Any]) -> bool:
        for field, op, value in self._filters:
            actual = data.get(field)
            if op == "==":
                if actual != value:
                    return False
            elif op == ">=":
                if actual is None or actual < value:
                    return False
            elif op == "<=":
                if actual is None or actual > value:
                    return False
            else:
                raise NotImplementedError(f"fake firestore: operator {op!r} not implemented")
        return True

    def _materialize(self) -> list[_Snapshot]:
        rows = [
            (doc_id, data)
            for doc_id, data in self._collection._docs.items()
            if self._matches(data)
        ]
        order = self._order_field
        if order is not None:
            rows.sort(key=lambda kv: kv[1].get(order) or "")
        if self._limit_n is not None:
            rows = rows[: self._limit_n]
        return [_Snapshot(doc_id, data) for doc_id, data in rows]

    def stream(self) -> list[_Snapshot]:
        return self._materialize()

    def count(self) -> _CountAggregation:
        return _CountAggregation(len(self._materialize()))


class _DocumentRef:
    def __init__(self, collection: _Collection, doc_id: str) -> None:
        self._collection = collection
        self._id = doc_id

    def get(self) -> _Snapshot:
        return _Snapshot(self._id, self._collection._docs.get(self._id))

    def set(self, data: dict[str, Any], merge: bool = False) -> None:
        if merge and self._id in self._collection._docs:
            existing = dict(self._collection._docs[self._id])
            existing.update(data)
            self._collection._docs[self._id] = existing
        else:
            self._collection._docs[self._id] = dict(data)


class _Collection:
    def __init__(self, name: str) -> None:
        self.name = name
        self._docs: dict[str, dict[str, Any]] = {}
        self._auto_ids = itertools.count(1)

    def document(self, doc_id: str) -> _DocumentRef:
        return _DocumentRef(self, doc_id)

    def add(self, data: dict[str, Any]) -> tuple[Any, _DocumentRef]:
        doc_id = f"auto-{next(self._auto_ids)}"
        ref = _DocumentRef(self, doc_id)
        ref.set(data)
        return (None, ref)

    def where(self, field: str, op: str, value: Any) -> _Query:
        return _Query(self).where(field, op, value)

    def order_by(self, field: str) -> _Query:
        return _Query(self).order_by(field)

    def limit(self, n: int) -> _Query:
        return _Query(self).limit(n)

    def stream(self) -> list[_Snapshot]:
        return _Query(self).stream()


class FakeFirestore:
    """Drop-in replacement for the bits of the Firestore client we use."""

    def __init__(self) -> None:
        self._collections: dict[str, _Collection] = {}

    def collection(self, name: str) -> _Collection:
        if name not in self._collections:
            self._collections[name] = _Collection(name)
        return self._collections[name]
