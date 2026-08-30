#!/bin/bash
# Verify the RAG Pipeline deployment is healthy

set -euo pipefail

echo "================================================"
echo "RAG Pipeline — Verification"
echo "================================================"

# Node health
echo ""
echo "Node status:"
kubectl get nodes --show-labels | grep -E "NAME|system|application"

# Pod health
echo ""
echo "Pod status — rag-api namespace:"
kubectl get pods -n rag-api -o wide

echo ""
echo "Pod status — qdrant namespace:"
kubectl get pods -n qdrant -o wide

# KEDA ScaledObjects
echo ""
echo "KEDA ScaledObjects:"
kubectl get scaledobjects -n rag-api

# External Secrets
echo ""
echo "External Secrets sync status:"
kubectl get externalsecret -n rag-api

# Argo CD applications
echo ""
echo "Argo CD applications:"
kubectl get applications -n argocd

# Test auth service
echo ""
echo "Testing auth service health..."
kubectl exec -n rag-api deployment/auth-service -- \
  curl -sf http://localhost:8001/health | python3 -m json.tool

# Test rate limiter
echo ""
echo "Testing rate limiter health..."
kubectl exec -n rag-api deployment/rate-limiter -- \
  curl -sf http://localhost:8002/health | python3 -m json.tool

# Test RAG API
echo ""
echo "Testing RAG API health..."
kubectl exec -n rag-api deployment/rag-api -- \
  curl -sf http://localhost:8000/health | python3 -m json.tool

# Test metrics endpoint
echo ""
echo "Testing metrics exporter..."
kubectl exec -n rag-api deployment/rag-api -c metrics-exporter -- \
  curl -sf http://localhost:8003/metrics | head -30

# Qdrant health
echo ""
echo "Testing Qdrant..."
kubectl exec -n qdrant statefulset/qdrant -- \
  curl -sf http://localhost:6333/healthz

echo ""
echo "================================================"
echo "All verification checks passed"
echo "================================================"
