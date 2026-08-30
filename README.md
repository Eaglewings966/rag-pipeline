<div align="center">

# Production RAG Pipeline

[![CI](https://github.com/Eaglewings966/rag-pipeline/actions/workflows/ci.yml/badge.svg)](https://github.com/Eaglewings966/rag-pipeline/actions/workflows/ci.yml)
[![Kubernetes](https://img.shields.io/badge/Kubernetes-1.29-326CE5?style=flat-square&logo=kubernetes&logoColor=white)](https://kubernetes.io/)
[![Terraform](https://img.shields.io/badge/Terraform-1.5+-7B42BC?style=flat-square&logo=terraform&logoColor=white)](https://www.terraform.io/)
[![Python](https://img.shields.io/badge/Python-3.11-3776AB?style=flat-square&logo=python&logoColor=white)](https://www.python.org/)
[![License](https://img.shields.io/badge/License-MIT-22c55e?style=flat-square)](LICENSE)

**A production-grade Retrieval-Augmented Generation pipeline on AWS EKS.
Documents in S3. Embeddings in Qdrant. Auth, rate limiting, and mTLS
throughout. GitOps via Argo CD. Metrics that show you what the LLM
actually costs.**

[📖 Deep-dive article](https://emmanuelubani.hashnode.dev) •
[🎥 Build walkthrough](https://youtube.com/@techwithemma) •
[💼 LinkedIn](https://linkedin.com/in/ubaniemmanuel) •
[🌐 Portfolio](https://ops-run.lovable.app)

<br/>

![Architecture](docs/architecture/architecture.png)

</div>

---

## Table of Contents

- [Why This Exists](#why-this-exists)
- [Who Uses It](#who-uses-it)
- [Tech Stack](#tech-stack)
- [Request Flow](#request-flow)
- [Architecture](#architecture)
- [Security — Defense in Depth](#security--defense-in-depth)
- [Autoscaling](#autoscaling)
- [Observability](#observability)
- [What's Implemented](#whats-implemented)
- [Directory Structure](#directory-structure)
- [Deployment](#deployment)

---

## Why This Exists

Every enterprise team building with LLMs eventually needs a document
Q&A system. The naive path — API key in an env var, no auth, no rate
limiting, no observability — works for demos and fails in production.
This pipeline shows what the production version actually requires.

---

## Who Uses It

- **API consumers** — query a knowledge base using POST /query with
  an API key; receive answers grounded in real documents with sources
- **Platform engineers** — deploy and operate via GitOps; every change
  goes through Argo CD sync waves; infrastructure is Terraform-managed
- **Security teams** — seven defense-in-depth layers; no static AWS
  credentials anywhere; mTLS between all pods; API keys from Secrets
  Manager via IRSA

---

## Tech Stack

**Cloud and Orchestration**

![AWS](https://img.shields.io/badge/AWS-EKS_EC2-FF9900?style=flat-square&logo=amazonaws&logoColor=white)
![Kubernetes](https://img.shields.io/badge/Kubernetes-1.29-326CE5?style=flat-square&logo=kubernetes&logoColor=white)
![Argo CD](https://img.shields.io/badge/Argo_CD-GitOps-EF7B4D?style=flat-square)
![Terraform](https://img.shields.io/badge/Terraform-1.5+-7B42BC?style=flat-square&logo=terraform&logoColor=white)

**Language and Framework**

![Python](https://img.shields.io/badge/Python-3.11-3776AB?style=flat-square&logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-0.109-009688?style=flat-square&logo=fastapi&logoColor=white)
![LangChain](https://img.shields.io/badge/LangChain-0.1-1C3C3C?style=flat-square)

**AI and Vector Storage**

![Claude](https://img.shields.io/badge/Claude-claude--sonnet--4--6-7B42BC?style=flat-square)
![Qdrant](https://img.shields.io/badge/Qdrant-1.9-FF4081?style=flat-square)
![HuggingFace](https://img.shields.io/badge/HuggingFace-Embeddings-FFD21E?style=flat-square)

**Security and Service Mesh**

![Linkerd](https://img.shields.io/badge/Linkerd-mTLS-2BEDA7?style=flat-square)
![ESO](https://img.shields.io/badge/External_Secrets-IRSA-FF9900?style=flat-square)

**Observability and Autoscaling**

![Prometheus](https://img.shields.io/badge/Prometheus-Metrics-E6522C?style=flat-square&logo=prometheus&logoColor=white)
![Grafana](https://img.shields.io/badge/Grafana-Dashboards-F46800?style=flat-square&logo=grafana&logoColor=white)
![KEDA](https://img.shields.io/badge/KEDA-Autoscaling-326CE5?style=flat-square)
![Redis](https://img.shields.io/badge/Redis-Rate_Limit-DC382D?style=flat-square&logo=redis&logoColor=white)

---

## Request Flow

1. Client sends `POST /query` with `X-API-Key` header to ALB (HTTPS)
2. ALB terminates TLS using ACM certificate; forwards to Ingress
3. Ingress routes to **auth-service** — validates key against ESO-synced Secret
4. auth-service enforces IP allowlist if configured
5. Request forwarded to **rate-limiter** — checks Redis sliding window (10 req/min)
6. rate-limiter returns 429 + Retry-After if limit exceeded
7. Allowed request reaches **rag-api** — embeds question using local sentence-transformers
8. rag-api queries **Qdrant** — retrieves top-5 chunks by cosine similarity
9. rag-api sends question + chunks as context to **Claude** via LangChain
10. Claude generates grounded answer; rag-api returns answer + sources + usage
11. **metrics-exporter** sidecar records token counts, latency, cost to Prometheus
12. Prometheus scrapes every 15 seconds; Grafana renders in three dashboards

---

## Architecture

~~~text
Internet / API Consumer
        │ HTTPS :443
        ▼
Route 53 ──► ACM certificate ──► Application Load Balancer
                                            │
                                            ▼
                                EKS private cluster / Ingress
                                            │
             ┌──────────────────────────────┼──────────────────────────────┐
             ▼                              ▼                              ▼
      auth-service                    rate-limiter                      rag-api
      API-key + IP check              Redis, 10 req/min           embed → retrieve → generate
                                                                        │          │
                                                                        ▼          ▼
                                                                  Qdrant       Claude API
                                                                  EBS gp3      via LangChain

S3 documents ──► ingestion job ──► chunks + embeddings ──► Qdrant

Developer ──► GitHub Actions (OIDC) ──► ECR ──► Argo CD ──► EKS
Secrets Manager ──► External Secrets Operator / IRSA ──► application pods
Prometheus ──► metrics scrape ──► Grafana dashboards + Alertmanager/SNS
~~~

### Infrastructure

| Component | Detail |
|-----------|--------|
| Region | us-east-1 |
| IaC | Terraform 1.5+ with S3 + DynamoDB state |
| Cluster | EKS 1.29, private endpoint, VPC CNI |
| System nodes | t3.medium, on-demand, min 2 |
| Application nodes | t3.large, Spot, min 1 (warm node — no cold start) |
| Networking | VPC 10.0.0.0/16, public + private subnets, NAT Gateway |
| TLS | ACM certificate on ALB |

### Application Services

| Service | Framework | Responsibility |
|---------|-----------|---------------|
| auth-service | FastAPI | API key validation via ESO-synced Secret; IP allowlist |
| rate-limiter | FastAPI + Redis | Sliding window 10 req/min per key; 429 + Retry-After |
| rag-api | FastAPI + LangChain | Embedding, Qdrant retrieval, Claude generation |
| metrics-exporter | FastAPI + prometheus-client | Token counts, latency histograms, cost estimate |
| ingestion | Python + LangChain | S3 download, chunking, embedding, Qdrant store |
| qdrant | StatefulSet | Vector storage; EBS gp3 20Gi PVC; REST + gRPC |

---
## Security — Defense in Depth

Each layer assumes the layer above may be compromised.

1. **Network perimeter** — Security groups restrict ALB to 80/443; EKS nodes unreachable from internet
2. **TLS** — ACM certificate terminates at ALB; HTTP-only inside VPC over private subnets
3. **Authentication** — API keys stored in Secrets Manager; synced to pods via ESO + IRSA; no static AWS credentials anywhere in-cluster
4. **IP allowlist** — Valid keys from unexpected source IPs are rejected at auth-service
5. **Rate limiting** — Sliding window per key prevents abuse even with a valid key
6. **Service mesh mTLS** — Linkerd auto-injects mTLS into rag-api and qdrant namespaces; all pod-to-pod traffic encrypted
7. **Network policies** — Default-deny in rag-api and qdrant namespaces; explicit allow rules only for documented traffic paths

---

## Autoscaling

- **Pod scaling** — KEDA ScaledObject on `sum(rag_queue_depth)` Prometheus metric; second pod at queue depth > 3
- **Node scaling** — EKS managed node group scales 1→5 as KEDA adds pods that cannot be scheduled
- **Warm node** — Application node group minimum is 1, never 0; Spot instance is pre-warmed to avoid cold-start latency
- **Spot interruption** — EKS handles interruption notice; pod reschedules onto remaining or new node; warm minimum absorbs the gap

---

## Observability

**Stack:** kube-prometheus-stack (Prometheus, Grafana, Alertmanager,
node-exporter, kube-state-metrics) + custom metrics-exporter sidecar

**Dashboards:**

| Dashboard | What It Shows |
|-----------|--------------|
| Cluster Overview | Node CPU/memory, pod health by namespace, RAG API replica count |
| Inference Metrics | Request rate, p50/p95/p99 latency, error rate, queue depth, retrieval latency |
| LLM Metrics | Tokens/min, prompt vs completion split, estimated cost/hour, chunks retrieved, generation duration |

**metrics-exporter extracts from every response:**
- `rag_prompt_tokens_total` — prompt tokens sent to Claude
- `rag_completion_tokens_total` — completion tokens received
- `rag_total_tokens_total` — combined
- `rag_token_cost_usd_total` — estimated USD cost
- `rag_retrieval_duration_seconds` — Qdrant search latency
- `rag_chunks_retrieved` — chunks per query
- `rag_queue_depth` — active in-flight requests (drives KEDA)
- `rag_request_duration_seconds` — end-to-end latency histogram

---

## What's Implemented

- [x] Production RAG pipeline — embed, retrieve, generate with source attribution
- [x] Provider-agnostic LLM — swap Claude for OpenAI via single env var
- [x] Self-hosted embedding — sentence-transformers/all-MiniLM-L6-v2 (no external API call for embeddings)
- [x] Auth service — API key validation from Secrets Manager via ESO + IRSA
- [x] IP allowlist — per-key source IP enforcement
- [x] Rate limiting — sliding window, 10 req/min, Redis-backed, 429 + Retry-After
- [x] Linkerd mTLS — all pod-to-pod traffic encrypted at L4
- [x] Network policies — default-deny + explicit allow rules in rag-api and qdrant namespaces
- [x] KEDA autoscaling — queue depth metric drives pod scaling
- [x] Warm Spot node — min 1 application node, never cold-start
- [x] GitOps via Argo CD — sync waves: platform → security → application
- [x] GitHub Actions OIDC — no static AWS credentials in CI
- [x] Trivy scanning — blocks on CRITICAL before push to ECR
- [x] kube-prometheus-stack — metrics, alerting, dashboards
- [x] Three Grafana dashboards — cluster, inference, LLM-specific
- [x] Estimated cost/hour Prometheus metric
- [x] Document ingestion pipeline — S3 → chunk → embed → Qdrant
- [x] EBS gp3 encrypted PVC for Qdrant persistence
- [x] Terraform remote state — S3 + DynamoDB lock
- [x] SNS alerts — emmaubani.dev@gmail.com

**Required GitHub Repository Secrets**

| Secret | Description |
|--------|-------------|
| `AWS_ACCOUNT_ID` | Your AWS account ID |
| `AWS_REGION` | Target region (us-east-1) |
| `ECR_REPOSITORY` | ECR repository base path |
| `OIDC_ROLE_ARN` | IAM role for GitHub Actions OIDC |
| `CLUSTER_NAME` | EKS cluster name |

---

## Directory Structure

~~~text
rag-pipeline/
├── .github/
│   └── workflows/
│       ├── ci.yml                         # Test, scan, build, publish
│       └── deploy.yml                     # GitOps deployment workflow
├── apps/
│   ├── auth-service/
│   │   ├── main.py
│   │   ├── Dockerfile
│   │   └── requirements.txt
│   ├── ingestion/
│   │   ├── ingest.py
│   │   ├── Dockerfile
│   │   └── requirements.txt
│   ├── metrics-exporter/
│   │   ├── main.py
│   │   ├── Dockerfile
│   │   └── requirements.txt
│   ├── rag-api/
│   │   ├── main.py
│   │   ├── Dockerfile
│   │   └── requirements.txt
│   └── rate-limiter/
│       ├── main.py
│       ├── Dockerfile
│       └── requirements.txt
├── docs/
│   ├── architecture/
│   │   ├── architecture.png              # Production architecture diagram
│   │   ├── architecture.mermaid          # Editable diagram source
│   │   └── architecture.md
│   └── images/
│       └── rag-pipeline-architecture.png
├── k8s/
│   ├── argocd/app-of-apps.yaml
│   ├── autoscaling/keda-scaledobject.yaml
│   ├── deployments/
│   │   ├── auth-service.yaml
│   │   ├── qdrant.yaml
│   │   ├── rag-api.yaml
│   │   └── rate-limiter.yaml
│   ├── external-secrets/cluster-secret-store.yaml
│   ├── monitoring/grafana-dashboards.yaml
│   ├── namespaces/namespaces.yaml
│   ├── network-policies/rag-api-netpol.yaml
│   ├── base.yaml
│   └── ingress.yaml
├── scripts/
│   ├── bootstrap.sh
│   ├── destroy.sh
│   └── verify.sh
├── terraform/
│   ├── environments/{dev,prod}/
│   ├── modules/{vpc,eks,iam,ecr,secrets,s3,dns}/
│   ├── main.tf
│   ├── outputs.tf
│   ├── terraform.tfvars
│   ├── variables.tf
│   └── versions.tf
├── .env.example
├── .gitignore
├── docker-compose.yml
└── README.md
~~~

---
## Deployment

### Prerequisites

Replace these placeholders before running:

| File | Line | Replace With |
|------|------|-------------|
| `terraform/versions.tf` | `bucket = "YOUR_TFSTATE_BUCKET"` | Your S3 state bucket name |
| `terraform/terraform.tfvars` | `domain_name = "api.yourdomain.com"` | Your actual domain |
| `k8s/deployments/*.yaml` | `YOUR_ACCOUNT_ID` | Your AWS account ID |
| `k8s/deployments/rag-api.yaml` | `YOUR_RAG_API_ROLE_ARN` | IRSA role ARN from terraform output |

See [terraform/README.md](terraform/README.md) for infrastructure details.
See [k8s/README.md](k8s/README.md) for Kubernetes deployment details.

> **⚠️ Warning:** The bootstrap script installs Linkerd, KEDA, ESO,
> kube-prometheus-stack, and Argo CD before applying application
> manifests. These must be healthy before application pods start.
> Check each with `kubectl get pods -n <namespace>` before proceeding.

```bash
# Export Claude API key (never committed to git)
export TF_VAR_claude_api_key="sk-ant-YOUR_KEY"

# Run full bootstrap
bash scripts/bootstrap.sh

# Verify deployment
bash scripts/verify.sh

# Destroy when done
bash scripts/destroy.sh
```

[END README]