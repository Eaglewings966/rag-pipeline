"""
Rate Limiter — sliding window, 10 requests per minute per API key.
Uses Redis for distributed state across replicas.
Returns 429 with Retry-After header on limit breach.

Sits between auth-service and rag-api in the request pipeline.
"""

import os
import time
import logging
from fastapi import FastAPI, Request, HTTPException, status
from fastapi.responses import JSONResponse
import redis.asyncio as aioredis

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="Rate Limiter",
    description="Sliding window rate limiter — 10 req/min per API key",
    version="1.0.0"
)

REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379/0")
RATE_LIMIT = int(os.environ.get("RATE_LIMIT_PER_MINUTE", "10"))
WINDOW_SECONDS = 60

redis_client: aioredis.Redis = None


@app.on_event("startup")
async def startup():
    global redis_client
    redis_client = aioredis.from_url(
        REDIS_URL,
        encoding="utf-8",
        decode_responses=True
    )
    logger.info(f"Redis connected: {REDIS_URL}")
    logger.info(f"Rate limit: {RATE_LIMIT} req/{WINDOW_SECONDS}s per key")


@app.on_event("shutdown")
async def shutdown():
    if redis_client:
        await redis_client.close()


async def sliding_window_check(api_key: str) -> tuple[bool, int, int]:
    """
    Sliding window rate limit check using Redis sorted sets.

    Each API key has a sorted set where:
    - Member = request timestamp (nanoseconds for uniqueness)
    - Score  = request timestamp (seconds for windowing)

    Algorithm:
    1. Remove members older than the window
    2. Count remaining members
    3. If count < limit: add new member and allow
    4. If count >= limit: reject with retry time

    Args:
        api_key: The API key to check

    Returns:
        Tuple of (allowed, current_count, retry_after_seconds)
    """
    now = time.time()
    window_start = now - WINDOW_SECONDS
    redis_key = f"rate_limit:{api_key}"

    pipe = redis_client.pipeline()

    # Remove expired entries
    pipe.zremrangebyscore(redis_key, 0, window_start)

    # Count current entries
    pipe.zcard(redis_key)

    # Add current request
    unique_member = f"{now}:{id(pipe)}"
    pipe.zadd(redis_key, {unique_member: now})

    # Set expiry on the key
    pipe.expire(redis_key, WINDOW_SECONDS * 2)

    results = await pipe.execute()
    current_count = results[1]

    if current_count >= RATE_LIMIT:
        # Calculate when the oldest request will expire
        oldest = await redis_client.zrange(redis_key, 0, 0, withscores=True)
        if oldest:
            oldest_time = oldest[0][1]
            retry_after = int(oldest_time + WINDOW_SECONDS - now) + 1
        else:
            retry_after = WINDOW_SECONDS

        # Remove the request we just added since we're rejecting
        await redis_client.zrem(redis_key, unique_member)

        return False, current_count, retry_after

    return True, current_count + 1, 0


@app.get("/health")
async def health():
    """Health check — also verifies Redis connectivity."""
    try:
        await redis_client.ping()
        return {
            "status": "healthy",
            "redis": "connected",
            "rate_limit": f"{RATE_LIMIT} req/min"
        }
    except Exception as e:
        return JSONResponse(
            status_code=503,
            content={"status": "unhealthy", "redis": str(e)}
        )


@app.post("/check")
async def check_rate_limit(request: Request):
    """
    Check if an API key is within its rate limit.
    Returns 200 if allowed, 429 if exceeded.

    Called by rag-api before processing each request.
    """
    api_key = request.headers.get("X-API-Key") or \
              request.headers.get("Authorization", "").replace("Bearer ", "")

    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="API key required"
        )

    allowed, count, retry_after = await sliding_window_check(api_key)

    if not allowed:
        logger.warning(
            f"Rate limit exceeded: key={api_key[:8]}... "
            f"count={count} limit={RATE_LIMIT} "
            f"retry_after={retry_after}s"
        )
        return JSONResponse(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            headers={"Retry-After": str(retry_after)},
            content={
                "error": "Rate limit exceeded",
                "limit": RATE_LIMIT,
                "window": f"{WINDOW_SECONDS}s",
                "retry_after": retry_after
            }
        )

    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={
            "allowed": True,
            "current_count": count,
            "limit": RATE_LIMIT,
            "remaining": RATE_LIMIT - count
        }
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8002)
