variable "aws_region" {
  description = "AWS region"
  type        = string
  default     = "us-east-1"
}

variable "project_name" {
  description = "Project name prefix"
  type        = string
  default     = "rag-pipeline"
}

variable "environment" {
  description = "Environment name"
  type        = string
  default     = "production"
}

variable "owner" {
  description = "Resource owner"
  type        = string
  default     = "emmanuel-ubani"
}

variable "vpc_cidr" {
  description = "VPC CIDR block"
  type        = string
  default     = "10.0.0.0/16"
}

variable "public_subnet_cidrs" {
  description = "Public subnet CIDRs"
  type        = list(string)
  default     = ["10.0.1.0/24", "10.0.2.0/24"]
}

variable "private_subnet_cidrs" {
  description = "Private subnet CIDRs"
  type        = list(string)
  default     = ["10.0.10.0/24", "10.0.11.0/24"]
}

variable "cluster_name" {
  description = "EKS cluster name"
  type        = string
  default     = "rag-pipeline-cluster"
}

variable "cluster_version" {
  description = "EKS Kubernetes version"
  type        = string
  default     = "1.29"
}

variable "system_node_instance_type" {
  description = "Instance type for system node group"
  type        = string
  default     = "t3.medium"
}

variable "app_node_instance_type" {
  description = "Instance type for application node group"
  type        = string
  default     = "t3.large"
}

variable "alert_email" {
  description = "Email for alerts"
  type        = string
  default     = "emmaubani.dev@gmail.com"
}

variable "domain_name" {
  description = "Domain name for the API"
  type        = string
  default     = "api.yourdomain.com"
}

variable "claude_api_key" {
  description = "Claude API key (stored in Secrets Manager)"
  type        = string
  sensitive   = true
  default     = ""
}
