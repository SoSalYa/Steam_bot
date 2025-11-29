"""
rate_limiter.py - Simple rate limiter for Steam API calls
Prevents hitting Steam API rate limits
"""

import asyncio
from datetime import datetime, timedelta
from typing import Dict, Optional
import logging

logger = logging.getLogger(__name__)


class RateLimiter:
    """Simple rate limiter with sliding window"""
    
    def __init__(self, max_requests: int = 100, window_seconds: int = 300):
        """
        Args:
            max_requests: Maximum requests per window
            window_seconds: Time window in seconds (default 5 min)
        """
        self.max_requests = max_requests
        self.window = timedelta(seconds=window_seconds)
        self.requests: Dict[str, list] = {}  # key -> [timestamps]
        self._lock = asyncio.Lock()
    
    async def acquire(self, key: str = "default") -> bool:
        """
        Try to acquire permission to make a request
        
        Args:
            key: Rate limit key (e.g., 'steam_api', 'discord_api')
        
        Returns:
            True if request is allowed
        """
        async with self._lock:
            now = datetime.utcnow()
            
            # Initialize key if new
            if key not in self.requests:
                self.requests[key] = []
            
            # Remove old requests outside window
            cutoff = now - self.window
            self.requests[key] = [ts for ts in self.requests[key] if ts > cutoff]
            
            # Check if we can make request
            if len(self.requests[key]) < self.max_requests:
                self.requests[key].append(now)
                return True
            else:
                # Calculate wait time
                oldest = self.requests[key][0]
                wait_until = oldest + self.window
                wait_seconds = (wait_until - now).total_seconds()
                
                logger.warning(
                    f"Rate limit reached for '{key}': "
                    f"{len(self.requests[key])}/{self.max_requests} requests. "
                    f"Wait {wait_seconds:.1f}s"
                )
                return False
    
    async def wait_if_needed(self, key: str = "default", max_wait: int = 60):
        """
        Wait until request is allowed (with timeout)
        
        Args:
            key: Rate limit key
            max_wait: Maximum seconds to wait
        
        Raises:
            TimeoutError: If wait exceeds max_wait
        """
        waited = 0
        while not await self.acquire(key):
            if waited >= max_wait:
                raise TimeoutError(f"Rate limit wait exceeded {max_wait}s for '{key}'")
            
            await asyncio.sleep(1)
            waited += 1
    
    def get_remaining(self, key: str = "default") -> int:
        """Get remaining requests in current window"""
        if key not in self.requests:
            return self.max_requests
        
        now = datetime.utcnow()
        cutoff = now - self.window
        current_requests = [ts for ts in self.requests[key] if ts > cutoff]
        
        return max(0, self.max_requests - len(current_requests))
    
    def reset(self, key: str = "default"):
        """Reset rate limit for key"""
        if key in self.requests:
            self.requests[key] = []


# Global instances
steam_api_limiter = RateLimiter(max_requests=100, window_seconds=300)  # 100 req / 5 min
discord_api_limiter = RateLimiter(max_requests=50, window_seconds=60)  # 50 req / min


async def with_steam_rate_limit(coro):
    """
    Decorator-like wrapper for Steam API calls
    
    Usage:
        result = await with_steam_rate_limit(get_price_info(appid))
    """
    await steam_api_limiter.wait_if_needed('steam_api')
    return await coro


# Example usage in your code:
"""
from rate_limiter import steam_api_limiter

async def fetch_steam_data(appid):
    if not await steam_api_limiter.acquire('steam_api'):
        await asyncio.sleep(5)  # Wait before retry
        
    # Make API call
    async with session.get(...) as resp:
        ...
"""
