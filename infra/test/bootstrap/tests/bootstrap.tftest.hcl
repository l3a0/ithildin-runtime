# Plan-mode checks on the bootstrap's roles, against literals. Every run plans, because
# test cleanup cannot destroy a prevent_destroy resource.

mock_provider "aws" {
  mock_data "aws_caller_identity" {
    defaults = {
      account_id = "000000000000"
    }
  }
}

variables {
  state_bucket   = "example-state"
  budget_email   = "owner@example.com"
  adopt_existing = false
}

run "each_trust_names_one_job_on_main" {
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
    error_message = "The test role's trust is not exactly ci.yml on a push to main."
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

run "the_apply_role_holds_no_iam_and_no_broad_read" {
  command = plan

  assert {
    condition = alltrue([
      for s in jsondecode(aws_iam_role_policy.apply.policy).Statement :
      alltrue([for a in s.Action : !startswith(a, "iam:") && !startswith(a, "ssm:Get") && a != "*"]) if s.Effect == "Allow"
    ])
    error_message = "The apply role allows an IAM action, an SSM read or every action."
  }

  assert {
    condition = alltrue([
      for s in jsondecode(aws_iam_role_policy.apply.policy).Statement :
      alltrue([for r in s.Resource : r != "*"]) if s.Effect == "Allow"
    ])
    error_message = "The apply role allows an action on every resource."
  }

  assert {
    condition = [
      for s in jsondecode(aws_iam_role_policy.apply.policy).Statement : s.Resource if s.Sid == "ReadAndWriteInfraTestsStateAndLock"
      ] == [[
        "arn:aws:s3:::example-state/test/terraform.tfstate",
        "arn:aws:s3:::example-state/test/terraform.tfstate.tflock",
    ]]
    error_message = "The apply role may write state other than infra/test's."
  }
}

run "the_budget_alarm_is_five_dollars_a_month" {
  command = plan

  assert {
    condition     = aws_budgets_budget.monthly.limit_amount == "5" && aws_budgets_budget.monthly.limit_unit == "USD" && aws_budgets_budget.monthly.time_unit == "MONTHLY"
    error_message = "The budget alarm is not 5 US dollars a month."
  }
}
