"""
Document Ingestion Pipeline.
Downloads documents from S3, chunks them, generates embeddings,
and stores in Qdrant.

Run as a Kubernetes Job or CronJob:
  kubectl create job ingest --from=cronjob/document-ingestion -n rag-api

Or trigger manually:
  python3 ingest.py --bucket YOUR_BUCKET --prefix documents/
"""

import os
import sys
import logging
import argparse
import hashlib
from pathlib import Path
from typing import List, Optional
import boto3
from botocore.exceptions import ClientError
from langchain_community.document_loaders import (
    PyPDFLoader,
    TextLoader,
    UnstructuredMarkdownLoader
)
from langchain.text_splitter import RecursiveCharacterTextSplitter
from langchain_huggingface import HuggingFaceEmbeddings
from langchain_qdrant import QdrantVectorStore
from qdrant_client import QdrantClient
from qdrant_client.http.models import Distance, VectorParams

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger(__name__)

QDRANT_URL = os.environ.get("QDRANT_URL", "http://qdrant:6333")
QDRANT_COLLECTION = os.environ.get("QDRANT_COLLECTION", "documents")
EMBEDDING_MODEL = os.environ.get(
    "EMBEDDING_MODEL",
    "sentence-transformers/all-MiniLM-L6-v2"
)
CHUNK_SIZE = int(os.environ.get("CHUNK_SIZE", "1000"))
CHUNK_OVERLAP = int(os.environ.get("CHUNK_OVERLAP", "200"))
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")


def download_from_s3(
    bucket: str,
    prefix: str,
    local_dir: str = "/tmp/documents"
) -> List[str]:
    """
    Download documents from S3 to local directory.
    Skips files already downloaded (based on ETag).

    Args:
        bucket: S3 bucket name
        prefix: S3 key prefix (folder)
        local_dir: Local directory to store downloads

    Returns:
        List of local file paths
    """
    s3 = boto3.client("s3", region_name=AWS_REGION)
    local_path = Path(local_dir)
    local_path.mkdir(parents=True, exist_ok=True)

    downloaded = []
    supported = {".pdf", ".txt", ".md", ".rst"}

    try:
        paginator = s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                suffix = Path(key).suffix.lower()

                if suffix not in supported:
                    logger.debug(f"Skipping unsupported file: {key}")
                    continue

                # Use ETag as cache key to avoid re-downloading
                etag = obj["ETag"].strip('"')
                local_file = local_path / f"{etag}{suffix}"

                if not local_file.exists():
                    logger.info(f"Downloading: s3://{bucket}/{key}")
                    s3.download_file(bucket, key, str(local_file))
                    # Store original source for metadata
                    local_file.with_suffix(f"{suffix}.meta").write_text(
                        f"s3://{bucket}/{key}"
                    )
                else:
                    logger.debug(f"Already cached: {local_file}")

                downloaded.append(str(local_file))

    except ClientError as e:
        logger.error(f"S3 download failed: {e}")
        raise

    logger.info(f"Downloaded {len(downloaded)} documents from S3")
    return downloaded


def load_and_chunk_documents(
    file_paths: List[str]
) -> list:
    """
    Load documents and split into chunks.

    Args:
        file_paths: List of local file paths

    Returns:
        List of LangChain Document objects (chunks)
    """
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=CHUNK_SIZE,
        chunk_overlap=CHUNK_OVERLAP,
        separators=["\n\n", "\n", ". ", " ", ""]
    )

    all_chunks = []

    for file_path in file_paths:
        path = Path(file_path)
        suffix = path.suffix.lower()

        # Get original source from metadata file if available
        meta_file = path.with_suffix(f"{suffix}.meta")
        source = (
            meta_file.read_text().strip()
            if meta_file.exists()
            else file_path
        )

        try:
            if suffix == ".pdf":
                loader = PyPDFLoader(file_path)
            elif suffix in (".txt", ".rst"):
                loader = TextLoader(file_path)
            elif suffix == ".md":
                loader = UnstructuredMarkdownLoader(file_path)
            else:
                logger.warning(f"Unsupported format: {suffix}")
                continue

            docs = loader.load()

            # Add source metadata to all chunks
            for doc in docs:
                doc.metadata["source"] = source
                doc.metadata["file_type"] = suffix

            chunks = splitter.split_documents(docs)
            all_chunks.extend(chunks)

            logger.info(
                f"Loaded {len(docs)} pages → {len(chunks)} chunks: {source}"
            )

        except Exception as e:
            logger.error(f"Failed to load {file_path}: {e}")
            continue

    logger.info(f"Total chunks ready for embedding: {len(all_chunks)}")
    return all_chunks


def ingest_to_qdrant(
    chunks: list,
    batch_size: int = 100
) -> int:
    """
    Generate embeddings and store in Qdrant.
    Processes in batches to avoid memory issues.

    Args:
        chunks: List of Document objects
        batch_size: Number of chunks per embedding batch

    Returns:
        Total number of chunks ingested
    """
    logger.info(f"Loading embedding model: {EMBEDDING_MODEL}")
    embeddings = HuggingFaceEmbeddings(
        model_name=EMBEDDING_MODEL,
        model_kwargs={"device": "cpu"},
        encode_kwargs={"normalize_embeddings": True}
    )

    client = QdrantClient(url=QDRANT_URL)

    # Ensure collection exists
    try:
        client.get_collection(QDRANT_COLLECTION)
        logger.info(f"Collection exists: {QDRANT_COLLECTION}")
    except Exception:
        logger.info(f"Creating collection: {QDRANT_COLLECTION}")
        client.create_collection(
            collection_name=QDRANT_COLLECTION,
            vectors_config=VectorParams(
                size=384,
                distance=Distance.COSINE
            )
        )

    vector_store = QdrantVectorStore(
        client=client,
        collection_name=QDRANT_COLLECTION,
        embedding=embeddings
    )

    total_ingested = 0

    # Process in batches
    for i in range(0, len(chunks), batch_size):
        batch = chunks[i:i + batch_size]
        try:
            vector_store.add_documents(batch)
            total_ingested += len(batch)
            logger.info(
                f"Ingested batch {i // batch_size + 1}: "
                f"{total_ingested}/{len(chunks)} chunks"
            )
        except Exception as e:
            logger.error(f"Failed to ingest batch {i // batch_size + 1}: {e}")

    # Log final collection stats
    collection_info = client.get_collection(QDRANT_COLLECTION)
    logger.info(
        f"Collection '{QDRANT_COLLECTION}' now has "
        f"{collection_info.points_count} vectors"
    )

    return total_ingested


def main():
    parser = argparse.ArgumentParser(
        description="Ingest documents from S3 into Qdrant"
    )
    parser.add_argument("--bucket", required=True, help="S3 bucket name")
    parser.add_argument(
        "--prefix", default="documents/", help="S3 key prefix"
    )
    parser.add_argument(
        "--local-dir", default="/tmp/documents",
        help="Local download directory"
    )
    args = parser.parse_args()

    logger.info("=" * 60)
    logger.info("Document Ingestion Pipeline — Tech with Emma RAG")
    logger.info("=" * 60)
    logger.info(f"Source:     s3://{args.bucket}/{args.prefix}")
    logger.info(f"Qdrant:     {QDRANT_URL}")
    logger.info(f"Collection: {QDRANT_COLLECTION}")
    logger.info(f"Chunk size: {CHUNK_SIZE} chars (overlap: {CHUNK_OVERLAP})")
    logger.info("=" * 60)

    # Download from S3
    file_paths = download_from_s3(args.bucket, args.prefix, args.local_dir)
    if not file_paths:
        logger.warning("No supported documents found in S3. Exiting.")
        sys.exit(0)

    # Load and chunk
    chunks = load_and_chunk_documents(file_paths)
    if not chunks:
        logger.warning("No chunks produced. Check document formats.")
        sys.exit(0)

    # Ingest to Qdrant
    total = ingest_to_qdrant(chunks)

    logger.info("=" * 60)
    logger.info(f"Ingestion complete: {total} chunks in Qdrant")
    logger.info("=" * 60)


if __name__ == "__main__":
    main()
