# Two roles, each assumed through GitHub's OIDC provider by one job on main.
#
# 1. The apply role, for .github/workflows/infra.yml's apply job in the `test` GitHub
#    environment. It plans and applies infra/test, so it carries the plan credential's
#    limit: no ReadOnlyAccess and no ssm:Get*. Its reads are listed. It holds no IAM
#    action, so no apply can widen a role, and needs no iam:PassRole, because a rule
#    reaches a queue through the queue's own policy. Test holds no separate plan
#    credential until #32's drift check builds one.
# 2. The test role, for ci.yml's `aws` job, with the grants #78's table gives.
#
# Every policy is a jsonencode() literal rather than an aws_iam_policy_document, because
# under tofu test's mock provider that data source returns a random string and no test
# could read the document.

locals {
  # The repository's subject template includes repo, context and job_workflow_ref, and
  # GitHub writes the repository with its owner and repository ids. A token printed on
  # this repository's pull request read
  # repo:l3a0@5200900/ithildin-runtime@1437672674:pull_request:job_workflow_ref:l3a0/ithildin-runtime/.github/workflows/<file>@refs/pull/<n>/merge,
  # so a push to main reads ref:refs/heads/main as its context, and a job in an
  # environment reads environment:<name>.
  github_subject_prefix = "repo:l3a0@5200900/ithildin-runtime@1437672674"
  workflows             = "l3a0/ithildin-runtime/.github/workflows"

  test_subject  = "${local.github_subject_prefix}:ref:refs/heads/main:job_workflow_ref:${local.workflows}/ci.yml@refs/heads/main"
  apply_subject = "${local.github_subject_prefix}:environment:test:job_workflow_ref:${local.workflows}/infra.yml@refs/heads/main"

  queues = "arn:aws:sqs:us-east-1:${local.account_id}:${local.queue_prefix}-*"
  rules  = "arn:aws:events:us-east-1:${local.account_id}:rule/${local.queue_prefix}-*"
  bucket = "arn:aws:s3:::${local.test_bucket}"

  # Every role but execution's is refused the broker key, in every environment's
  # account, by docs/architecture/07-deployment.md's "What guards the key inside each
  # environment's account".
  broker_key = "arn:aws:ssm:us-east-1:${local.account_id}:parameter/ithildin/*/broker/*"
}

# -- the apply role -----------------------------------------------------------------

resource "aws_iam_role" "apply" {
  name = "ithildin-test-apply"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Federated = local.github_oidc_provider_arn }
      Action    = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com"
          "token.actions.githubusercontent.com:sub" = local.apply_subject
        }
      }
    }]
  })

  depends_on = [aws_iam_openid_connect_provider.github]
}

resource "aws_iam_role_policy" "apply" {
  name = "apply-infra-test"
  role = aws_iam_role.apply.name

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "ListTheStateBucket"
        Effect   = "Allow"
        Action   = ["s3:ListBucket"]
        Resource = ["arn:aws:s3:::${var.state_bucket}"]
      },
      {
        Sid    = "ReadAndWriteInfraTestsStateAndLock"
        Effect = "Allow"
        Action = ["s3:GetObject", "s3:PutObject"]
        Resource = [
          "arn:aws:s3:::${var.state_bucket}/test/terraform.tfstate",
          "arn:aws:s3:::${var.state_bucket}/test/terraform.tfstate.tflock",
        ]
      },
      {
        Sid      = "ReleaseTheLock"
        Effect   = "Allow"
        Action   = ["s3:DeleteObject"]
        Resource = ["arn:aws:s3:::${var.state_bucket}/test/terraform.tfstate.tflock"]
      },
      {
        # The bucket's own settings, never its objects: every resource is the bucket's
        # ARN, which matches no object. No delete of the bucket and no tag write, since
        # nothing here sets a tag and retiring test runs from the laptop.
        Sid    = "ManageTheTestBucket"
        Effect = "Allow"
        Action = [
          "s3:CreateBucket",
          "s3:Get*",
          "s3:List*",
          "s3:PutBucketPolicy",
          "s3:DeleteBucketPolicy",
          "s3:PutBucketVersioning",
          "s3:PutBucketObjectLockConfiguration",
          "s3:PutEncryptionConfiguration",
          "s3:PutBucketPublicAccessBlock",
          "s3:PutBucketOwnershipControls",
          "s3:PutBucketNotification",
          "s3:PutLifecycleConfiguration",
        ]
        Resource = [local.bucket]
      },
      {
        Sid    = "ManageTheTestQueues"
        Effect = "Allow"
        Action = [
          "sqs:CreateQueue",
          "sqs:DeleteQueue",
          "sqs:SetQueueAttributes",
          "sqs:GetQueueAttributes",
          "sqs:GetQueueUrl",
          "sqs:ListQueueTags",
          "sqs:ListDeadLetterSourceQueues",
        ]
        Resource = [local.queues]
      },
      {
        Sid    = "ManageTheTestRules"
        Effect = "Allow"
        Action = [
          "events:PutRule",
          "events:DeleteRule",
          "events:DescribeRule",
          "events:EnableRule",
          "events:DisableRule",
          "events:PutTargets",
          "events:RemoveTargets",
          "events:ListTargetsByRule",
          "events:ListTagsForResource",
        ]
        Resource = [local.rules]
      },
      {
        Sid      = "RefuseTheBrokerKey"
        Effect   = "Deny"
        Action   = ["ssm:GetParameter", "ssm:GetParameters", "ssm:GetParameterHistory"]
        Resource = [local.broker_key]
      },
    ]
  })
}

# -- the test role ------------------------------------------------------------------

resource "aws_iam_role" "test" {
  name = "ithildin-test-ci"

  # One hour, above the `aws` job's 15-minute ceiling.
  max_session_duration = 3600

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Federated = local.github_oidc_provider_arn }
      Action    = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com"
          "token.actions.githubusercontent.com:sub" = local.test_subject
        }
      }
    }]
  })

  depends_on = [aws_iam_openid_connect_provider.github]
}

# The role grants no sqs:SendMessage, so every message the suites read came from S3
# through EventBridge.
resource "aws_iam_role_policy" "test" {
  name = "run-the-aws-suites"
  role = aws_iam_role.test.name

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "CreateAndReadARunsKeys"
        Effect = "Allow"
        Action = ["s3:PutObject", "s3:PutObjectRetention", "s3:GetObject"]
        Resource = [
          "${local.bucket}/runs/ci/*",
          "${local.bucket}/leases/plans/ci/*",
        ]
      },
      {
        # With no prefix condition, so a missing key answers 404 rather than 403. A
        # request for one object carries no s3:prefix, so a prefix condition would turn
        # every 404 into a 403.
        Sid      = "ListTheTestBucket"
        Effect   = "Allow"
        Action   = ["s3:ListBucket"]
        Resource = [local.bucket]
      },
      {
        Sid    = "ReadTheTestQueues"
        Effect = "Allow"
        Action = [
          "sqs:ReceiveMessage",
          "sqs:DeleteMessage",
          "sqs:ChangeMessageVisibility",
          "sqs:GetQueueUrl",
          "sqs:GetQueueAttributes",
          "sqs:PurgeQueue",
        ]
        Resource = [local.queues]
      },
      {
        Sid      = "RefuseTheBrokerKey"
        Effect   = "Deny"
        Action   = ["ssm:GetParameter", "ssm:GetParameters", "ssm:GetParameterHistory"]
        Resource = [local.broker_key]
      },
      {
        Sid      = "RefuseEveryOtherService"
        Effect   = "Deny"
        Action   = ["ssm:*", "ec2:*", "iam:*", "events:*", "kms:*", "sts:AssumeRole"]
        Resource = ["*"]
      },
      {
        Sid    = "RefuseDeletesAndBucketSettings"
        Effect = "Deny"
        Action = [
          "s3:DeleteObject",
          "s3:DeleteObjectVersion",
          "s3:BypassGovernanceRetention",
          "s3:PutBucket*",
          "s3:PutLifecycleConfiguration",
        ]
        Resource = ["*"]
      },
    ]
  })
}

output "test_role_arn" {
  description = "The test role's ARN, for the AWS_TEST_ROLE_ARN secret."
  value       = aws_iam_role.test.arn
}

output "apply_role_arn" {
  description = "The apply role's ARN, for the AWS_TEST_APPLY_ROLE_ARN secret."
  value       = aws_iam_role.apply.arn
}
