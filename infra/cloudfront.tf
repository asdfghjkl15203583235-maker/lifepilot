# HTTPS + edge access in front of public ALBs (viewer TLS; origin stays HTTP on port 80).

data "aws_cloudfront_cache_policy" "caching_disabled" {
  name = "Managed-CachingDisabled"
}

data "aws_cloudfront_origin_request_policy" "all_viewer" {
  name = "Managed-AllViewer"
}

resource "aws_cloudfront_distribution" "frontend" {
  enabled             = true
  is_ipv6_enabled     = true
  comment             = "${local.prefix} Next.js frontend"
  price_class         = var.cloudfront_price_class
  wait_for_deployment = true

  origin {
    domain_name = aws_lb.frontend.dns_name
    origin_id   = "${local.prefix}-frontend-alb"

    custom_origin_config {
      http_port              = 80
      https_port             = 443
      origin_protocol_policy = "http-only"
      origin_ssl_protocols     = ["TLSv1.2"]
      origin_read_timeout      = 60
      origin_keepalive_timeout = 5
    }
  }

  default_cache_behavior {
    target_origin_id       = "${local.prefix}-frontend-alb"
    viewer_protocol_policy = "redirect-to-https"
    allowed_methods        = ["DELETE", "GET", "HEAD", "OPTIONS", "PATCH", "POST", "PUT"]
    cached_methods         = ["GET", "HEAD"]
    compress               = true

    cache_policy_id          = data.aws_cloudfront_cache_policy.caching_disabled.id
    origin_request_policy_id = data.aws_cloudfront_origin_request_policy.all_viewer.id
  }

  restrictions {
    geo_restriction {
      restriction_type = "none"
    }
  }

  viewer_certificate {
    cloudfront_default_certificate = true
    minimum_protocol_version       = "TLSv1.2_2021"
  }

  tags = merge(local.common_tags, { Name = "${local.prefix}-frontend-cf" })
}

resource "aws_cloudfront_distribution" "backend" {
  enabled             = true
  is_ipv6_enabled     = true
  comment             = "${local.prefix} FastAPI backend"
  price_class         = var.cloudfront_price_class
  wait_for_deployment = true

  origin {
    domain_name = aws_lb.backend.dns_name
    origin_id   = "${local.prefix}-backend-alb"

    custom_origin_config {
      http_port              = 80
      https_port             = 443
      origin_protocol_policy = "http-only"
      origin_ssl_protocols     = ["TLSv1.2"]
      # Origin read timeout: AWS allows 1–120 s without a quota increase; >120 needs Service Quotas.
      # Long SSE/voice streams may hit this cap; raise quota if needed.
      origin_read_timeout      = 120
      origin_keepalive_timeout = 60
    }
  }

  default_cache_behavior {
    target_origin_id       = "${local.prefix}-backend-alb"
    viewer_protocol_policy = "redirect-to-https"
    allowed_methods        = ["DELETE", "GET", "HEAD", "OPTIONS", "PATCH", "POST", "PUT"]
    cached_methods         = ["GET", "HEAD"]
    compress               = true

    cache_policy_id          = data.aws_cloudfront_cache_policy.caching_disabled.id
    origin_request_policy_id = data.aws_cloudfront_origin_request_policy.all_viewer.id
  }

  restrictions {
    geo_restriction {
      restriction_type = "none"
    }
  }

  viewer_certificate {
    cloudfront_default_certificate = true
    minimum_protocol_version       = "TLSv1.2_2021"
  }

  tags = merge(local.common_tags, { Name = "${local.prefix}-backend-cf" })
}

locals {
  public_frontend_base_url = "https://${aws_cloudfront_distribution.frontend.domain_name}"
  public_backend_base_url  = "https://${aws_cloudfront_distribution.backend.domain_name}"
}
