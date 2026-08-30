"""
Metrics Exporter — wraps the RAG API and exposes Prometheus metrics.
Extracts token counts, latency, queue depth, and error rates.
Scraped by Prometheus every 15 seconds.

Sits as a sidecar to rag-api, proxying all requests and
adding observability without modifying the RAG API itself.
"""

import os
import time
import logging
import asyncio
from collections import deque
from fastapi import FastAPI, Request, Response
from prometheus_client import (
    Counter, Histogram, Gauge,
    generate_latest, CONTENT_TYPE_LATEST
)
import httpx

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="Metrics Exporter",
    description="Prometheus metrics for RAG API",
    version="1.0.0"
)

RAG_API_URL = os.environ.get("RAG_API_URL", "http://localhost:8000")

# -------------------------------------------------------
# PROMETHEUS METRICS
# -------------------------------------------------------

# Request counters
REQUEST_TOTAL = Counter(
    "rag_requests_total",
    "Total RAG API requests",
    ["method", "status_code", "endpoint"]
)

# Latency histogram — p50, p95, p99
REQUEST_LATENCY = Histogram(
    "rag_request_duration_seconds",
    "RAG API request duration",
    ["endpoint"],
    buckets=[0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0]
)

# LLM-specific token metrics
PROMPT_TOKENS = Counter(
    "rag_prompt_tokens_total",
    "Total prompt tokens sent to LLM"
)

COMPLETION_TOKENS = Counter(
    "rag_completion_tokens_total",
    "Total completion tokens received from LLM"
)

TOTAL_TOKENS = Counter(
    "rag_total_tokens_total",
    "Total tokens (prompt + completion)"
)

# Estimated cost metric (recording rule multiplies by price/token)
TOKEN_COST_ESTIMATE = Counter(
    "rag_token_cost_usd_total",
    "Estimated cost in USD (based on Claude Sonnet pricing)"
)

# Queue depth — how many requests are in flight
QUEUE_DEPTH = Gauge(
    "rag_queue_depth",
    "Number of requests currently being processed"
)

# Qdrant retrieval metrics
RETRIEVAL_LATENCY = Histogram(
    "rag_retrieval_duration_seconds",
    "Qdrant vector search duration",
    buckets=[0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5]
)

CHUNKS_RETRIEVED = Histogram(
    "rag_chunks_retrieved",
    "Number of document chunks retrieved per query",
    buckets=[1, 2, 3, 5, 10, 20]
)

# Claude Sonnet pricing (as of 2024)
# Update via ConfigMap if pricing changes
CLAUDE_PROMPT_PRICE_PER_TOKEN = float(
    os.environ.get("CLAUDE_PROMPT_PRICE_PER_1K", "0.003")
) / 1000

CLAUDE_COMPLETION_PRICE_PER_TOKEN = float(
    os.environ.get("CLAUDE_COMPLETION_PRICE_PER_1K", "0.015")
) / 1000


@app.get("/health")
async def health():
    """Health check."""
    return {"status": "healthy", "proxying_to": RAG_API_URL}


@app.get("/metrics")
async def metrics():
    """Prometheus metrics endpoint — scraped by kube-prometheus-stack."""
    return Response(
        content=generate_latest(),
        media_type=CONTENT_TYPE_LATEST
    )


@app.api_route(
    "/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH"]
)
async def proxy(request: Request, path: str):
    """
    Transparent proxy to RAG API.
    Measures latency, extracts token counts from response,
    and records all metrics before returning response to client.
    """
    start_time = time.time()
    QUEUE_DEPTH.inc()

    try:
        # Forward request to RAG API
        body = await request.body()
        headers = dict(request.headers)
        headers.pop("host", None)

        async with httpx.AsyncClient(timeout=120.0) as client:
            response = await client.request(
                method=request.method,
                url=f"{RAG_API_URL}/{path}",
                headers=headers,
                content=body,
                params=dict(request.query_params)
            )

        latency = time.time() - start_time

        # Record request metrics
        REQUEST_TOTAL.labels(
            method=request.method,
            status_code=response.status_code,
            endpoint=f"/{path}"
        ).inc()

        REQUEST_LATENCY.labels(endpoint=f"/{path}").observe(latency)

        # Extract token usage from response body if it's a RAG query
        if path in ["query", "search"] and response.status_code == 200:
            try:
                response_data = response.json()
                usage = response_data.get("usage", {})

                prompt_tokens = usage.get("prompt_tokens", 0)
                completion_tokens = usage.get("completion_tokens", 0)
                total_tokens = usage.get("total_tokens", 0)

                PROMPT_TOKENS.inc(prompt_tokens)
                COMPLETION_TOKENS.inc(completion_tokens)
                TOTAL_TOKENS.inc(total_tokens)

                # Estimate cost
                cost = (
                    prompt_tokens * CLAUDE_PROMPT_PRICE_PER_TOKEN +
                    completion_tokens * CLAUDE_COMPLETION_PRICE_PER_TOKEN
                )
                TOKEN_COST_ESTIMATE.inc(cost)

                # Retrieval metrics
                retrieval_time = response_data.get("retrieval_time_ms", 0)
                if retrieval_time:
                    RETRIEVAL_LATENCY.observe(retrieval_time / 1000)

                chunks = response_data.get("chunks_retrieved", 0)
                if chunks:
                    CHUNKS_RETRIEVED.observe(chunks)

            except Exception as e:
                logger.warning(f"Could not extract metrics from response: {e}")

        return Response(
            content=response.content,
            status_code=response.status_code,
            headers=dict(response.headers)
        )

    except Exception as e:
        latency = time.time() - start_time
        REQUEST_TOTAL.labels(
            method=request.method,
            status_code=500,
            endpoint=f"/{path}"
        ).inc()
        REQUEST_LATENCY.labels(endpoint=f"/{path}").observe(latency)
        logger.error(f"Proxy error for /{path}: {e}")
        return Response(
            content=f'{{"error": "Upstream error: {str(e)}"}}',
            status_code=502,
            media_type="application/json"
        )

    finally:
        QUEUE_DEPTH.dec()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8003)
