#!/bin/bash
# Bootstrap the full RAG Pipeline deployment
# Run from: SSH into EC2 via MobaXterm OR VS Code terminal

set -euo pipefail

echo "================================================"
echo "RAG Pipeline — Bootstrap Script"
echo "Project 15 — Tech with Emma"
echo "================================================"

AWS_REGION="${AWS_REGION:-us-east-1}"
CLUSTER_NAME="${CLUSTER_NAME:-rag-pipeline-cluster}"

# -------------------------------------------------------
# STEP 1 — Terraform
# -------------------------------------------------------
echo ""
echo "Step 1: Provisioning infrastructure with Terraform..."
cd terraform

# Validate Claude API key is set
if [ -z "${TF_VAR_claude_api_key:-}" ]; then
  echo "ERROR: TF_VAR_claude_api_key is not set"
  echo "Run: export TF_VAR_claude_api_key=sk-ant-YOUR_KEY"
  exit 1
fi

terraform init
terraform validate
terraform apply --auto-approve

# Save outputs
CLUSTER_NAME=$(terraform output -raw cluster_name)
ESO_ROLE_ARN=$(terraform output -raw eso_role_arn)
DOCUMENTS_BUCKET=$(terraform output -raw documents_bucket)
ECR_RAG_API=$(terraform output -raw ecr_rag_api_url)

cd ..

echo "Infrastructure provisioned"

# -------------------------------------------------------
# STEP 2 — Configure kubectl
# -------------------------------------------------------
echo ""
echo "Step 2: Configuring kubectl..."
aws eks update-kubeconfig \
  --region ${AWS_REGION} \
  --name ${CLUSTER_NAME}

kubectl get nodes
echo "kubectl configured"

# -------------------------------------------------------
# STEP 3 — Install Linkerd
# -------------------------------------------------------
echo ""
echo "Step 3: Installing Linkerd service mesh..."
curl --proto '=https' --tlsv1.2 -sSfL https://run.linkerd.io/install | sh
export PATH=$HOME/.linkerd2/bin:$PATH

linkerd check --pre
linkerd install --crds | kubectl apply -f -
linkerd install | kubectl apply -f -
linkerd check

echo "Linkerd installed"

# -------------------------------------------------------
# STEP 4 — Install KEDA
# -------------------------------------------------------
echo ""
echo "Step 4: Installing KEDA..."
helm repo add kedacore https://kedacore.github.io/charts
helm repo update

helm install keda kedacore/keda \
  --namespace kube-system \
  --wait

echo "KEDA installed"

# -------------------------------------------------------
# STEP 5 — Install External Secrets Operator
# -------------------------------------------------------
echo ""
echo "Step 5: Installing External Secrets Operator..."
helm repo add external-secrets https://charts.external-secrets.io
helm repo update

helm install external-secrets external-secrets/external-secrets \
  --namespace external-secrets \
  --create-namespace \
  --set serviceAccount.annotations."eks\.amazonaws\.com/role-arn"="${ESO_ROLE_ARN}" \
  --wait

echo "ESO installed with IRSA role: ${ESO_ROLE_ARN}"

# -------------------------------------------------------
# STEP 6 — Install kube-prometheus-stack
# -------------------------------------------------------
echo ""
echo "Step 6: Installing monitoring stack..."
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm repo update

helm install kube-prometheus-stack \
  prometheus-community/kube-prometheus-stack \
  --namespace monitoring \
  --create-namespace \
  --set grafana.adminPassword="TechWithEmma2024!" \
  --set prometheus.prometheusSpec.retention=15d \
  --wait

echo "Monitoring stack installed"

# -------------------------------------------------------
# STEP 7 — Install Argo CD
# -------------------------------------------------------
echo ""
echo "Step 7: Installing Argo CD..."
kubectl create namespace argocd --dry-run=client -o yaml | kubectl apply -f -

kubectl apply -n argocd \
  -f https://raw.githubusercontent.com/argoproj/argo-cd/stable/manifests/install.yaml

kubectl wait --for=condition=available \
  deployment/argocd-server \
  -n argocd \
  --timeout=300s

echo "Argo CD installed"

# -------------------------------------------------------
# STEP 8 — Apply Kubernetes manifests
# -------------------------------------------------------
echo ""
echo "Step 8: Applying Kubernetes manifests..."
kubectl apply -f k8s/namespaces/
kubectl apply -f k8s/external-secrets/
kubectl apply -f k8s/deployments/
kubectl apply -f k8s/autoscaling/
kubectl apply -f k8s/network-policies/
kubectl apply -f k8s/monitoring/
kubectl apply -f k8s/argocd/

echo "Manifests applied"

# -------------------------------------------------------
# STEP 9 — Build and push Docker images
# -------------------------------------------------------
echo ""
echo "Step 9: Building and pushing Docker images..."

ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)

aws ecr get-login-password --region ${AWS_REGION} | \
  docker login --username AWS \
  --password-stdin ${ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com

for service in rag-api auth-service rate-limiter metrics-exporter ingestion; do
  echo "Building ${service}..."
  docker build -t ${ECR_RAG_API%/rag-api}/${service}:latest apps/${service}/
  docker push ${ECR_RAG_API%/rag-api}/${service}:latest
  echo "${service} pushed"
done

# -------------------------------------------------------
# STEP 10 — Verify
# -------------------------------------------------------
echo ""
echo "Step 10: Verifying deployment..."
kubectl get nodes
kubectl get pods -n rag-api
kubectl get pods -n qdrant
kubectl get pods -n monitoring
kubectl get pods -n argocd

# -------------------------------------------------------
# SUMMARY
# -------------------------------------------------------
echo ""
echo "================================================"
echo "RAG Pipeline Bootstrap Complete"
echo "================================================"
echo ""
echo "Documents S3 bucket: ${DOCUMENTS_BUCKET}"
echo ""
echo "Access Grafana:"
echo "  kubectl port-forward svc/kube-prometheus-stack-grafana -n monitoring 3000:80"
echo "  http://localhost:3000 | admin / TechWithEmma2024!"
echo ""
echo "Access Argo CD:"
echo "  kubectl port-forward svc/argocd-server -n argocd 8080:443"
echo "  https://localhost:8080 | admin / [kubectl get secret argocd-initial-admin-secret]"
echo ""
echo "Test RAG API:"
echo "  curl -X POST http://RAG_API_IP/query \\"
echo "    -H 'X-API-Key: YOUR_KEY' \\"
echo "    -H 'Content-Type: application/json' \\"
echo "    -d '{\"question\": \"What is the main topic of the documents?\"}'"
echo ""
echo "⚠️  Run bash scripts/destroy.sh when done"
echo "================================================"
