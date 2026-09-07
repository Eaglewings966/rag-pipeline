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
TFSTATE_BUCKET="${TFSTATE_BUCKET:-}"
PROJECT_NAME="${PROJECT_NAME:-rag-pipeline}"

if [ -z "${TFSTATE_BUCKET}" ]; then
  echo "ERROR: TFSTATE_BUCKET is not set"
  echo "Run: export TFSTATE_BUCKET=your-unique-terraform-state-bucket"
  exit 1
fi

# -------------------------------------------------------
# STEP 1 — Terraform
# -------------------------------------------------------
echo ""
echo "Step 1: Provisioning infrastructure with Terraform..."
cd terraform

# Terraform never receives the LLM key: otherwise it would be written to state.
read -r -s -p "Paste your Anthropic API key: " ANTHROPIC_API_KEY
echo
if [ -z "${ANTHROPIC_API_KEY}" ]; then
  echo "ERROR: an Anthropic API key is required"
  exit 1
fi

terraform init \
  -backend-config="bucket=${TFSTATE_BUCKET}" \
  -backend-config="region=${AWS_REGION}" \
  -backend-config="dynamodb_table=terraform-state-lock"
terraform validate
terraform plan -out=tfplan
read -r -p "Review the plan above. Create these AWS resources? [y/N] " CONFIRM
if [[ ! "${CONFIRM}" =~ ^[Yy]$ ]]; then
  echo "Deployment cancelled before Terraform apply."
  exit 0
fi
terraform apply tfplan

# Save outputs
CLUSTER_NAME=$(terraform output -raw cluster_name)
ESO_ROLE_ARN=$(terraform output -raw eso_role_arn)
RAG_API_ROLE_ARN=$(terraform output -raw rag_api_role_arn)
DOCUMENTS_BUCKET=$(terraform output -raw documents_bucket)
ECR_RAG_API=$(terraform output -raw ecr_rag_api_url)

cd ..

echo "Infrastructure provisioned"
# Store the LLM key directly in Secrets Manager after Terraform creates the secret.
# This keeps it out of Terraform state and plan files.
aws secretsmanager put-secret-value \
  --secret-id "${PROJECT_NAME}/claude-api-key" \
  --secret-string "$(jq -nc --arg api_key "${ANTHROPIC_API_KEY}" '{api_key: $api_key}')" \
  --region "${AWS_REGION}" >/dev/null
unset ANTHROPIC_API_KEY
echo "Claude API key stored in AWS Secrets Manager outside Terraform state."

# Resolve values embedded in the Kubernetes manifests.
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
ECR_REGISTRY="${ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"
find k8s -name '*.yaml' -type f -print0 | xargs -0 sed -i \
  -e "s#YOUR_ACCOUNT_ID.dkr.ecr.us-east-1.amazonaws.com#${ECR_REGISTRY}#g" \
  -e "s#YOUR_ACCOUNT.dkr.ecr.us-east-1.amazonaws.com#${ECR_REGISTRY}#g" \
  -e "s#YOUR_RAG_API_ROLE_ARN#${RAG_API_ROLE_ARN}#g"

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
# STEP 3 — Install AWS Load Balancer Controller (ALB-only demo endpoint)
# -------------------------------------------------------
echo ""
echo "Step 3: Installing AWS Load Balancer Controller..."
LBC_POLICY_NAME="AWSLoadBalancerControllerIAMPolicy"
LBC_POLICY_ARN="arn:aws:iam::${ACCOUNT_ID}:policy/${LBC_POLICY_NAME}"
if ! aws iam get-policy --policy-arn "${LBC_POLICY_ARN}" >/dev/null 2>&1; then
  curl -fsSLo iam_policy.json \
    https://raw.githubusercontent.com/kubernetes-sigs/aws-load-balancer-controller/v2.14.1/docs/install/iam_policy.json
  aws iam create-policy --policy-name "${LBC_POLICY_NAME}" \
    --policy-document file://iam_policy.json >/dev/null
fi

# The Terraform-created EKS OIDC provider lets eksctl bind this role to the controller service account.
eksctl create iamserviceaccount \
  --cluster "${CLUSTER_NAME}" \
  --region "${AWS_REGION}" \
  --namespace kube-system \
  --name aws-load-balancer-controller \
  --role-name "${PROJECT_NAME}-aws-load-balancer-controller" \
  --attach-policy-arn "${LBC_POLICY_ARN}" \
  --override-existing-serviceaccounts \
  --approve

VPC_ID=$(aws eks describe-cluster --name "${CLUSTER_NAME}" --region "${AWS_REGION}" \
  --query 'cluster.resourcesVpcConfig.vpcId' --output text)
helm repo add eks https://aws.github.io/eks-charts
helm repo update
helm upgrade --install aws-load-balancer-controller eks/aws-load-balancer-controller \
  --namespace kube-system \
  --set clusterName="${CLUSTER_NAME}" \
  --set serviceAccount.create=false \
  --set serviceAccount.name=aws-load-balancer-controller \
  --set region="${AWS_REGION}" \
  --set vpcId="${VPC_ID}" \
  --wait
kubectl rollout status deployment/aws-load-balancer-controller -n kube-system --timeout=5m
echo "AWS Load Balancer Controller installed"

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

kubectl create namespace monitoring --dry-run=client -o yaml | kubectl apply -f -
GRAFANA_ADMIN_PASSWORD="${GRAFANA_ADMIN_PASSWORD:-$(openssl rand -base64 32 | tr -d '\n')}"
kubectl -n monitoring create secret generic grafana-admin-credentials \
  --from-literal=admin-user=admin \
  --from-literal=admin-password="${GRAFANA_ADMIN_PASSWORD}" \
  --dry-run=client -o yaml | kubectl apply -f -
helm upgrade --install kube-prometheus-stack \
  prometheus-community/kube-prometheus-stack \
  --namespace monitoring \
  --set grafana.admin.existingSecret=grafana-admin-credentials \
  --set grafana.admin.userKey=admin-user \
  --set grafana.admin.passwordKey=admin-password \
  --set prometheus.prometheusSpec.retention=15d \
  --wait
printf 'Grafana credentials: admin / %s\n' "${GRAFANA_ADMIN_PASSWORD}"
unset GRAFANA_ADMIN_PASSWORD

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
# STEP 8 — Build and push Docker images
# -------------------------------------------------------
echo ""
echo "Step 8: Building and pushing Docker images..."

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
# STEP 9 — Apply Kubernetes manifests
# -------------------------------------------------------
echo ""
echo "Step 9: Applying Kubernetes manifests..."
kubectl apply -f k8s/namespaces/
kubectl apply -f k8s/external-secrets/
kubectl apply -f k8s/deployments/
kubectl apply -f k8s/ingress/
kubectl apply -f k8s/autoscaling/
kubectl apply -f k8s/network-policies/
kubectl apply -f k8s/monitoring/
kubectl apply -f k8s/argocd/

echo "Manifests applied"

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
kubectl get ingress -n rag-api

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
echo "  http://localhost:3000 | admin / password printed during bootstrap"
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
