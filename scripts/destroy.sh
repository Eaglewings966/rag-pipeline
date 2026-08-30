#!/bin/bash
# Destroy all RAG Pipeline resources
# Run after screenshots and video recording

set -euo pipefail

echo "================================================"
echo "Destroying RAG Pipeline — ALL Resources"
echo "================================================"
echo ""
echo "This will destroy:"
echo "  - EKS cluster: rag-pipeline-cluster"
echo "  - VPC and all networking"
echo "  - S3 documents bucket"
echo "  - ECR repositories"
echo "  - Secrets Manager secrets"
echo "  - SNS topics and subscriptions"
echo "  - IAM roles and OIDC providers"
echo ""
read -p "Type 'destroy' to confirm: " confirm

if [ "${confirm}" != "destroy" ]; then
  echo "Destroy cancelled"
  exit 0
fi

AWS_REGION="${AWS_REGION:-us-east-1}"

# Step 1 — Remove all Kubernetes resources
echo ""
echo "Step 1: Removing Kubernetes resources..."
kubectl delete -f k8s/ --recursive --ignore-not-found=true 2>/dev/null || true

# Step 2 — Uninstall Helm releases
echo ""
echo "Step 2: Uninstalling Helm releases..."
helm uninstall kube-prometheus-stack -n monitoring 2>/dev/null || true
helm uninstall external-secrets -n external-secrets 2>/dev/null || true
helm uninstall keda -n kube-system 2>/dev/null || true

# Step 3 — Remove Linkerd
echo ""
echo "Step 3: Removing Linkerd..."
linkerd uninstall | kubectl delete -f - 2>/dev/null || true

# Step 4 — Remove Argo CD
echo ""
echo "Step 4: Removing Argo CD..."
kubectl delete -n argocd \
  -f https://raw.githubusercontent.com/argoproj/argo-cd/stable/manifests/install.yaml \
  2>/dev/null || true

# Step 5 — Empty S3 buckets before Terraform destroy
echo ""
echo "Step 5: Emptying S3 buckets..."
DOCUMENTS_BUCKET=$(cd terraform && \
  terraform output -raw documents_bucket 2>/dev/null || echo "")

if [ -n "${DOCUMENTS_BUCKET}" ]; then
  aws s3 rm s3://${DOCUMENTS_BUCKET} --recursive \
    --region ${AWS_REGION} 2>/dev/null || true
  echo "Documents bucket emptied: ${DOCUMENTS_BUCKET}"
fi

# Step 6 — Terraform destroy
echo ""
echo "Step 6: Running terraform destroy..."
cd terraform
terraform destroy --auto-approve
cd ..

# Step 7 — Verify
echo ""
echo "================================================"
echo "Verifying cleanup..."
echo ""

echo "Remaining EKS clusters:"
aws eks list-clusters --region ${AWS_REGION} \
  --query 'clusters' --output text 2>/dev/null || echo "None"

echo ""
echo "Remaining EC2 instances tagged rag-pipeline:"
aws ec2 describe-instances \
  --filters "Name=tag:Project,Values=rag-pipeline" \
           "Name=instance-state-name,Values=running" \
  --query 'Reservations[*].Instances[*].InstanceId' \
  --output text \
  --region ${AWS_REGION} 2>/dev/null || echo "None"

echo ""
echo "================================================"
echo "Destroy complete"
echo ""
echo "Verify manually in AWS console:"
echo "  - EKS clusters"
echo "  - NAT Gateways (can incur charges)"
echo "  - Load Balancers"
echo "  - Elastic IPs"
echo "================================================"
