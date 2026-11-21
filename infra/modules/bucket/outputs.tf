output "name" {
  description = "The bucket's name."
  value       = aws_s3_bucket.this.bucket
}

output "arn" {
  description = "The bucket's ARN."
  value       = local.bucket_arn
}

output "policy" {
  description = "The bucket policy's document, which the tests read."
  value       = aws_s3_bucket_policy.this.policy
}

output "lifecycle_rules" {
  description = "The lifecycle rules the bucket carries, as given, which the tests read."
  value       = var.lifecycle_rules
}
