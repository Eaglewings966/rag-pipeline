output "cluster_name" {
  description = "EKS cluster name"
  value       = aws_eks_cluster.main.name
}

output "cluster_endpoint" {
  description = "EKS cluster endpoint"
  value       = aws_eks_cluster.main.endpoint
}

output "cluster_oidc_issuer" {
  description = "EKS OIDC issuer URL"
  value       = aws_eks_cluster.main.identity[0].oidc[0].issuer
}

output "eso_role_arn" {
  description = "IAM role ARN for External Secrets Operator"
  value       = aws_iam_role.eso.arn
}

output "rag_api_role_arn" {
  description = "IAM role ARN for RAG API pod"
  value       = aws_iam_role.rag_api.arn
}

output "github_actions_role_arn" {
  description = "IAM role ARN for GitHub Actions OIDC"
  value       = aws_iam_role.github_actions.arn
}

output "documents_bucket" {
  description = "S3 bucket for document ingestion"
  value       = aws_s3_bucket.documents.bucket
}

output "ecr_rag_api_url" {
  description = "ECR URL for rag-api image"
  value       = aws_ecr_repository.rag_api.repository_url
}

output "ecr_auth_service_url" {
  description = "ECR URL for auth-service image"
  value       = aws_ecr_repository.auth_service.repository_url
}

output "ecr_rate_limiter_url" {
  description = "ECR URL for rate-limiter image"
  value       = aws_ecr_repository.rate_limiter.repository_url
}

output "ecr_metrics_exporter_url" {
  description = "ECR URL for metrics-exporter image"
  value       = aws_ecr_repository.metrics_exporter.repository_url
}

output "sns_topic_arn" {
  description = "SNS topic for alerts"
  value       = aws_sns_topic.alerts.arn
}

output "vpc_id" {
  description = "VPC ID"
  value       = aws_vpc.main.id
}

output "configure_kubectl" {
  description = "Command to configure kubectl"
  value       = "aws eks update-kubeconfig --region ${var.aws_region} --name ${var.cluster_name}"
}

output "destroy_command" {
  description = "Command to destroy all resources"
  value       = "bash scripts/destroy.sh"
}
