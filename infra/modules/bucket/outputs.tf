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
  description = "The lifecycle rules the bucket's lifecycle configuration carries, read from the resource, which the tests read."
  value = flatten([
    for c in aws_s3_bucket_lifecycle_configuration.this : [
      for r in c.rule : {
        id         = r.id
        status     = r.status
        prefix     = one(r.filter[*].prefix)
        current    = try(one(r.expiration[*].days), null)
        marker     = try(one(r.expiration[*].expired_object_delete_marker), null)
        noncurrent = try(one(r.noncurrent_version_expiration[*].noncurrent_days), null)
        uploads    = try(one(r.abort_incomplete_multipart_upload[*].days_after_initiation), null)
      }
    ]
  ])
}

output "settings" {
  description = "The bucket's settings, read from the resources, which the tests read."
  value = {
    object_lock_enabled     = aws_s3_bucket.this.object_lock_enabled
    versioning              = aws_s3_bucket_versioning.this.versioning_configuration[0].status
    object_lock             = aws_s3_bucket_object_lock_configuration.this.object_lock_enabled
    sse_algorithm           = one(one(aws_s3_bucket_server_side_encryption_configuration.this.rule[*].apply_server_side_encryption_by_default[*].sse_algorithm))
    block_public_acls       = aws_s3_bucket_public_access_block.this.block_public_acls
    block_public_policy     = aws_s3_bucket_public_access_block.this.block_public_policy
    ignore_public_acls      = aws_s3_bucket_public_access_block.this.ignore_public_acls
    restrict_public_buckets = aws_s3_bucket_public_access_block.this.restrict_public_buckets
    object_ownership        = one(aws_s3_bucket_ownership_controls.this.rule[*].object_ownership)
    eventbridge             = aws_s3_bucket_notification.this.eventbridge
  }
}
