"""Aggregate graphics residency, with deletion left on each graphics owner.

Device caches decline admission when their own unleased textures cannot make
room. Established full-frame shaders may run one exclusive oversized operation
and immediately retire it; they must not silently change the native reference.
"""
from itertools import count
from threading import RLock


class GraphicsResidency:
    def __init__(self, budget=512*1024*1024):
        self.budget = max(1,int(budget))
        self.lock = RLock()
        self.entries = {}
        self.exclusive = None
        self.tokens = count(1)
        self.peak_bytes = 0

    def token(self):
        with self.lock:
            return next(self.tokens)

    @property
    def bytes(self):
        with self.lock:
            return sum(self.entries.values())

    def change(self, owner, size, *, allow_exclusive=False):
        size = max(0,int(size))
        with self.lock:
            previous = self.entries.get(owner,0)
            total = sum(self.entries.values())-previous+size
            if size > previous:
                if self.exclusive is not None and self.exclusive != owner:
                    return False
                if total > self.budget:
                    if not allow_exclusive:
                        return False
                    self.exclusive = owner
            if size:
                self.entries[owner] = size
            else:
                self.entries.pop(owner,None)
                if self.exclusive == owner:
                    self.exclusive = None
            self.peak_bytes = max(self.peak_bytes,total)
            return True


GRAPHICS_RESIDENCY = GraphicsResidency()
