"""
Auth Service — validates API keys from AWS Secrets Manager.
Enforces IP allowlist per key.
All keys synced via External Secrets Operator (ESO) from
AWS Secrets Manager — no static credentials in-cluster.

Runs as a FastAPI sidecar in the rag-api namespace.
Every inbound request passes through here first.
"""

import os
import json
import time
import logging
from typing import Optional
from fastapi import FastAPI, Request, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
import boto3
from botocore.exceptions import ClientError

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="Auth Service",
    description="API key validation and IP allowlist enforcement",
    version="1.0.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"]
)

# Keys are injected as env vars by ESO from Secrets Manager
# ESO ExternalSecret → Kubernetes Secret → Pod env var
VALID_API_KEYS: set = set()
IP_ALLOWLIST: dict = {}  # key → [allowed_ips]

# Load keys from environment (set by ESO)
_raw_keys = os.environ.get("API_KEYS_JSON", "{}")
try:
    _keys_dict = json.loads(_raw_keys)
    VALID_API_KEYS = set(_keys_dict.values())
    logger.info(f"Loaded {len(VALID_API_KEYS)} API keys from environment")
except json.JSONDecodeError:
    logger.error("Failed to parse API_KEYS_JSON — no keys loaded")

# IP allowlist — configurable via ConfigMap
_allowlist_raw = os.environ.get("IP_ALLOWLIST_JSON", "{}")
try:
    IP_ALLOWLIST = json.loads(_allowlist_raw)
except json.JSONDecodeError:
    IP_ALLOWLIST = {}


def get_client_ip(request: Request) -> str:
    """Extract real client IP from X-Forwarded-For or direct connection."""
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def validate_api_key(api_key: str, client_ip: str) -> bool:
    """
    Validate API key and optionally enforce IP allowlist.

    Args:
        api_key: The key from Authorization header
        client_ip: The originating IP address

    Returns:
        True if key is valid and IP is allowed
    """
    if api_key not in VALID_API_KEYS:
        logger.warning(
            f"Invalid API key attempt from {client_ip}: "
            f"{api_key[:8]}..."
        )
        return False

    # IP allowlist enforcement
    if api_key in IP_ALLOWLIST:
        allowed_ips = IP_ALLOWLIST[api_key]
        if client_ip not in allowed_ips:
            logger.warning(
                f"Valid key from unauthorized IP: "
                f"key={api_key[:8]}... ip={client_ip} "
                f"allowed={allowed_ips}"
            )
            return False

    return True


@app.get("/health")
async def health():
    """Health check for Kubernetes liveness probe."""
    return {
        "status": "healthy",
        "keys_loaded": len(VALID_API_KEYS),
        "timestamp": time.time()
    }


@app.post("/validate")
async def validate(request: Request):
    """
    Validate an inbound request's API key and IP.
    Called by the Kubernetes Ingress auth annotation or
    directly by the rate-limiter before forwarding.

    Returns 200 if valid, 401/403 if not.
    """
    api_key = request.headers.get("X-API-Key") or \
              request.headers.get("Authorization", "").replace("Bearer ", "")

    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="API key required. Pass X-API-Key header."
        )

    client_ip = get_client_ip(request)

    if not validate_api_key(api_key, client_ip):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid API key or unauthorized IP address."
        )

    logger.info(
        f"Validated request: key={api_key[:8]}... ip={client_ip}"
    )

    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={
            "valid": True,
            "client_ip": client_ip,
            "key_prefix": api_key[:8]
        }
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8001)
