output "frontend_url" {
  description = "Public HTTPS URL of the frontend (CloudFront)"
  value       = local.public_frontend_base_url
}

output "backend_url" {
  description = "Public HTTPS URL of the backend API (CloudFront)"
  value       = local.public_backend_base_url
}

output "alb_frontend_dns" {
  description = "Frontend ALB DNS (direct HTTP; debugging only)"
  value       = aws_lb.frontend.dns_name
}

output "alb_backend_dns" {
  description = "Backend ALB DNS (direct HTTP; debugging only)"
  value       = aws_lb.backend.dns_name
}

output "ecr_backend_url" {
  description = "ECR repository URL — push backend image here"
  value       = aws_ecr_repository.backend.repository_url
}

output "ecr_frontend_url" {
  description = "ECR repository URL — push frontend image here"
  value       = aws_ecr_repository.frontend.repository_url
}

output "rds_endpoint" {
  description = "RDS PostgreSQL endpoint (private)"
  value       = aws_db_instance.postgres.address
  sensitive   = true
}

output "redis_endpoint" {
  description = "ElastiCache Redis endpoint (private)"
  value       = aws_elasticache_cluster.redis.cache_nodes[0].address
  sensitive   = true
}

output "ecs_cluster_name" {
  description = "ECS cluster name"
  value       = aws_ecs_cluster.main.name
}
