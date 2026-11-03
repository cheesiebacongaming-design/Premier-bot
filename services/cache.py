"""Persistent async cache with stale fallback and atomic JSON snapshots."""

import asyncio
import json
import logging
import os
import tempfile
import time
from pathlib import Path


logger = logging.getLogger(__name__)


class _KeyLock:
    def __init__(self):
        self.lock = asyncio.Lock()
        self.users = 0


class JsonCache:
    """A disk-backed cache that keeps the legacy ``{key: {t, ttl, data}}`` format."""

    def __init__(self, path, stale_retention=30 * 86400, cleanup_interval=6 * 3600):
        self.path = Path(path)
        self.stale_retention = stale_retention
        self.cleanup_interval = cleanup_interval
        self.entries = {}
        self._locks = {}
        self._save_task = None
        self._generation = 0
        self._last_cleanup = 0
        self._writable = True
        self._load()
        inventory = self.stats()
        logger.info(
            "Cache inventory: %d total (%d permanent, %d expiring, %d malformed); disk writes %s",
            inventory["entries"],
            inventory["permanent"],
            inventory["expiring"],
            inventory["malformed"],
            "enabled" if inventory["disk_writes_enabled"] else "disabled",
        )

    def _load(self):
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return
        except UnicodeDecodeError as exc:
            self._writable = False
            logger.error("Cache file %s is not valid UTF-8; preserving it and disabling disk writes: %s", self.path, exc)
            return
        except OSError as exc:
            self._writable = False
            logger.error("Cache file %s could not be read; disk writes disabled: %s", self.path, exc)
            return

        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            # Keep the original file intact and avoid replacing it with an empty cache.
            self._writable = False
            logger.error("Cache file %s is invalid; preserving it and disabling disk writes: %s", self.path, exc)
            return

        if not isinstance(data, dict):
            self._writable = False
            logger.error("Cache file %s has an unexpected top-level format; disk writes disabled", self.path)
            return
        self.entries = data
        logger.info("Loaded %d cache entries from %s", len(self.entries), self.path)

    def _entry_is_valid(self, entry):
        return isinstance(entry, dict) and "t" in entry and "ttl" in entry and "data" in entry

    def _is_fresh(self, entry, now):
        if not self._entry_is_valid(entry):
            return False
        if entry["ttl"] is None:
            return True
        try:
            return now - float(entry["t"]) < float(entry["ttl"])
        except (TypeError, ValueError):
            return False

    def _prune_expired(self, now):
        if now - self._last_cleanup < self.cleanup_interval:
            return 0
        self._last_cleanup = now
        expired_keys = []
        for key, entry in self.entries.items():
            if not self._entry_is_valid(entry) or entry["ttl"] is None:
                continue
            try:
                is_too_old = now - float(entry["t"]) > float(entry["ttl"]) + self.stale_retention
            except (TypeError, ValueError):
                continue
            if is_too_old:
                expired_keys.append(key)
        for key in expired_keys:
            del self.entries[key]
        if expired_keys:
            logger.info("Pruned %d expired cache entries", len(expired_keys))
            self._schedule_save()
        return len(expired_keys)

    async def get(self, key, ttl, fetch):
        now = time.time()
        self._prune_expired(now)

        keyed_lock = self._locks.get(key)
        if keyed_lock is None:
            keyed_lock = _KeyLock()
            self._locks[key] = keyed_lock
        keyed_lock.users += 1
        try:
            async with keyed_lock.lock:
                hit = self.entries.get(key)
                valid_hit = self._entry_is_valid(hit)
                now = time.time()
                if self._is_fresh(hit, now):
                    return hit["data"]
                try:
                    data = await fetch()
                except Exception as exc:
                    if valid_hit:
                        logger.warning("Serving stale cache data after refresh failed (%s)", type(exc).__name__)
                        return hit["data"]
                    raise
                self.entries[key] = {"t": time.time(), "ttl": ttl, "data": data}
                self._schedule_save()
                return data
        finally:
            keyed_lock.users -= 1
            if keyed_lock.users == 0 and self._locks.get(key) is keyed_lock:
                del self._locks[key]

    def peek(self, key):
        """Return a cache entry without fetching; used for freshness diagnostics."""
        return self.entries.get(key)

    def clear_expiring(self):
        """Match the existing /clearcache behavior: retain permanent entries."""
        keys = [
            key for key, entry in self.entries.items()
            if self._entry_is_valid(entry) and entry["ttl"] is not None
        ]
        for key in keys:
            del self.entries[key]
        if keys:
            self._schedule_save()
        return len(keys)

    def stats(self):
        permanent = expiring = malformed = 0
        for entry in self.entries.values():
            if not self._entry_is_valid(entry):
                malformed += 1
            elif entry["ttl"] is None:
                permanent += 1
            else:
                expiring += 1
        return {
            "entries": len(self.entries),
            "permanent": permanent,
            "expiring": expiring,
            "malformed": malformed,
            "active_key_locks": len(self._locks),
            "disk_writes_enabled": self._writable,
        }

    def _schedule_save(self):
        if not self._writable:
            return
        self._generation += 1
        if self._save_task is None or self._save_task.done():
            self._save_task = asyncio.create_task(self._flush())

    async def _flush(self):
        await asyncio.sleep(0.3)
        loop = asyncio.get_running_loop()
        while True:
            generation = self._generation
            snapshot = dict(self.entries)
            try:
                await loop.run_in_executor(None, self._atomic_write, snapshot)
            except Exception:
                # Serialization and filesystem failures both leave the previous
                # snapshot intact; keep the cache usable in memory.
                logger.exception("Could not persist cache file %s", self.path)
                return
            if generation == self._generation:
                return
            await asyncio.sleep(0.05)

    def _atomic_write(self, snapshot):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{self.path.name}.", suffix=".tmp", dir=str(self.path.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as output:
                json.dump(snapshot, output, separators=(",", ":"))
                output.flush()
                os.fsync(output.fileno())
            os.replace(temp_name, self.path)
        except Exception:
            try:
                os.unlink(temp_name)
            except OSError:
                pass
            raise

    async def flush_pending(self):
        task = self._save_task
        if task is not None and not task.done():
            await task
