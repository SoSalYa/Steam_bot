"""
redis_manager.py - Redis connection manager with leader election
Handles caching, rate limiting, and distributed locking
"""

import redis.asyncio as redis
import asyncio
import logging
import uuid
from typing import Optional
from datetime import timedelta

logger = logging.getLogger(__name__)


class RedisManager:
    """Manages Redis connection and provides helper methods"""
    
    def __init__(self, redis_url: Optional[str] = None):
        self.redis_url = redis_url
        self.client: Optional[redis.Redis] = None
        self.instance_id = str(uuid.uuid4())[:8]  # Unique instance identifier
        self._leader_tasks = {}
        
    async def connect(self):
        """Connect to Redis"""
        if not self.redis_url:
            logger.warning("No REDIS_URL provided, Redis features disabled")
            return
        
        try:
            self.client = await redis.from_url(
                self.redis_url,
                encoding="utf-8",
                decode_responses=True,
                max_connections=10
            )
            # Test connection
            await self.client.ping()
            logger.info(f"Redis connected (instance: {self.instance_id})")
        except Exception as e:
            logger.error(f"Failed to connect to Redis: {e}")
            self.client = None
    
    async def close(self):
        """Close Redis connection"""
        if self.client:
            await self.client.close()
            logger.info("Redis connection closed")
    
    def is_available(self) -> bool:
        """Check if Redis is available"""
        return self.client is not None
    
    # ========== Caching ==========
    
    async def get(self, key: str) -> Optional[str]:
        """Get value from cache"""
        if not self.is_available():
            return None
        try:
            return await self.client.get(key)
        except Exception as e:
            logger.error(f"Redis GET error: {e}")
            return None
    
    async def set(self, key: str, value: str, ex: Optional[int] = None):
        """Set value in cache with optional expiration (seconds)"""
        if not self.is_available():
            return
        try:
            await self.client.set(key, value, ex=ex)
        except Exception as e:
            logger.error(f"Redis SET error: {e}")
    
    async def delete(self, key: str):
        """Delete key from cache"""
        if not self.is_available():
            return
        try:
            await self.client.delete(key)
        except Exception as e:
            logger.error(f"Redis DELETE error: {e}")
    
    async def exists(self, key: str) -> bool:
        """Check if key exists"""
        if not self.is_available():
            return False
        try:
            return await self.client.exists(key) > 0
        except Exception as e:
            logger.error(f"Redis EXISTS error: {e}")
            return False
    
    async def ttl(self, key: str) -> int:
        """Get TTL of key"""
        if not self.is_available():
            return -1
        try:
            return await self.client.ttl(key)
        except Exception as e:
            logger.error(f"Redis TTL error: {e}")
            return -1
    
    async def setex(self, key: str, seconds: int, value: str):
        """Set key with expiration"""
        if not self.is_available():
            return
        try:
            await self.client.setex(key, seconds, value)
        except Exception as e:
            logger.error(f"Redis SETEX error: {e}")
    
    # ========== Rate Limiting ==========
    
    async def check_rate_limit(
        self, 
        key: str, 
        limit: int, 
        window: int
    ) -> tuple[bool, int]:
        """
        Check rate limit using sliding window
        
        Args:
            key: Rate limit key
            limit: Max requests in window
            window: Time window in seconds
            
        Returns:
            (allowed: bool, remaining: int)
        """
        if not self.is_available():
            return True, limit  # No rate limiting without Redis
        
        try:
            pipe = self.client.pipeline()
            now = asyncio.get_event_loop().time()
            window_start = now - window
            
            # Remove old entries
            pipe.zremrangebyscore(key, 0, window_start)
            # Count current entries
            pipe.zcard(key)
            # Add current request
            pipe.zadd(key, {str(now): now})
            # Set expiration
            pipe.expire(key, window)
            
            results = await pipe.execute()
            current_count = results[1]
            
            allowed = current_count < limit
            remaining = max(0, limit - current_count - 1)
            
            return allowed, remaining
            
        except Exception as e:
            logger.error(f"Rate limit check error: {e}")
            return True, limit  # Fail open
    
    # ========== Leader Election ==========
    
    async def acquire_leader_lock(
        self, 
        lock_name: str, 
        ttl: int = 30
    ) -> bool:
        """
        Try to acquire leader lock
        
        Args:
            lock_name: Name of the lock
            ttl: Lock TTL in seconds
            
        Returns:
            True if lock acquired
        """
        if not self.is_available():
            return True  # Single instance, always leader
        
        try:
            key = f"leader_lock:{lock_name}"
            acquired = await self.client.set(
                key, 
                self.instance_id, 
                nx=True,  # Only set if doesn't exist
                ex=ttl
            )
            
            if acquired:
                logger.info(f"Acquired leader lock: {lock_name}")
            
            return bool(acquired)
            
        except Exception as e:
            logger.error(f"Error acquiring leader lock: {e}")
            return False
    
    async def is_leader(self, lock_name: str) -> bool:
        """Check if this instance is the leader"""
        if not self.is_available():
            return True
        
        try:
            key = f"leader_lock:{lock_name}"
            current_leader = await self.client.get(key)
            return current_leader == self.instance_id
        except Exception as e:
            logger.error(f"Error checking leader status: {e}")
            return False
    
    async def renew_leader_lock(
        self, 
        lock_name: str, 
        ttl: int = 30
    ) -> bool:
        """
        Renew leader lock if this instance owns it
        
        Returns:
            True if renewed successfully
        """
        if not self.is_available():
            return True
        
        try:
            key = f"leader_lock:{lock_name}"
            
            # Only renew if we own the lock
            script = """
            if redis.call("get", KEYS[1]) == ARGV[1] then
                return redis.call("expire", KEYS[1], ARGV[2])
            else
                return 0
            end
            """
            
            result = await self.client.eval(
                script, 
                1, 
                key, 
                self.instance_id, 
                ttl
            )
            
            return bool(result)
            
        except Exception as e:
            logger.error(f"Error renewing leader lock: {e}")
            return False
    
    async def release_leader_lock(self, lock_name: str):
        """Release leader lock if owned by this instance"""
        if not self.is_available():
            return
        
        try:
            key = f"leader_lock:{lock_name}"
            
            # Only delete if we own the lock
            script = """
            if redis.call("get", KEYS[1]) == ARGV[1] then
                return redis.call("del", KEYS[1])
            else
                return 0
            end
            """
            
            await self.client.eval(
                script, 
                1, 
                key, 
                self.instance_id
            )
            
            logger.info(f"Released leader lock: {lock_name}")
            
        except Exception as e:
            logger.error(f"Error releasing leader lock: {e}")
    
    async def start_leader_heartbeat(
        self, 
        lock_name: str, 
        ttl: int = 30,
        interval: int = 10
    ):
        """
        Start background task to maintain leader lock
        
        Args:
            lock_name: Name of the lock
            ttl: Lock TTL in seconds
            interval: Renewal interval in seconds
        """
        
        async def heartbeat():
            while True:
                try:
                    if await self.is_leader(lock_name):
                        renewed = await self.renew_leader_lock(lock_name, ttl)
                        if not renewed:
                            logger.warning(f"Failed to renew leader lock: {lock_name}")
                            # Try to reacquire
                            await self.acquire_leader_lock(lock_name, ttl)
                    else:
                        # Try to become leader
                        await self.acquire_leader_lock(lock_name, ttl)
                    
                    await asyncio.sleep(interval)
                    
                except asyncio.CancelledError:
                    # Clean shutdown
                    await self.release_leader_lock(lock_name)
                    break
                except Exception as e:
                    logger.error(f"Error in leader heartbeat: {e}")
                    await asyncio.sleep(interval)
        
        task = asyncio.create_task(heartbeat())
        self._leader_tasks[lock_name] = task
        logger.info(f"Started leader heartbeat for: {lock_name}")
        return task
    
    async def stop_leader_heartbeat(self, lock_name: str):
        """Stop leader heartbeat task"""
        if lock_name in self._leader_tasks:
            task = self._leader_tasks[lock_name]
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            del self._leader_tasks[lock_name]
            logger.info(f"Stopped leader heartbeat for: {lock_name}")
    
    # ========== Distributed Locks ==========
    
    async def acquire_lock(
        self, 
        key: str, 
        ttl: int = 5
    ) -> bool:
        """
        Acquire a distributed lock
        
        Args:
            key: Lock key
            ttl: Lock TTL in seconds
            
        Returns:
            True if lock acquired
        """
        if not self.is_available():
            return True
        
        try:
            return await self.client.set(
                f"lock:{key}",
                self.instance_id,
                nx=True,
                ex=ttl
            )
        except Exception as e:
            logger.error(f"Error acquiring lock: {e}")
            return False
    
    async def release_lock(self, key: str):
        """Release a distributed lock"""
        if not self.is_available():
            return
        
        try:
            script = """
            if redis.call("get", KEYS[1]) == ARGV[1] then
                return redis.call("del", KEYS[1])
            else
                return 0
            end
            """
            
            await self.client.eval(
                script,
                1,
                f"lock:{key}",
                self.instance_id
            )
        except Exception as e:
            logger.error(f"Error releasing lock: {e}")


# Global instance
_redis_manager: Optional[RedisManager] = None


def get_redis_manager() -> RedisManager:
    """Get or create global Redis manager"""
    global _redis_manager
    if _redis_manager is None:
        import os
        redis_url = os.getenv('REDIS_URL')
        _redis_manager = RedisManager(redis_url)
    return _redis_manager


async def init_redis():
    """Initialize Redis connection"""
    manager = get_redis_manager()
    await manager.connect()


async def close_redis():
    """Close Redis connection"""
    manager = get_redis_manager()
    await manager.close()
