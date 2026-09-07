"""
RAG API — Production Retrieval-Augmented Generation pipeline.
LangChain abstraction makes this provider-agnostic.
Default: Claude claude-sonnet-4-6 via Anthropic API.
Swap provider by setting LLM_PROVIDER=openai env var.

Pipeline:
1. Receive query
2. Retrieve relevant chunks from Qdrant
3. Build context from chunks
4. Send to LLM via LangChain
5. Return answer + sources + usage + retrieval_time
"""

import os
import time
import logging
import uuid
from typing import Optional, List, Dict, Any
from fastapi import FastAPI, HTTPException, Depends, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
import httpx
from langchain_anthropic import ChatAnthropic
from langchain_openai import ChatOpenAI
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import StrOutputParser
from langchain_qdrant import QdrantVectorStore
from langchain_huggingface import HuggingFaceEmbeddings
from qdrant_client import QdrantClient
from qdrant_client.http.models import Distance, VectorParams

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="RAG API",
    description=(
        "Production Retrieval-Augmented Generation pipeline. "
        "LangChain abstraction — provider-agnostic. "
        "Project 15 — Tech with Emma AI Infrastructure."
    ),
    version="1.0.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"]
)

# -------------------------------------------------------
# CONFIGURATION
# -------------------------------------------------------
QDRANT_URL = os.environ.get("QDRANT_URL", "http://qdrant:6333")
QDRANT_COLLECTION = os.environ.get("QDRANT_COLLECTION", "documents")
EMBEDDING_MODEL = os.environ.get(
    "EMBEDDING_MODEL",
    "sentence-transformers/all-MiniLM-L6-v2"
)
LLM_PROVIDER = os.environ.get("LLM_PROVIDER", "anthropic")
CLAUDE_API_KEY = os.environ.get("CLAUDE_API_KEY", "")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
TOP_K_CHUNKS = int(os.environ.get("TOP_K_CHUNKS", "5"))
SIMILARITY_THRESHOLD = float(os.environ.get("SIMILARITY_THRESHOLD", "0.7"))
AUTH_SERVICE_URL = os.environ.get("AUTH_SERVICE_URL", "http://auth-service.rag-api.svc.cluster.local/validate")
RATE_LIMITER_URL = os.environ.get("RATE_LIMITER_URL", "http://rate-limiter.rag-api.svc.cluster.local/check")

# -------------------------------------------------------
# INITIALIZATION
# -------------------------------------------------------
embeddings = None
qdrant_store = None
llm = None
qdrant_client = None

RAG_PROMPT = ChatPromptTemplate.from_template("""
You are a helpful AI assistant. Answer the question based only on
the provided context. If the context does not contain enough
information to answer the question, say so clearly.

Context:
{context}

Question: {question}

Answer:""")


@app.on_event("startup")
async def startup():
    global embeddings, qdrant_store, llm, qdrant_client

    logger.info("Initializing RAG API...")

    # Initialize embeddings — self-hosted, no external API call
    logger.info(f"Loading embedding model: {EMBEDDING_MODEL}")
    embeddings = HuggingFaceEmbeddings(
        model_name=EMBEDDING_MODEL,
        model_kwargs={"device": "cpu"},
        encode_kwargs={"normalize_embeddings": True}
    )
    logger.info("Embedding model loaded")

    # Initialize Qdrant client
    qdrant_client = QdrantClient(url=QDRANT_URL)

    # Create collection if it doesn't exist
    try:
        qdrant_client.get_collection(QDRANT_COLLECTION)
        logger.info(f"Qdrant collection exists: {QDRANT_COLLECTION}")
    except Exception:
        logger.info(f"Creating Qdrant collection: {QDRANT_COLLECTION}")
        qdrant_client.create_collection(
            collection_name=QDRANT_COLLECTION,
            vectors_config=VectorParams(
                size=384,  # all-MiniLM-L6-v2 dimension
                distance=Distance.COSINE
            )
        )

    # Initialize vector store
    qdrant_store = QdrantVectorStore(
        client=qdrant_client,
        collection_name=QDRANT_COLLECTION,
        embedding=embeddings
    )

    # Initialize LLM — provider-agnostic via LangChain
    if LLM_PROVIDER == "anthropic":
        if not CLAUDE_API_KEY:
            raise RuntimeError(
                "CLAUDE_API_KEY not set. "
                "Check External Secrets Operator sync."
            )
        llm = ChatAnthropic(
            model="claude-sonnet-4-6",
            api_key=CLAUDE_API_KEY,
            temperature=0.1,
            max_tokens=2048
        )
        logger.info("LLM: Claude claude-sonnet-4-6 (Anthropic)")

    elif LLM_PROVIDER == "openai":
        if not OPENAI_API_KEY:
            raise RuntimeError("OPENAI_API_KEY not set")
        llm = ChatOpenAI(
            model="gpt-4o",
            api_key=OPENAI_API_KEY,
            temperature=0.1,
            max_tokens=2048
        )
        logger.info("LLM: GPT-4o (OpenAI)")

    else:
        raise RuntimeError(
            f"Unknown LLM_PROVIDER: {LLM_PROVIDER}. "
            f"Use 'anthropic' or 'openai'"
        )

    logger.info("RAG API initialized successfully")


# -------------------------------------------------------
# REQUEST/RESPONSE MODELS
# -------------------------------------------------------
class QueryRequest(BaseModel):
    """RAG query request."""
    question: str = Field(
        ...,
        min_length=3,
        max_length=2000,
        description="The question to answer"
    )
    collection: Optional[str] = Field(
        default=None,
        description="Qdrant collection to query (defaults to env var)"
    )
    top_k: Optional[int] = Field(
        default=None,
        ge=1,
        le=20,
        description="Number of chunks to retrieve"
    )


class SourceChunk(BaseModel):
    """A retrieved document chunk."""
    content: str
    source: str
    score: float
    metadata: Dict[str, Any] = {}


class QueryResponse(BaseModel):
    """RAG query response."""
    request_id: str
    question: str
    answer: str
    sources: List[SourceChunk]
    chunks_retrieved: int
    retrieval_time_ms: float
    generation_time_ms: float
    total_time_ms: float
    llm_provider: str
    model: str
    usage: Dict[str, int]


# -------------------------------------------------------
# ENDPOINTS
# -------------------------------------------------------
@app.get("/health")
async def health():
    """Health check — verifies Qdrant and LLM connectivity."""
    qdrant_ok = False
    llm_ok = llm is not None

    try:
        collections = qdrant_client.get_collections()
        qdrant_ok = True
    except Exception as e:
        logger.warning(f"Qdrant health check failed: {e}")

    return {
        "status": "healthy" if qdrant_ok and llm_ok else "degraded",
        "qdrant": "connected" if qdrant_ok else "disconnected",
        "llm": LLM_PROVIDER if llm_ok else "not_initialized",
        "collection": QDRANT_COLLECTION,
        "embedding_model": EMBEDDING_MODEL
    }


async def enforce_request_security(request: Request) -> None:
    """Validate the caller and enforce its rate limit before running the LLM."""
    headers = {}
    for name in ("X-API-Key", "Authorization", "X-Forwarded-For"):
        value = request.headers.get(name)
        if value:
            headers[name] = value

    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            auth_response = await client.post(AUTH_SERVICE_URL, headers=headers)
            if auth_response.status_code != 200:
                raise HTTPException(status_code=auth_response.status_code, detail="Request authentication failed")

            rate_response = await client.post(RATE_LIMITER_URL, headers=headers)
            if rate_response.status_code != 200:
                retry_after = rate_response.headers.get("Retry-After")
                response_headers = {"Retry-After": retry_after} if retry_after else None
                raise HTTPException(
                    status_code=rate_response.status_code,
                    detail="Rate limit exceeded",
                    headers=response_headers
                )
    except HTTPException:
        raise
    except httpx.HTTPError as exc:
        logger.error("Security service unavailable: %s", exc)
        raise HTTPException(status_code=503, detail="Request security service unavailable") from exc
@app.post("/query", response_model=QueryResponse)
async def query(request: QueryRequest, _: None = Depends(enforce_request_security)):
    """
    Query the RAG pipeline.

    1. Embed the question using the local embedding model
    2. Retrieve top-k relevant chunks from Qdrant
    3. Build context from retrieved chunks
    4. Send question + context to LLM
    5. Return answer with sources and timing
    """
    if not qdrant_store or not llm:
        raise HTTPException(
            status_code=503,
            detail="RAG pipeline not initialized"
        )

    request_id = str(uuid.uuid4())
    collection = request.collection or QDRANT_COLLECTION
    top_k = request.top_k or TOP_K_CHUNKS

    logger.info(
        f"Query: request_id={request_id} "
        f"question={request.question[:50]}..."
    )

    # -------------------------------------------------------
    # STEP 1 — RETRIEVAL
    # -------------------------------------------------------
    retrieval_start = time.time()

    try:
        results = qdrant_store.similarity_search_with_score(
            query=request.question,
            k=top_k
        )
    except Exception as e:
        logger.error(f"Qdrant retrieval failed: {e}")
        raise HTTPException(
            status_code=502,
            detail=f"Vector retrieval failed: {str(e)}"
        )

    retrieval_time_ms = (time.time() - retrieval_start) * 1000

    # Filter by similarity threshold
    filtered_results = [
        (doc, score) for doc, score in results
        if score >= SIMILARITY_THRESHOLD
    ]

    if not filtered_results:
        logger.warning(
            f"No relevant chunks found for: {request.question[:50]}"
        )

    # Build source chunks for response
    source_chunks = [
        SourceChunk(
            content=doc.page_content,
            source=doc.metadata.get("source", "unknown"),
            score=float(score),
            metadata=doc.metadata
        )
        for doc, score in filtered_results
    ]

    # Build context string
    context = "\n\n---\n\n".join([
        f"Source: {chunk.source}\n{chunk.content}"
        for chunk in source_chunks
    ])

    if not context:
        context = (
            "No relevant context found in the knowledge base. "
            "Please answer based on general knowledge or indicate "
            "that the information is not available."
        )

    # -------------------------------------------------------
    # STEP 2 — GENERATION
    # -------------------------------------------------------
    generation_start = time.time()

    try:
        chain = RAG_PROMPT | llm | StrOutputParser()
        answer = await chain.ainvoke({
            "context": context,
            "question": request.question
        })
    except Exception as e:
        logger.error(f"LLM generation failed: {e}")
        raise HTTPException(
            status_code=502,
            detail=f"LLM generation failed: {str(e)}"
        )

    generation_time_ms = (time.time() - generation_start) * 1000
    total_time_ms = retrieval_time_ms + generation_time_ms

    # Estimate token usage (LangChain doesn't always return exact counts)
    prompt_tokens = len(context.split()) + len(request.question.split())
    completion_tokens = len(answer.split())

    logger.info(
        f"Query complete: request_id={request_id} "
        f"chunks={len(filtered_results)} "
        f"retrieval={retrieval_time_ms:.0f}ms "
        f"generation={generation_time_ms:.0f}ms "
        f"total={total_time_ms:.0f}ms"
    )

    return QueryResponse(
        request_id=request_id,
        question=request.question,
        answer=answer,
        sources=source_chunks,
        chunks_retrieved=len(filtered_results),
        retrieval_time_ms=round(retrieval_time_ms, 2),
        generation_time_ms=round(generation_time_ms, 2),
        total_time_ms=round(total_time_ms, 2),
        llm_provider=LLM_PROVIDER,
        model="claude-sonnet-4-6" if LLM_PROVIDER == "anthropic" else "gpt-4o",
        usage={
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens
        }
    )


@app.get("/collections")
async def list_collections():
    """List all available Qdrant collections."""
    try:
        collections = qdrant_client.get_collections()
        return {
            "collections": [
                c.name for c in collections.collections
            ]
        }
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
