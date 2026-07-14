import hashlib
import time


class UrlDeduplicator:
    def __init__(self, ttl_seconds: int = 3600):
        self._ttl = ttl_seconds
        self._store: dict[str, float] = {}

    def _hash(self, url: str) -> str:
        return hashlib.sha256(url.encode("utf-8")).hexdigest()

    def _evict(self) -> None:
        now = time.time()
        expired = [k for k, ts in self._store.items() if now - ts > self._ttl]
        for k in expired:
            del self._store[k]

    def is_seen(self, url: str) -> bool:
        self._evict()
        return self._hash(url) in self._store

    def mark_seen(self, url: str) -> None:
        self._store[self._hash(url)] = time.time()
