# The test environment CI's `aws` job runs against: the test bucket, and the queues and
# rules that infra/test/routes.json lists. .github/workflows/infra.yml applies this
# after each merge to main that changes infra/ or that workflow, and on a manual
# dispatch from main, in the `test` GitHub environment, with the apply role
# infra/test/bootstrap creates. docs/what-a-person-runs.md carries the owner's steps.
#
# The bucket and its routes share one configuration here only because test lets the
# apply role change the test bucket's settings. 07-deployment.md's "Environment rule
# 10: how an environment's infrastructure is applied" says how shadow, paper and live
# split them.

terraform {
  required_version = "~> 1.13"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }

  # The bucket arrives through `tofu init -backend-config=<file>`, so no tracked file
  # names it. The apply role may write only this key and its lock file.
  backend "s3" {
    key          = "test/terraform.tfstate"
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

  # The bootstrap creates both roles with these fixed names, and AWS Organizations
  # creates the owner's role in every account it creates.
  test_role  = "arn:aws:iam::${local.account_id}:role/ithildin-test-ci"
  apply_role = "arn:aws:iam::${local.account_id}:role/ithildin-test-apply"
  owner_role = "arn:aws:iam::${local.account_id}:role/OrganizationAccountAccessRole"

  # tests/support/routes_file.py writes this file, and tests/test_routes.py fails when
  # the committed copy differs from what it writes.
  routes_file = jsondecode(file("${path.module}/routes.json"))
}

module "bucket" {
  source = "../modules/bucket"

  name = "ithildin-runtime-test"

  allowed_principal_arns  = [local.test_role, local.apply_role, local.owner_role]
  runtime_principal_arns  = [local.test_role]
  settings_principal_arns = [local.apply_role, local.owner_role]
  owner_principal_arns    = [local.owner_role]
  switch_clearer_arns     = [local.owner_role]
  stop_clearer_arns       = [local.owner_role]
  off_switch_clearer_arns = [local.owner_role]

  # No test write can lock a version for longer than a day, so the lifecycle can
  # always empty the bucket.
  max_retention_days = 1

  # Every blob in the test bucket can be made again by a run, so its lifecycle may
  # expire everything.
  regenerable_only = true

  # Everything expires. S3 rounds each expiry up to the next midnight UTC, so a current
  # version expires one to two days after it is written, and the bucket holds no
  # version or delete marker at least three days after the last write.
  lifecycle_rules = [
    {
      id                      = "expire-everything"
      prefix                  = ""
      expire_current_days     = 1
      expire_noncurrent_days  = 1
      abort_incomplete_upload = 1
    },
    {
      id                       = "remove-old-delete-markers"
      prefix                   = ""
      expire_old_delete_marker = true
    },
  ]
}

module "routes" {
  source = "../modules/routes"

  name_prefix                   = "ithildin-test"
  bucket                        = module.bucket.name
  queues                        = local.routes_file.queues
  rules                         = local.routes_file.rules
  message_retention_seconds     = 3600
  dead_letter_retention_seconds = 3600
}

output "queues" {
  description = "Each route's queue and dead-letter queue name, by reader."
  value       = module.routes.queues
}
