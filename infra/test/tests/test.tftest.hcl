# Plan-mode checks on the test bucket's policy and lifecycle and on the routes, against
# literals. Every run plans, because test cleanup cannot destroy a prevent_destroy
# resource.

mock_provider "aws" {
  mock_data "aws_caller_identity" {
    defaults = {
      account_id = "000000000000"
    }
  }

  # The provider checks that a target's and a queue policy's ARNs are ARNs, and the
  # mock's random strings are not.
  mock_resource "aws_sqs_queue" {
    defaults = {
      arn = "arn:aws:sqs:us-east-1:000000000000:mock"
      id  = "https://sqs.us-east-1.amazonaws.com/000000000000/mock"
    }
  }

  mock_resource "aws_cloudwatch_event_rule" {
    defaults = {
      arn = "arn:aws:events:us-east-1:000000000000:rule/mock"
    }
  }
}

variables {}

run "the_bucket_policy_holds_the_refusals_production_uses" {
  command = plan

  # Each statement the docs' table of refusals asks for, by its Sid, written out.
  assert {
    condition = [for s in jsondecode(module.bucket.policy).Statement : s if s.Sid == "RefuseCreateOnlyWritesWithoutIfNoneMatch"] == [{
      Sid       = "RefuseCreateOnlyWritesWithoutIfNoneMatch"
      Effect    = "Deny"
      Principal = "*"
      Action    = "s3:PutObject"
      Resource = [
        "arn:aws:s3:::ithildin-runtime-test/runs/*",
        "arn:aws:s3:::ithildin-runtime-test/journal/*",
        "arn:aws:s3:::ithildin-runtime-test/sim/*",
      ]
      Condition = { Null = { "s3:if-none-match" = "true" } }
    }]
    error_message = "The policy does not refuse a write on runs/, journal/ and sim/ without If-None-Match."
  }

  assert {
    condition = [for s in jsondecode(module.bucket.policy).Statement : s if s.Sid == "RefuseRetentionPastTheCap"] == [{
      Sid       = "RefuseRetentionPastTheCap"
      Effect    = "Deny"
      Principal = "*"
      Action    = ["s3:PutObject", "s3:PutObjectRetention"]
      Resource  = ["arn:aws:s3:::ithildin-runtime-test/*"]
      Condition = { NumericGreaterThan = { "s3:object-lock-remaining-retention-days" = "1" } }
    }]
    error_message = "The test bucket does not cap retention at one day."
  }

  assert {
    condition = [for s in jsondecode(module.bucket.policy).Statement : s.Condition if s.Sid == "RefuseUnlistedPrincipals"] == [{
      StringNotLike = { "aws:PrincipalArn" = [
        "arn:aws:iam::000000000000:role/ithildin-test-ci",
        "arn:aws:iam::000000000000:role/ithildin-test-apply",
        "arn:aws:iam::000000000000:role/OrganizationAccountAccessRole",
      ] }
    }]
    error_message = "The bucket admits a principal other than the test role, the apply role and the owner's role."
  }

  # The test role is the test bucket's runtime principal, so every refusal the docs
  # give the runtime's roles binds the suites.
  assert {
    condition = alltrue([
      for sid in ["RefuseRuntimeLeaseWritesWithNeitherCondition", "RefuseRuntimeWritesWithoutIfNoneMatch", "RefuseRuntimeDeletes"] :
      [for s in jsondecode(module.bucket.policy).Statement : s.Condition.ArnLike["aws:PrincipalArn"] if s.Sid == sid] == [["arn:aws:iam::000000000000:role/ithildin-test-ci"]]
    ])
    error_message = "A refusal of the runtime's principals does not bind the test role."
  }

  assert {
    condition = [for s in jsondecode(module.bucket.policy).Statement : s.Condition.Null if s.Sid == "RefuseRuntimeLeaseWritesWithNeitherCondition"] == [{
      "s3:if-none-match" = "true"
      "s3:if-match"      = "true"
    }]
    error_message = "A lease write that carries neither conditional header is not refused."
  }

  assert {
    condition = [for s in jsondecode(module.bucket.policy).Statement : s.Condition if s.Sid == "RefuseSettingsChanges"] == [{
      StringNotLike = { "aws:PrincipalArn" = [
        "arn:aws:iam::000000000000:role/ithildin-test-apply",
        "arn:aws:iam::000000000000:role/OrganizationAccountAccessRole",
      ] }
    }]
    error_message = "A principal other than the apply role and the owner's role may change the test bucket's settings."
  }

  assert {
    condition = toset([for s in jsondecode(module.bucket.policy).Statement : s.Sid]) == toset([
      "RefuseUnlistedPrincipals",
      "RefuseCreateOnlyWritesWithoutIfNoneMatch",
      "RefuseCreateOnlyDeletes",
      "RefuseLeaseDeletes",
      "RefuseRuntimeLeaseWritesWithNeitherCondition",
      "RefuseRuntimeWritesWithoutIfNoneMatch",
      "RefuseRuntimeDeletes",
      "RefusePeoplesAccountLeaseWritesWithoutIfMatch",
      "RefuseSwitchDeletes",
      "RefuseLossStopDeletes",
      "RefuseOffSwitchDeletes",
      "RefuseSettingsChanges",
      "RefuseRetentionPastTheCap",
    ])
    error_message = "The test bucket's policy gained or lost a statement."
  }

  assert {
    condition     = alltrue([for s in jsondecode(module.bucket.policy).Statement : s.Effect == "Deny"])
    error_message = "The bucket policy allows something. Every grant belongs on a role."
  }
}

run "the_test_bucket_empties_itself" {
  command = plan

  assert {
    condition     = module.bucket.name == "ithildin-runtime-test"
    error_message = "The test bucket's name moved."
  }

  assert {
    condition = jsonencode([for r in module.bucket.lifecycle_rules : {
      id         = r.id
      current    = r.expire_current_days
      noncurrent = r.expire_noncurrent_days
      uploads    = r.abort_incomplete_upload
      markers    = r.expire_old_delete_marker
      prefix     = r.prefix
      }]) == jsonencode([
      { id = "expire-everything", current = 1, noncurrent = 1, uploads = 1, markers = false, prefix = "" },
      { id = "remove-old-delete-markers", current = null, noncurrent = null, uploads = null, markers = true, prefix = "" },
    ])
    error_message = "The test bucket does not expire everything after a day."
  }
}

run "each_route_matches_its_own_keys_on_the_test_bucket" {
  command = plan

  assert {
    condition = jsondecode(module.routes.event_patterns["smoke"]) == {
      source        = ["aws.s3"]
      "detail-type" = ["Object Created"]
      detail = {
        bucket = { name = ["ithildin-runtime-test"] }
        object = { key = [{ wildcard = "runs/ci/*/smoke/*" }] }
      }
    }
    error_message = "The smoke route's rule does not match Object Created events for its keys on the test bucket."
  }

  assert {
    condition     = module.routes.queues["smoke"] == { queue = "ithildin-test-smoke", dead_letter = "ithildin-test-smoke-dlq" }
    error_message = "The smoke route's queues are not named ithildin-test-smoke and ithildin-test-smoke-dlq."
  }
}
