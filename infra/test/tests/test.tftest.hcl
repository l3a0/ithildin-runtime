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

# The dead-letter queue gets an ARN of its own, so a rule aimed at it fails the check
# that the rule targets its queue.
override_resource {
  target = module.routes.aws_sqs_queue.dead_letter["smoke"]
  values = {
    arn = "arn:aws:sqs:us-east-1:000000000000:mock-dlq"
    id  = "https://sqs.us-east-1.amazonaws.com/000000000000/mock-dlq"
  }
}

variables {}

run "the_bucket_policy_is_exactly_the_reviewed_document" {
  command = plan

  # tests/test-bucket-policy.json is the test bucket's whole policy, written out and
  # read against 07-deployment.md's table of refusals. Every statement is compared,
  # so a changed action, resource, condition or principal fails here.
  assert {
    condition     = jsondecode(module.bucket.policy) == jsondecode(file("${path.module}/tests/test-bucket-policy.json"))
    error_message = "The test bucket's policy differs from tests/test-bucket-policy.json. Read the change against 07-deployment.md's table of refusals before updating the file."
  }

  assert {
    condition     = alltrue([for s in jsondecode(module.bucket.policy).Statement : s.Effect == "Deny"])
    error_message = "The bucket policy allows something. Every grant belongs on a role."
  }
}

run "the_test_bucket_carries_the_settings_production_uses" {
  command = plan

  assert {
    condition     = module.bucket.name == "ithildin-runtime-test"
    error_message = "The test bucket's name moved."
  }

  assert {
    condition = module.bucket.settings == {
      object_lock_enabled     = true
      versioning              = "Enabled"
      object_lock             = "Enabled"
      sse_algorithm           = "AES256"
      block_public_acls       = true
      block_public_policy     = true
      ignore_public_acls      = true
      restrict_public_buckets = true
      object_ownership        = "BucketOwnerEnforced"
      eventbridge             = true
    }
    error_message = "The test bucket lost versioning, Object Lock, S3's managed key, the public access block, owner-enforced ownership or its EventBridge notifications."
  }
}

run "the_test_bucket_empties_itself" {
  command = plan

  # A day count of 0 and a marker of false are how the provider shows an expiration
  # that sets neither.
  assert {
    condition = jsonencode(module.bucket.lifecycle_rules) == jsonencode([
      { id = "expire-everything", status = "Enabled", prefix = "", current = 1, marker = false, noncurrent = 1, uploads = 1 },
      { id = "remove-old-delete-markers", status = "Enabled", prefix = "", current = 0, marker = true, noncurrent = null, uploads = null },
    ])
    error_message = "The test bucket's lifecycle does not expire everything after a day."
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

run "each_route_takes_its_settings_from_the_routes_file_and_admits_only_its_rule" {
  command = plan

  assert {
    condition = {
      for k, v in module.routes.queue_settings : k => {
        retention             = v.retention
        dead_letter_retention = v.dead_letter_retention
        visibility_timeout    = v.visibility_timeout
        sse                   = v.sse
        dead_letter_sse       = v.dead_letter_sse
        max_receive_count     = v.max_receive_count
      }
      } == {
      smoke = {
        retention             = 3600
        dead_letter_retention = 3600
        visibility_timeout    = 30
        sse                   = true
        dead_letter_sse       = true
        max_receive_count     = 3
      }
    }
    error_message = "A route's queues do not carry routes.json's visibility timeout and receive count, one hour's retention, and SSE-SQS."
  }

  # The mock gives every queue the same ARN apart from the dead-letter queue's, so this
  # checks the shape: the rule sends to the queue, not the dead-letter queue, the queue
  # redrives to its dead-letter queue, and the queue's policy admits EventBridge only
  # for that rule.
  assert {
    condition     = module.routes.queue_settings["smoke"].rule_target == "arn:aws:sqs:us-east-1:000000000000:mock"
    error_message = "The smoke rule does not target the smoke queue."
  }

  assert {
    condition     = module.routes.queue_settings["smoke"].dead_letter_target == "arn:aws:sqs:us-east-1:000000000000:mock-dlq"
    error_message = "The smoke queue's redrive does not point at its dead-letter queue."
  }

  assert {
    condition = module.routes.queue_settings["smoke"].queue_policy.Statement == [{
      Sid       = "AdmitOnlyThisRoutesRule"
      Effect    = "Allow"
      Principal = { Service = "events.amazonaws.com" }
      Action    = "sqs:SendMessage"
      Resource  = "arn:aws:sqs:us-east-1:000000000000:mock"
      Condition = { ArnEquals = { "aws:SourceArn" = "arn:aws:events:us-east-1:000000000000:rule/mock" } }
    }]
    error_message = "The smoke queue's policy admits more than EventBridge sending for the smoke rule."
  }
}
