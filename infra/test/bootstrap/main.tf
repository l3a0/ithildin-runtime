# What CI needs in the test account before it can run: the OpenTofu state bucket,
# GitHub's OIDC provider, the apply role .github/workflows/infra.yml uses, the test role
# ci.yml's `aws` job uses, and a budget alarm with the account's alarm topic. The owner applies this from the laptop,
# through OrganizationAccountAccessRole, because the CI it creates cannot apply it.
# docs/what-a-person-runs.md carries the steps.

terraform {
  required_version = "~> 1.13"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }

  # The bucket arrives through `tofu init -backend-config=<file>`, so no tracked file
  # names it. The apply role may write only infra/test's key, and this one stays the
  # owner's.
  backend "s3" {
    key          = "bootstrap/terraform.tfstate"
    region       = "us-east-1"
    encrypt      = true
    use_lockfile = true
  }
}

provider "aws" {
  region = "us-east-1"
}

data "aws_caller_identity" "current" {}

locals {
  account_id = data.aws_caller_identity.current.account_id

  # One provider per issuer in an account, so this ARN is fixed once the account is.
  github_oidc_provider_arn = "arn:aws:iam::${local.account_id}:oidc-provider/token.actions.githubusercontent.com"

  # infra/test names the test bucket, the queues and the rules with these.
  test_bucket  = "ithildin-runtime-test"
  queue_prefix = "ithildin-test"
}

# -- the state bucket ---------------------------------------------------------------

# The owner creates the bucket with one `aws s3api create-bucket`, and this adopts it,
# because the backend that holds this configuration's state must exist before the first
# `tofu init`. A new bucket comes encrypted with S3's managed key and with public access
# blocked, but not versioned.
import {
  for_each = var.adopt_existing ? toset([var.state_bucket]) : toset([])
  to       = aws_s3_bucket.state
  id       = each.value
}

resource "aws_s3_bucket" "state" {
  bucket = var.state_bucket

  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_s3_bucket_versioning" "state" {
  bucket = aws_s3_bucket.state.id

  versioning_configuration {
    status = "Enabled"
  }
}

# -- GitHub's OIDC provider ---------------------------------------------------------

resource "aws_iam_openid_connect_provider" "github" {
  url            = "https://token.actions.githubusercontent.com"
  client_id_list = ["sts.amazonaws.com"]
}

# -- the budget alarm ---------------------------------------------------------------

# AWS Budgets charges nothing for an account's first two budgets. It publishes to the
# account's one alarm topic, as every account has one, and the owner subscribes an address to the topic by hand and confirms it, as
# docs/what-a-person-runs.md's "Confirm an alarm email subscription" says. So the
# address enters no tracked file and no OpenTofu state, as with every other alarm email.
resource "aws_sns_topic" "alarms" {
  name = "ithildin-test-alarms"
}

resource "aws_sns_topic_policy" "alarms" {
  arn = aws_sns_topic.alarms.arn

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "AdmitOnlyThisAccountsBudgets"
      Effect    = "Allow"
      Principal = { Service = "budgets.amazonaws.com" }
      Action    = "SNS:Publish"
      Resource  = aws_sns_topic.alarms.arn
      Condition = { StringEquals = { "aws:SourceAccount" = local.account_id } }
    }]
  })
}

resource "aws_budgets_budget" "monthly" {
  name         = "ithildin-test-monthly"
  budget_type  = "COST"
  limit_amount = var.budget_limit
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  notification {
    comparison_operator       = "GREATER_THAN"
    threshold                 = 100
    threshold_type            = "PERCENTAGE"
    notification_type         = "ACTUAL"
    subscriber_sns_topic_arns = [aws_sns_topic.alarms.arn]
  }

  depends_on = [aws_sns_topic_policy.alarms]
}

output "alarm_topic_arn" {
  description = "The test account's one alarm topic, which the budget alarm publishes to and the owner subscribes an address to by hand."
  value       = aws_sns_topic.alarms.arn
}
