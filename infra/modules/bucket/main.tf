# A runtime bucket: versioned, Object Lock enabled at creation, encrypted with S3's
# managed key, sending its events to EventBridge, and carrying the policy statements of
# docs/architecture/07-deployment.md's "What the bucket's policy refuses, and to whom".
# Every environment calls this module, so the suites that run against the test bucket
# meet the statements production uses. Who the statements name, the retention cap and
# the lifecycle are inputs.
#
# The policy is a jsonencode() literal rather than an aws_iam_policy_document, because
# under tofu test's mock provider that data source returns a random string and no test
# could read the statements.

terraform {
  required_version = "~> 1.13"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }
}

locals {
  bucket_arn = "arn:aws:s3:::${var.name}"
  objects    = "${local.bucket_arn}/*"

  # The prefixes nobody may rewrite or delete: a run's records, the journal and the
  # simulated broker's accounts.
  create_only = [for prefix in ["runs", "journal", "sim"] : "${local.bucket_arn}/${prefix}/*"]

  people = concat(var.owner_principal_arns, var.operator_principal_arns)

  # A statement refused to every principal but those named. An empty list refuses
  # everyone, since IAM refuses a condition with no values.
  all_but = {
    switch   = var.switch_clearer_arns
    stop     = var.stop_clearer_arns
    off      = var.off_switch_clearer_arns
    settings = var.settings_principal_arns
  }
  all_but_condition = {
    for name, arns in local.all_but :
    name => length(arns) > 0 ? { Condition = { StringNotLike = { "aws:PrincipalArn" = arns } } } : {}
  }

  statements = concat(
    [
      {
        Sid       = "RefuseUnlistedPrincipals"
        Effect    = "Deny"
        Principal = "*"
        Action    = "s3:*"
        Resource  = [local.bucket_arn, local.objects]
        Condition = { StringNotLike = { "aws:PrincipalArn" = var.allowed_principal_arns } }
      },
      {
        Sid       = "RefuseCreateOnlyWritesWithoutIfNoneMatch"
        Effect    = "Deny"
        Principal = "*"
        Action    = "s3:PutObject"
        Resource  = local.create_only
        Condition = { Null = { "s3:if-none-match" = "true" } }
      },
      {
        Sid       = "RefuseCreateOnlyDeletes"
        Effect    = "Deny"
        Principal = "*"
        Action    = ["s3:DeleteObject", "s3:DeleteObjectVersion"]
        Resource  = local.create_only
      },
      {
        Sid       = "RefuseLeaseDeletes"
        Effect    = "Deny"
        Principal = "*"
        Action    = ["s3:DeleteObject", "s3:DeleteObjectVersion"]
        Resource  = ["${local.bucket_arn}/leases/*"]
      },
    ],
    [for s in [
      {
        Sid       = "RefuseRuntimeLeaseWritesWithNeitherCondition"
        Effect    = "Deny"
        Principal = "*"
        Action    = "s3:PutObject"
        Resource  = ["${local.bucket_arn}/leases/*"]
        Condition = {
          Null    = { "s3:if-none-match" = "true", "s3:if-match" = "true" }
          ArnLike = { "aws:PrincipalArn" = var.runtime_principal_arns }
        }
      },
      {
        Sid         = "RefuseRuntimeWritesWithoutIfNoneMatch"
        Effect      = "Deny"
        Principal   = "*"
        Action      = "s3:PutObject"
        NotResource = ["${local.bucket_arn}/leases/*"]
        Condition = {
          Null    = { "s3:if-none-match" = "true" }
          ArnLike = { "aws:PrincipalArn" = var.runtime_principal_arns }
        }
      },
      {
        Sid       = "RefuseRuntimeDeletes"
        Effect    = "Deny"
        Principal = "*"
        Action    = ["s3:DeleteObject", "s3:DeleteObjectVersion"]
        Resource  = [local.objects]
        Condition = { ArnLike = { "aws:PrincipalArn" = var.runtime_principal_arns } }
      },
    ] : s if length(var.runtime_principal_arns) > 0],
    [for s in [
      {
        Sid       = "RefusePeoplesAccountLeaseWritesWithoutIfMatch"
        Effect    = "Deny"
        Principal = "*"
        Action    = "s3:PutObject"
        Resource  = ["${local.bucket_arn}/leases/accounts/*"]
        Condition = {
          Null    = { "s3:if-match" = "true" }
          ArnLike = { "aws:PrincipalArn" = local.people }
        }
      },
    ] : s if length(local.people) > 0],
    [for s in [
      {
        Sid       = "RefuseOperatorVersionDeletes"
        Effect    = "Deny"
        Principal = "*"
        Action    = "s3:DeleteObjectVersion"
        Resource  = [local.objects]
        Condition = { ArnLike = { "aws:PrincipalArn" = var.operator_principal_arns } }
      },
    ] : s if length(var.operator_principal_arns) > 0],
    [
      merge({
        Sid       = "RefuseSwitchDeletes"
        Effect    = "Deny"
        Principal = "*"
        Action    = ["s3:DeleteObject", "s3:DeleteObjectVersion"]
        Resource  = ["${local.bucket_arn}/accounts/*/switch.json"]
      }, local.all_but_condition.switch),
      merge({
        Sid       = "RefuseLossStopDeletes"
        Effect    = "Deny"
        Principal = "*"
        Action    = ["s3:DeleteObject", "s3:DeleteObjectVersion"]
        Resource  = ["${local.bucket_arn}/accounts/*/stop.json"]
      }, local.all_but_condition.stop),
      merge({
        Sid       = "RefuseOffSwitchDeletes"
        Effect    = "Deny"
        Principal = "*"
        Action    = ["s3:DeleteObject", "s3:DeleteObjectVersion"]
        Resource  = ["${local.bucket_arn}/accounts/*/strategies/*/off.json"]
      }, local.all_but_condition.off),
      merge({
        Sid       = "RefuseSettingsChanges"
        Effect    = "Deny"
        Principal = "*"
        Action = [
          "s3:PutBucketPolicy",
          "s3:DeleteBucketPolicy",
          "s3:PutBucketVersioning",
          "s3:PutBucketObjectLockConfiguration",
          "s3:PutLifecycleConfiguration",
          "s3:PutBucketNotification",
        ]
        Resource = [local.bucket_arn]
      }, local.all_but_condition.settings),
    ],
    [for s in [
      {
        Sid       = "RefuseRetentionPastTheCap"
        Effect    = "Deny"
        Principal = "*"
        Action    = ["s3:PutObject", "s3:PutObjectRetention"]
        Resource  = [local.objects]
        Condition = {
          NumericGreaterThan = { "s3:object-lock-remaining-retention-days" = tostring(var.max_retention_days) }
        }
      },
    ] : s if var.max_retention_days != null],
  )
}

resource "aws_s3_bucket" "this" {
  bucket              = var.name
  object_lock_enabled = true

  # Retiring an environment takes a commit that removes this, once its bucket is empty.
  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_s3_bucket_versioning" "this" {
  bucket = aws_s3_bucket.this.id

  versioning_configuration {
    status = "Enabled"
  }
}

# Object Lock with no default retention: each write that needs a retention date sets
# its own, in governance mode.
resource "aws_s3_bucket_object_lock_configuration" "this" {
  bucket              = aws_s3_bucket.this.id
  object_lock_enabled = "Enabled"

  depends_on = [aws_s3_bucket_versioning.this]
}

resource "aws_s3_bucket_server_side_encryption_configuration" "this" {
  bucket = aws_s3_bucket.this.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "this" {
  bucket                  = aws_s3_bucket.this.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "this" {
  bucket = aws_s3_bucket.this.id

  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_notification" "this" {
  bucket      = aws_s3_bucket.this.id
  eventbridge = true
}

resource "aws_s3_bucket_lifecycle_configuration" "this" {
  count  = length(var.lifecycle_rules) > 0 ? 1 : 0
  bucket = aws_s3_bucket.this.id

  dynamic "rule" {
    for_each = var.lifecycle_rules
    content {
      id     = rule.value.id
      status = "Enabled"

      filter {
        prefix = rule.value.prefix
      }

      dynamic "expiration" {
        for_each = rule.value.expire_current_days != null ? [rule.value.expire_current_days] : []
        content {
          days = expiration.value
        }
      }

      dynamic "expiration" {
        for_each = rule.value.expire_old_delete_marker ? [true] : []
        content {
          expired_object_delete_marker = true
        }
      }

      dynamic "noncurrent_version_expiration" {
        for_each = rule.value.expire_noncurrent_days != null ? [rule.value.expire_noncurrent_days] : []
        content {
          noncurrent_days = noncurrent_version_expiration.value
        }
      }

      dynamic "abort_incomplete_multipart_upload" {
        for_each = rule.value.abort_incomplete_upload != null ? [rule.value.abort_incomplete_upload] : []
        content {
          days_after_initiation = abort_incomplete_multipart_upload.value
        }
      }
    }
  }

  depends_on = [aws_s3_bucket_versioning.this]
}

resource "aws_s3_bucket_policy" "this" {
  bucket = aws_s3_bucket.this.id
  policy = jsonencode({ Version = "2012-10-17", Statement = local.statements })

  depends_on = [aws_s3_bucket_public_access_block.this]
}
