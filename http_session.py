"""
http_session.py - Centralized HTTP session management with retry logic
Provides reusable aiohttp ClientSession with exponential backoff
"""

import aiohttp
import asyncio
from typing import Optional, Dict, Any
from datetime import datetime, timedelta
import logging

logger = logging.getLogger(__name__)

class HTTPSessionManager:
    """Manages a single aiohttp ClientSession with retry logic"""
    
    def __init__(
        self, 
        max_retries: int = 3,
        base_delay: float = 1.0,
        max_delay: float = 60.0,
        timeout: int = 30,
        concurrency_limit: int = 5
    ):
        self.session: Optional[aiohttp.ClientSession] = None
        self.max_retries = max_retries
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self.semaphore = asyncio.Semaphore(concurrency_limit)
        self._rate_limits: Dict[str, datetime] = {}
        
    async def init_session(self):
        """Initialize the HTTP session"""
        if self.session is None or self.session.closed:
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
            }
            self.session = aiohttp.ClientSession(
                headers=headers,
                timeout=self.timeout,
                connector=aiohttp.TCPConnector(limit=100, ttl_dns_cache=300)
            )
            logger.info("HTTP session initialized")
    
    async def close_session(self):
        """Close the HTTP session"""
        if self.session and not self.session.closed:
            await self.session.close()
            logger.info("HTTP session closed")
    
    def _calculate_backoff(self, attempt: int) -> float:
        """Calculate exponential backoff delay"""
        delay = self.base_delay * (2 ** attempt)
        return min(delay, self.max_delay)
    
    def _check_rate_limit(self, domain: str) -> bool:
        """Check if domain is rate limited"""
        if domain in self._rate_limits:
            if datetime.utcnow() < self._rate_limits[domain]:
                return False
            else:
                del self._rate_limits[domain]
        return True
    
    def _set_rate_limit(self, domain: str, retry_after: int):
        """Set rate limit for domain"""
        self._rate_limits[domain] = datetime.utcnow() + timedelta(seconds=retry_after)
    
    async def get(
        self, 
        url: str, 
        params: Optional[Dict] = None,
        headers: Optional[Dict] = None,
        retry_on_429: bool = True,
        **kwargs
    ) -> Optional[aiohttp.ClientResponse]:
        """
        Make GET request with retry logic
        
        Args:
            url: Target URL
            params: Query parameters
            headers: Additional headers
            retry_on_429: Whether to retry on 429 status
            **kwargs: Additional aiohttp options
            
        Returns:
            ClientResponse or None on failure
        """
        await self.init_session()
        
        # Extract domain for rate limiting
        from urllib.parse import urlparse
        domain = urlparse(url).netloc
        
        # Check rate limit
        if not self._check_rate_limit(domain):
            logger.warning(f"Domain {domain} is rate limited, skipping request")
            return None
        
        attempt = 0
        last_exception = None
        
        async with self.semaphore:  # Limit concurrent requests
            while attempt <= self.max_retries:
                try:
                    async with self.session.get(
                        url, 
                        params=params, 
                        headers=headers,
                        **kwargs
                    ) as response:
                        
                        # Handle rate limiting
                        if response.status == 429:
                            retry_after = int(response.headers.get('Retry-After', 60))
                            logger.warning(f"Rate limited by {domain}, retry after {retry_after}s")
                            
                            if retry_on_429 and attempt < self.max_retries:
                                self._set_rate_limit(domain, retry_after)
                                await asyncio.sleep(min(retry_after, self.max_delay))
                                attempt += 1
                                continue
                            return response
                        
                        # Success or client error (4xx except 429)
                        if response.status < 500:
                            return response
                        
                        # Server error (5xx) - retry
                        if attempt < self.max_retries:
                            delay = self._calculate_backoff(attempt)
                            logger.warning(
                                f"Server error {response.status} for {url}, "
                                f"retrying in {delay}s (attempt {attempt + 1}/{self.max_retries})"
                            )
                            await asyncio.sleep(delay)
                            attempt += 1
                        else:
                            return response
                            
                except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                    last_exception = e
                    if attempt < self.max_retries:
                        delay = self._calculate_backoff(attempt)
                        logger.warning(
                            f"Request failed: {e}, retrying in {delay}s "
                            f"(attempt {attempt + 1}/{self.max_retries})"
                        )
                        await asyncio.sleep(delay)
                        attempt += 1
                    else:
                        logger.error(f"Request failed after {self.max_retries} retries: {e}")
                        raise
        
        return None
    
    async def post(
        self, 
        url: str, 
        data: Optional[Dict] = None,
        json: Optional[Dict] = None,
        headers: Optional[Dict] = None,
        **kwargs
    ) -> Optional[aiohttp.ClientResponse]:
        """Make POST request with retry logic"""
        await self.init_session()
        
        attempt = 0
        async with self.semaphore:
            while attempt <= self.max_retries:
                try:
                    async with self.session.post(
                        url, 
                        data=data,
                        json=json,
                        headers=headers,
                        **kwargs
                    ) as response:
                        if response.status < 500:
                            return response
                        
                        if attempt < self.max_retries:
                            delay = self._calculate_backoff(attempt)
                            await asyncio.sleep(delay)
                            attempt += 1
                        else:
                            return response
                            
                except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                    if attempt < self.max_retries:
                        delay = self._calculate_backoff(attempt)
                        await asyncio.sleep(delay)
                        attempt += 1
                    else:
                        logger.error(f"POST request failed: {e}")
                        raise
        
        return None


# Global instance
_http_manager: Optional[HTTPSessionManager] = None

def get_http_manager() -> HTTPSessionManager:
    """Get or create global HTTP manager instance"""
    global _http_manager
    if _http_manager is None:
        _http_manager = HTTPSessionManager()
    return _http_manager

async def init_http_session():
    """Initialize global HTTP session"""
    manager = get_http_manager()
    await manager.init_session()
    logger.info("Global HTTP session initialized")

async def close_http_session():
    """Close global HTTP session"""
    global _http_manager
    if _http_manager:
        await _http_manager.close_session()
        _http_manager = None
        logger.info("Global HTTP session closed")
