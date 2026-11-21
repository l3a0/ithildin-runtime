# Plan-mode checks on the bootstrap's roles, against literals. Every run plans, because
# test cleanup cannot destroy a prevent_destroy resource.

mock_provider "aws" {
  mock_data "aws_caller_identity" {
    defaults = {
      account_id = "000000000000"
    }
  }

  # The topic policy's ARN must look like an ARN, and the mock's random string does not.
  mock_resource "aws_sns_topic" {
    defaults = {
      arn = "arn:aws:sns:us-east-1:000000000000:ithildin-test-alarms"
    }
  }
}

variables {
  state_bucket   = "example-state"
  adopt_existing = false
}

run "each_trust_names_one_workflow_file_on_main" {
  command = plan

  assert {
    condition = jsondecode(aws_iam_role.test.assume_role_policy).Statement == [{
      Effect    = "Allow"
      Principal = { Federated = "arn:aws:iam::000000000000:oidc-provider/token.actions.githubusercontent.com" }
      Action    = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com"
          "token.actions.githubusercontent.com:sub" = "repo:l3a0@5200900/ithildin-runtime@1437672674:ref:refs/heads/main:job_workflow_ref:l3a0/ithildin-runtime/.github/workflows/ci.yml@refs/heads/main"
        }
      }
    }]
    error_message = "The test role's trust is not exactly ci.yml on main."
  }

  assert {
    condition = jsondecode(aws_iam_role.apply.assume_role_policy).Statement == [{
      Effect    = "Allow"
      Principal = { Federated = "arn:aws:iam::000000000000:oidc-provider/token.actions.githubusercontent.com" }
      Action    = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com"
          "token.actions.githubusercontent.com:sub" = "repo:l3a0@5200900/ithildin-runtime@1437672674:environment:test:job_workflow_ref:l3a0/ithildin-runtime/.github/workflows/infra.yml@refs/heads/main"
        }
      }
    }]
    error_message = "The apply role's trust is not exactly infra.yml in the test environment on main."
  }

  assert {
    condition     = aws_iam_role.test.max_session_duration == 3600
    error_message = "The test role's sessions do not last one hour."
  }

  assert {
    condition     = aws_iam_openid_connect_provider.github.url == "https://token.actions.githubusercontent.com" && aws_iam_openid_connect_provider.github.client_id_list == toset(["sts.amazonaws.com"])
    error_message = "The OIDC provider is not GitHub's, with the sts audience alone."
  }
}

run "the_state_bucket_keeps_every_version" {
  command = plan

  assert {
    condition     = aws_s3_bucket.state.bucket == "example-state" && aws_s3_bucket_versioning.state.versioning_configuration[0].status == "Enabled"
    error_message = "The state bucket is not versioned."
  }
}

run "the_test_role_holds_exactly_the_tables_grants" {
  command = plan

  assert {
    condition = jsondecode(aws_iam_role_policy.test.policy).Statement == [
      {
        Sid      = "CreateAndReadARunsKeys"
        Effect   = "Allow"
        Action   = ["s3:PutObject", "s3:PutObjectRetention", "s3:GetObject"]
        Resource = ["arn:aws:s3:::ithildin-runtime-test/runs/ci/*", "arn:aws:s3:::ithildin-runtime-test/leases/plans/ci/*"]
      },
      {
        Sid      = "ListTheTestBucket"
        Effect   = "Allow"
        Action   = ["s3:ListBucket"]
        Resource = ["arn:aws:s3:::ithildin-runtime-test"]
      },
      {
        Sid      = "ReadTheTestQueues"
        Effect   = "Allow"
        Action   = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:ChangeMessageVisibility", "sqs:GetQueueUrl", "sqs:GetQueueAttributes", "sqs:PurgeQueue"]
        Resource = ["arn:aws:sqs:us-east-1:000000000000:ithildin-test-*"]
      },
      {
        Sid      = "RefuseTheBrokerKey"
        Effect   = "Deny"
        Action   = ["ssm:GetParameter", "ssm:GetParameters", "ssm:GetParameterHistory"]
        Resource = ["arn:aws:ssm:us-east-1:000000000000:parameter/ithildin/*/broker/*"]
      },
      {
        Sid      = "RefuseEveryOtherService"
        Effect   = "Deny"
        Action   = ["ssm:*", "ec2:*", "iam:*", "events:*", "kms:*", "sts:AssumeRole"]
        Resource = ["*"]
      },
      {
        Sid      = "RefuseDeletesAndBucketSettings"
        Effect   = "Deny"
        Action   = ["s3:DeleteObject", "s3:DeleteObjectVersion", "s3:BypassGovernanceRetention", "s3:PutBucket*", "s3:PutLifecycleConfiguration"]
        Resource = ["*"]
      },
    ]
    error_message = "The test role's policy is not exactly the grants and refusals #78's table gives."
  }
}

run "the_apply_role_holds_exactly_its_listed_grants" {
  command = plan

  # Written out in full, so a widened action, a wider resource, a dropped refusal of the
  # broker key or a new statement fails here.
  assert {
    condition = jsondecode(aws_iam_role_policy.apply.policy).Statement == [
      {
        Sid      = "ListTheStateBucket"
        Effect   = "Allow"
        Action   = ["s3:ListBucket"]
        Resource = ["arn:aws:s3:::example-state"]
      },
      {
        Sid    = "ReadAndWriteInfraTestsStateAndLock"
        Effect = "Allow"
        Action = ["s3:GetObject", "s3:PutObject"]
        Resource = [
          "arn:aws:s3:::example-state/test/terraform.tfstate",
          "arn:aws:s3:::example-state/test/terraform.tfstate.tflock",
        ]
      },
      {
        Sid      = "ReleaseTheLock"
        Effect   = "Allow"
        Action   = ["s3:DeleteObject"]
        Resource = ["arn:aws:s3:::example-state/test/terraform.tfstate.tflock"]
      },
      {
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
        Resource = ["arn:aws:s3:::ithildin-runtime-test"]
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
        Resource = ["arn:aws:sqs:us-east-1:000000000000:ithildin-test-*"]
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
        Resource = ["arn:aws:events:us-east-1:000000000000:rule/ithildin-test-*"]
      },
      {
        Sid      = "RefuseTheBrokerKey"
        Effect   = "Deny"
        Action   = ["ssm:GetParameter", "ssm:GetParameters", "ssm:GetParameterHistory"]
        Resource = ["arn:aws:ssm:us-east-1:000000000000:parameter/ithildin/*/broker/*"]
      },
    ]
    error_message = "The apply role's policy is not exactly its listed reads and writes on the state key, the test bucket's settings, the test queues and rules, and the refusal of the broker key."
  }

  assert {
    condition = alltrue([
      for s in jsondecode(aws_iam_role_policy.apply.policy).Statement :
      alltrue([for a in s.Action : !startswith(a, "iam:") && !startswith(a, "ssm:") && a != "*"]) if s.Effect == "Allow"
    ])
    error_message = "The apply role allows an IAM action, an SSM action or every action."
  }
}

run "the_budget_alarm_is_five_dollars_a_month_to_its_topic" {
  command = plan

  assert {
    condition     = aws_budgets_budget.monthly.limit_amount == "5" && aws_budgets_budget.monthly.limit_unit == "USD" && aws_budgets_budget.monthly.time_unit == "MONTHLY" && aws_budgets_budget.monthly.budget_type == "COST"
    error_message = "The budget alarm is not 5 US dollars of cost a month."
  }

  assert {
    condition = jsonencode([for n in aws_budgets_budget.monthly.notification : {
      comparison_operator = n.comparison_operator
      threshold           = n.threshold
      threshold_type      = n.threshold_type
      notification_type   = n.notification_type
      topics              = n.subscriber_sns_topic_arns
      emails              = n.subscriber_email_addresses
      }]) == jsonencode([{
      comparison_operator = "GREATER_THAN"
      threshold           = 100
      threshold_type      = "PERCENTAGE"
      notification_type   = "ACTUAL"
      topics              = ["arn:aws:sns:us-east-1:000000000000:ithildin-test-alarms"]
      emails              = null
    }])
    error_message = "The budget alarm does not notify its topic, and only its topic, when actual spend passes the whole budget."
  }

  assert {
    condition = jsondecode(aws_sns_topic_policy.alarms.policy).Statement == [{
      Sid       = "AdmitOnlyThisAccountsBudgets"
      Effect    = "Allow"
      Principal = { Service = "budgets.amazonaws.com" }
      Action    = "SNS:Publish"
      Resource  = aws_sns_topic.alarms.arn
      Condition = { StringEquals = { "aws:SourceAccount" = "000000000000" } }
    }]
    error_message = "The alarm topic admits more than this account's budgets publishing to it."
  }

  assert {
    condition     = aws_sns_topic.alarms.name == "ithildin-test-alarms"
    error_message = "The test account's alarm topic is not named ithildin-test-alarms, the name the docs give."
  }

  assert {
    condition     = aws_sns_topic_policy.alarms.arn == "arn:aws:sns:us-east-1:000000000000:ithildin-test-alarms"
    error_message = "The alarm topic's policy is attached to another topic."
  }
}
