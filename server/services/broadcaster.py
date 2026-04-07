import os
import json
import asyncio
import logging
from typing import Callable

import redis.asyncio as redis

logger = logging.getLogger(__name__)

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")


class Broadcaster:
    """Redis Pub/Sub publisher. Display and mobile endpoints subscribe per church_id."""

    def __init__(self):
        self._redis: redis.Redis | None = None
        self._available = False

    @property
    def available(self) -> bool:
        return self._available

    async def connect(self):
        try:
            self._redis = redis.from_url(REDIS_URL, decode_responses=True)
            await self._redis.ping()
            self._available = True
            logger.info("Redis connected at %s", REDIS_URL)
        except Exception as e:
            logger.warning("Redis unavailable (%s) — falling back to in-process broadcast", e)
            self._available = False
            self._local_subs: dict[str, list[Callable]] = {}

    async def disconnect(self):
        if self._redis:
            await self._redis.aclose()

    async def publish(self, church_id: str, event: dict):
        channel = f"church:{church_id}:translations"
        payload = json.dumps(event)

        if self._available and self._redis:
            await self._redis.publish(channel, payload)
        else:
            # In-process fallback for local dev without Redis
            for cb in self._local_subs.get(channel, []):
                asyncio.create_task(cb(payload))

    def subscribe_local(self, church_id: str, callback: Callable):
        """Register an in-process subscriber (used when Redis is unavailable)."""
        channel = f"church:{church_id}:translations"
        if not hasattr(self, "_local_subs"):
            self._local_subs = {}
        self._local_subs.setdefault(channel, []).append(callback)
        return lambda: self._local_subs[channel].remove(callback)

    async def subscribe(self, church_id: str):
        """Async generator that yields raw JSON strings for a church channel."""
        if not self._available or not self._redis:
            raise RuntimeError("Redis required for multi-client subscribe")

        channel = f"church:{church_id}:translations"
        pubsub = self._redis.pubsub()
        await pubsub.subscribe(channel)
        try:
            async for message in pubsub.listen():
                if message["type"] == "message":
                    yield message["data"]
        finally:
            await pubsub.unsubscribe(channel)
            await pubsub.aclose()
