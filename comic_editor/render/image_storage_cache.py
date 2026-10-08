"""Owned, bounded QImage handles with one charge per shared COW storage.

Storage identities only account process-local ownership. They do not replace
semantic cache keys, content/dependency identities, or persistent validation.
"""
from collections import OrderedDict
from collections.abc import MutableMapping

from PySide6.QtGui import QImage


class QImageStorageCache(MutableMapping):
    """LRU image mapping; returned handles cannot mutate the retained handle.

    Equal Qt cache keys on independently owned QImage handles identify shared
    immutable pixel storage. Every insertion takes a COW handle, every read
    returns a COW handle, and no retained handle escapes through values/items.
    A writer therefore detaches instead of changing the storage ledger.
    """
    def __init__(self, budget=64 * 1024 * 1024, limit=512, *, pool=None,
                 reject_oversized=False, metadata_copy=None, allow_null=False,
                 metadata_identity=None):
        if budget < 0 or limit < 1:
            raise ValueError('Invalid QImage cache bounds')
        self._budget = int(budget)
        self._limit = int(limit)
        self.bytes = 0
        self._entries = OrderedDict()
        self._storage = {}
        self._protected_aliases = OrderedDict()
        self.pool = pool
        self._reject_oversized = reject_oversized
        self._allow_null = allow_null
        self._metadata_copy = metadata_copy or (lambda value: value)
        self._metadata_identity = metadata_identity
        self._pool_id = pool.register(self, budget) if pool is not None else None

    @property
    def budget(self):
        return self._budget

    @budget.setter
    def budget(self, value):
        if value < 0:
            raise ValueError('Invalid QImage cache budget')
        self._budget = int(value)
        if self.pool is not None:
            if self._reject_oversized:
                for key in tuple(self._entries):
                    if self._storage[self._entries[key][0]][0] > self._budget:
                        del self[key]
            self.pool.contribution(self._pool_id, self._budget)

    @property
    def limit(self):
        return self._limit

    @limit.setter
    def limit(self, value):
        if value < 1:
            raise ValueError('Invalid QImage cache record limit')
        self._limit = int(value)
        self._spill_aliases()
        while len(self._entries) > self._limit:
            if self.pool is not None and self.pool.segmented:
                del self[self._metadata_victim()]
            else:
                self.popitem(last=False)

    @property
    def protected_limit(self):
        return self.limit * 2 // 3 if self.pool is not None and self.pool.segmented else 0

    def _demote_alias(self, key):
        self._protected_aliases.pop(key, None)

    def _spill_aliases(self):
        while len(self._protected_aliases) > self.protected_limit:
            self._protected_aliases.popitem(last=False)

    def _metadata_victim(self):
        if self.pool is not None and self.pool.segmented:
            for key in self._entries:
                if key not in self._protected_aliases:
                    return key
            return next(iter(self._protected_aliases))
        return next(iter(self._entries))

    def reuse(self, key):
        """Promote one alias only after its semantic key/state was validated."""
        if self.pool is None or not self.pool.segmented:
            return False
        if not self.pool.reuse(self._entries[key][0]):
            return False
        self._protected_aliases[key] = None
        self._protected_aliases.move_to_end(key)
        self._spill_aliases()
        return key in self._protected_aliases

    def protected_aliases(self):
        """Detached prior history with an owned image and semantic identity."""
        return tuple((key, self.peek(key), self._metadata_identity(self.metadata(key))
                      if self._metadata_identity is not None else None)
                     for key in self._protected_aliases)

    def restore_aliases(self, values):
        """Restore only admitted, still-identical aliases of protected nodes."""
        self._protected_aliases.clear()
        if self.pool is None or not self.pool.segmented:
            return
        for key, image, identity in values:
            entry = self._entries.get(key)
            if entry is None or entry[0] != int(image.cacheKey()) or entry[0] not in self.pool._protected:
                continue
            actual = self._metadata_identity(entry[1]) if self._metadata_identity is not None else None
            if actual == identity:
                self._protected_aliases[key] = None
        self._spill_aliases()

    def __len__(self):
        return len(self._entries)

    def __iter__(self):
        return iter(tuple(self._entries) if self.pool is not None else self._entries)

    def __contains__(self, key):
        return key in self._entries

    def __getitem__(self, key):
        if self.pool is not None:
            image = self.peek(key)
            self.move_to_end(key)
            return image
        return QImage(self._entries[key][0])

    def peek(self, key):
        """Owned inspection/transfer handle without a semantic cache hit."""
        return self.pool.image(self._entries[key][0]) if self.pool is not None else self[key]

    def items(self):
        if self.pool is None:
            return super().items()
        return ((key, self.peek(key)) for key in tuple(self._entries))

    def values(self):
        if self.pool is None:
            return super().values()
        return (self.peek(key) for key in tuple(self._entries))

    def export(self):
        return OrderedDict(self.items())

    def metadata(self, key):
        return self._metadata_copy(self._entries[key][1])

    def reorder(self, keys):
        """Restore local metadata order without changing global reuse order."""
        for key in keys:
            if key in self._entries:
                self._entries.move_to_end(key)

    def __setitem__(self, key, image):
        self.store(key, image)

    def store(self, key, image, *, metadata=None):
        """Admit ordinary aliases, or one oversized storage exclusively."""
        owned = QImage(image)
        size = int(owned.sizeInBytes())
        if size <= 0 and not (self.pool is not None and self._allow_null):
            return False
        if self.pool is not None:
            owned_metadata = self._metadata_copy(metadata)
            storage = int(owned.cacheKey())
            previous = self._entries.get(key)
            if self.pool.segmented and previous is not None and previous[0] == storage:
                # Refreshing an already owned allocation is bookkeeping.
                # Preserve a prior qualified read without promoting a new
                # alias or temporarily losing the node's last ownership.
                same_identity = (self._metadata_identity is None or
                    self._metadata_identity(previous[1]) == self._metadata_identity(owned_metadata))
                if self._reject_oversized and size > self.budget:
                    del self[key]
                    return False
                if not same_identity:
                    self._demote_alias(key)
                self._entries[key] = storage, owned_metadata
                self.move_to_end(key)
                return True
            if key in self._entries:
                del self[key]
            if self._reject_oversized and size > self.budget:
                return False
            while len(self._entries) >= self.limit:
                if self.pool.segmented:
                    del self[self._metadata_victim()]
                else:
                    self.popitem(last=False)
            storage, size = self.pool.attach(self, key, owned)
            self._entries[key] = storage, owned_metadata
            if storage in self._storage:
                self._storage[storage][1] += 1
            else:
                self._storage[storage] = [size, 1]
                self.bytes += size
            return True
        storage = int(owned.cacheKey())
        if key in self._entries:
            del self[key]
        while self._entries and (
            self.bytes + (0 if storage in self._storage else size)
                > max(self.budget, size)
            or len(self._entries) >= self.limit
        ):
            self.popitem(last=False)
        self._entries[key] = owned, storage
        if storage in self._storage:
            self._storage[storage][1] += 1
        else:
            self._storage[storage] = [size, 1]
            self.bytes += size
        return True

    def __delitem__(self, key):
        if self.pool is not None:
            storage = self._entries[key][0]
            self._pool_evict(key, storage)
            self.pool.detach(self._pool_id, key, storage)
            return
        _image, storage = self._entries.pop(key)
        size, references = self._storage[storage]
        if references == 1:
            del self._storage[storage]
            self.bytes -= size
        else:
            self._storage[storage][1] = references - 1

    def _pool_evict(self, key, storage):
        self._entries.pop(key)
        self._demote_alias(key)
        size, references = self._storage[storage]
        if references == 1:
            self.bytes -= size
            del self._storage[storage]
        else:
            self._storage[storage][1] -= 1

    def popitem(self, last=True):
        if not self._entries:
            raise KeyError('cache is empty')
        key = next(reversed(self._entries) if last else iter(self._entries))
        image = self.peek(key) if self.pool is not None else self[key]
        del self[key]
        return key, image

    def move_to_end(self, key, last=True):
        self._entries.move_to_end(key, last=last)
        if self.pool is not None:
            self.pool.touch(self._entries[key][0], last=last)

    def clear(self):
        if self.pool is not None:
            for key in tuple(self._entries):
                del self[key]
            return
        self._entries.clear()
        self._storage.clear()
        self.bytes = 0
