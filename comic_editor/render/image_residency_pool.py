"""Evaluator-local immutable QImage residency; semantic maps stay independent.

The process-local Qt storage identity never enters persistent keys. A node owns
one private COW handle. Byte pressure retires every alias of its selected node;
map metadata pressure retires only one alias. Borrowed handles remain valid.
"""
from collections import OrderedDict
import weakref

from PySide6.QtGui import QImage


class QImageResidencyPool:
    """Sum of nominal role contributions, with one exclusive oversized node.

This is retained mapped ownership, not a process/RSS or working-admission cap.
The pool never owns a scene, callback, admission ticket or durable descriptor.
"""
    def __init__(self, *, segmented=False):
        self.bytes = 0
        self._nodes = OrderedDict()
        self.segmented = bool(segmented)
        self._protected = OrderedDict()
        self.protected_bytes = 0
        self._maps = {}
        self._contributions = {}
        self._next_map = 0

    @property
    def budget(self):
        return sum(self._contributions.values())

    @property
    def node_count(self):
        return len(self._nodes)

    @property
    def pixel_node_count(self):
        # Null checkpoints are valid key/state records with no pixel storage.
        return sum(node[1] > 0 for node in self._nodes.values())

    @property
    def alias_count(self):
        return sum(len(node[2]) for node in self._nodes.values())

    @property
    def protected_budget(self):
        return self.budget * 2 // 3 if self.segmented else 0

    @property
    def protected_node_count(self):
        return len(self._protected)

    def reuse(self, storage):
        """A validated semantic read, never an insertion or accounting read."""
        if not self.segmented:
            return False
        size = self._nodes[storage][1]
        if size > self.protected_budget:
            return False
        self.touch(storage)
        if storage not in self._protected:
            self._protected[storage] = None
            self.protected_bytes += size
        self._protected.move_to_end(storage)
        self._spill_protected()
        return storage in self._protected

    def _demote(self, storage):
        self._protected.pop(storage)
        node = self._nodes[storage]
        self.protected_bytes -= node[1]
        # A global demotion also makes its role aliases probationary. No
        # handles, semantic identities, bytes or aliases change here.
        for identifier, key in node[2]:
            reference = self._maps.get(identifier)
            mapping = reference() if reference is not None else None
            if mapping is not None:
                mapping._demote_alias(key)
        self._nodes.move_to_end(storage)

    def _spill_protected(self):
        while self.protected_bytes > self.protected_budget:
            self._demote(next(iter(self._protected)))

    def _forget_protected(self, storage, size):
        if storage in self._protected:
            self._protected.pop(storage)
            self.protected_bytes -= size

    def _byte_victim(self):
        if not self.segmented:
            return next(iter(self._nodes))
        # Null checkpoint nodes cannot relieve byte pressure. They retain
        # their ordinary per-role metadata bound and exclusive image policy.
        for storage, node in self._nodes.items():
            if node[1] > 0 and storage not in self._protected:
                return storage
        return next(storage for storage in self._protected if self._nodes[storage][1] > 0)

    def register(self, mapping, contribution):
        identifier = self._next_map
        self._next_map += 1
        pool_ref = weakref.ref(self)
        def retired(_reference):
            pool = pool_ref()
            if pool is not None:
                pool._unregister(identifier)
        self._maps[identifier] = weakref.ref(mapping, retired)
        self._contributions[identifier] = int(contribution)
        return identifier

    def contribution(self, identifier, value):
        if self._contributions[identifier] == int(value):
            return
        self._contributions[identifier] = int(value)
        self._spill_protected()
        self._trim()

    def _unregister(self, identifier):
        for storage, node in tuple(self._nodes.items()):
            node[2].difference_update(alias for alias in tuple(node[2]) if alias[0] == identifier)
            if not node[2]:
                self.bytes -= node[1]
                self._forget_protected(storage, node[1])
                del self._nodes[storage]
        self._maps.pop(identifier, None)
        self._contributions.pop(identifier, None)
        self._spill_protected()
        self._trim()

    def image(self, storage):
        return QImage(self._nodes[storage][0])

    def touch(self, storage, last=True):
        self._nodes.move_to_end(storage, last=last)

    def attach(self, mapping, key, owned):
        storage, size = int(owned.cacheKey()), int(owned.sizeInBytes())
        if storage not in self._nodes:
            # An image larger than the complete allowance replaces every node.
            # Any later distinct image also replaces that exclusive exception.
            while size > 0 and self._nodes and self.bytes + size > max(self.budget, size):
                self._evict(self._byte_victim())
            self._nodes[storage] = [QImage(owned), size, set()]
            self.bytes += size
        self._nodes[storage][2].add((mapping._pool_id, key))
        self.touch(storage)
        return storage, size

    def detach(self, identifier, key, storage):
        node = self._nodes[storage]
        node[2].remove((identifier, key))
        if not node[2]:
            self.bytes -= node[1]
            self._forget_protected(storage, node[1])
            del self._nodes[storage]

    def _evict(self, storage):
        _image, size, aliases = self._nodes.pop(storage)
        self.bytes -= size
        self._forget_protected(storage, size)
        for identifier, key in aliases:
            reference = self._maps.get(identifier)
            mapping = reference() if reference is not None else None
            if mapping is not None:
                mapping._pool_evict(key, storage)

    def _trim(self):
        # Preserve the existing completed source/effect handoff exception.
        while self.bytes > self.budget and self.pixel_node_count > 1:
            self._evict(self._byte_victim())

    def clear(self):
        while self._nodes:
            self._evict(next(iter(self._nodes)))

    def storage_order(self):
        """Transient transfer handles, never serialized IDs or shared ledgers."""
        return tuple(QImage(node[0]) for node in self._nodes.values())

    def protected_storage(self):
        """Owned history handles; exporting never touches or promotes storage."""
        return tuple(self.image(storage) for storage in self._protected)

    def restore_protected(self, values):
        """Rehydrate prior history after admission, without claiming a read."""
        for storage in tuple(self._protected):
            self._demote(storage)
        if not self.segmented:
            return
        for image in values:
            storage = int(image.cacheKey())
            node = self._nodes.get(storage)
            if node is not None and storage not in self._protected and node[1] <= self.protected_budget:
                self._protected[storage] = None
                self.protected_bytes += node[1]
        self._spill_protected()
