variable "aws_region" {
  description = "AWS region to deploy into"
  type        = string
  default     = "us-east-1"
}

variable "app_name" {
  description = "Short name used to prefix all AWS resources"
  type        = string
  default     = "chief-of-staff"
}

variable "environment" {
  description = "Deployment stage (prod, staging)"
  type        = string
  default     = "prod"
}

# ── Networking ────────────────────────────────────────────────────────────────

variable "vpc_cidr" {
  type    = string
  default = "10.0.0.0/16"
}

variable "public_subnet_cidrs" {
  type    = list(string)
  default = ["10.0.1.0/24", "10.0.2.0/24"]
}

variable "private_subnet_cidrs" {
  type    = list(string)
  default = ["10.0.10.0/24", "10.0.11.0/24"]
}

# ── ECS ───────────────────────────────────────────────────────────────────────

variable "backend_image_tag" {
  description = "ECR image tag for the backend container"
  type        = string
  default     = "latest"
}

variable "frontend_image_tag" {
  description = "ECR image tag for the frontend container"
  type        = string
  default     = "latest"
}

variable "backend_cpu" {
  type    = number
  default = 512
}

variable "backend_memory" {
  type    = number
  default = 1024
}

variable "frontend_cpu" {
  type    = number
  default = 512
}

variable "frontend_memory" {
  type    = number
  default = 1024
}

variable "backend_desired_count" {
  type    = number
  default = 1
}

variable "frontend_desired_count" {
  type    = number
  default = 1
}

# ── RDS ───────────────────────────────────────────────────────────────────────

variable "db_instance_class" {
  type    = string
  default = "db.t3.micro"
}

variable "db_name" {
  type    = string
  default = "chiefofstaff"
}

variable "db_username" {
  type      = string
  sensitive = true
}

variable "db_password" {
  type      = string
  sensitive = true
}

# ── Application secrets ───────────────────────────────────────────────────────

variable "jwt_secret" {
  type      = string
  sensitive = true
}

variable "openrouter_api_key" {
  type      = string
  sensitive = true
  default   = ""
}

variable "google_client_id" {
  type    = string
  default = ""
}

variable "google_client_secret" {
  type      = string
  sensitive = true
  default   = ""
}

variable "notion_client_id" {
  type    = string
  default = ""
}

variable "notion_client_secret" {
  type      = string
  sensitive = true
  default   = ""
}

variable "voyageai_api_key" {
  type      = string
  sensitive = true
  default   = ""
}

variable "groq_api_key" {
  type      = string
  sensitive = true
  default   = ""
}

variable "langsmith_tracing_enabled" {
  description = "When true and langsmith_api_key is set, send traces to LangSmith"
  type        = bool
  default     = false
}

variable "langsmith_api_key" {
  type      = string
  sensitive = true
  default   = ""
}

variable "langsmith_project" {
  description = "LangSmith project name (optional)"
  type        = string
  default     = ""
}

variable "langsmith_api_url" {
  description = "LangSmith API URL override (optional)"
  type        = string
  default     = ""
}

# ── CloudFront ────────────────────────────────────────────────────────────────

variable "cloudfront_price_class" {
  description = "CloudFront edge price class (all edge locations vs reduced)"
  type        = string
  default     = "PriceClass_100"
}
