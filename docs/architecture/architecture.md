# Production RAG Pipeline Architecture

The browser-facing path is Route 53, ACM-enabled ALB, Kubernetes Ingress, and the FastAPI RAG service. The service retrieves document chunks from Qdrant and uses the configured Claude or OpenAI provider for answer generation.

Documents flow from S3 through the ingestion job into Qdrant. GitHub Actions builds application images, ECR stores them, and Argo CD synchronizes Kubernetes manifests to EKS. AWS Secrets Manager supplies runtime secrets through External Secrets Operator.

The production architecture diagram is available at [rag-pipeline-architecture.png](../images/rag-pipeline-architecture.png).
