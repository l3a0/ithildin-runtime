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

# Each queue, dead-letter queue and rule gets an ARN of its own, named as AWS would name
# it, so a rule aimed at another reader's queue, a redrive to another dead-letter queue,
# or a policy admitting another reader's rule fails the checks below. A rule the routes
# file adds needs an override here too, or it keeps the mock's ARN and those checks
# fail.
override_resource {
  target = module.routes.aws_sqs_queue.this["alpha"]
  values = {
    arn = "arn:aws:sqs:us-east-1:000000000000:ithildin-test-alpha"
    id  = "https://sqs.us-east-1.amazonaws.com/000000000000/ithildin-test-alpha"
  }
}

override_resource {
  target = module.routes.aws_sqs_queue.dead_letter["alpha"]
  values = {
    arn = "arn:aws:sqs:us-east-1:000000000000:ithildin-test-alpha-dlq"
    id  = "https://sqs.us-east-1.amazonaws.com/000000000000/ithildin-test-alpha-dlq"
  }
}

override_resource {
  target = module.routes.aws_sqs_queue.this["book"]
  values = {
    arn = "arn:aws:sqs:us-east-1:000000000000:ithildin-test-book"
    id  = "https://sqs.us-east-1.amazonaws.com/000000000000/ithildin-test-book"
  }
}

override_resource {
  target = module.routes.aws_sqs_queue.dead_letter["book"]
  values = {
    arn = "arn:aws:sqs:us-east-1:000000000000:ithildin-test-book-dlq"
    id  = "https://sqs.us-east-1.amazonaws.com/000000000000/ithildin-test-book-dlq"
  }
}

override_resource {
  target = module.routes.aws_sqs_queue.this["risk"]
  values = {
    arn = "arn:aws:sqs:us-east-1:000000000000:ithildin-test-risk"
    id  = "https://sqs.us-east-1.amazonaws.com/000000000000/ithildin-test-risk"
  }
}

override_resource {
  target = module.routes.aws_sqs_queue.dead_letter["risk"]
  values = {
    arn = "arn:aws:sqs:us-east-1:000000000000:ithildin-test-risk-dlq"
    id  = "https://sqs.us-east-1.amazonaws.com/000000000000/ithildin-test-risk-dlq"
  }
}

override_resource {
  target = module.routes.aws_sqs_queue.this["smoke"]
  values = {
    arn = "arn:aws:sqs:us-east-1:000000000000:ithildin-test-smoke"
    id  = "https://sqs.us-east-1.amazonaws.com/000000000000/ithildin-test-smoke"
  }
}

override_resource {
  target = module.routes.aws_sqs_queue.dead_letter["smoke"]
  values = {
    arn = "arn:aws:sqs:us-east-1:000000000000:ithildin-test-smoke-dlq"
    id  = "https://sqs.us-east-1.amazonaws.com/000000000000/ithildin-test-smoke-dlq"
  }
}

override_resource {
  target = module.routes.aws_cloudwatch_event_rule.this["alpha-29888b8cd48e"]
  values = {
    arn = "arn:aws:events:us-east-1:000000000000:rule/ithildin-test-alpha-29888b8cd48e"
  }
}

override_resource {
  target = module.routes.aws_cloudwatch_event_rule.this["book-54eba3127b27"]
  values = {
    arn = "arn:aws:events:us-east-1:000000000000:rule/ithildin-test-book-54eba3127b27"
  }
}

override_resource {
  target = module.routes.aws_cloudwatch_event_rule.this["book-ab4e249d5889"]
  values = {
    arn = "arn:aws:events:us-east-1:000000000000:rule/ithildin-test-book-ab4e249d5889"
  }
}

override_resource {
  target = module.routes.aws_cloudwatch_event_rule.this["book-b2ab60123b9f"]
  values = {
    arn = "arn:aws:events:us-east-1:000000000000:rule/ithildin-test-book-b2ab60123b9f"
  }
}

override_resource {
  target = module.routes.aws_cloudwatch_event_rule.this["book-8ad2112f06aa"]
  values = {
    arn = "arn:aws:events:us-east-1:000000000000:rule/ithildin-test-book-8ad2112f06aa"
  }
}

override_resource {
  target = module.routes.aws_cloudwatch_event_rule.this["risk-5d65847bc100"]
  values = {
    arn = "arn:aws:events:us-east-1:000000000000:rule/ithildin-test-risk-5d65847bc100"
  }
}

override_resource {
  target = module.routes.aws_cloudwatch_event_rule.this["smoke-483ea2f8cd44"]
  values = {
    arn = "arn:aws:events:us-east-1:000000000000:rule/ithildin-test-smoke-483ea2f8cd44"
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

run "each_rule_matches_its_own_keys_on_the_test_bucket_and_targets_its_readers_queue" {
  command = plan

  assert {
    condition     = jsonencode(keys(module.routes.rules)) == jsonencode(sort([for r in local.routes_file.rules : r.name]))
    error_message = "The module did not make one rule for each entry of routes.json's rules."
  }

  assert {
    condition = alltrue([for r in local.routes_file.rules : jsonencode(module.routes.rules[r.name]) == jsonencode({
      name          = "ithildin-test-${r.name}"
      arn           = "arn:aws:events:us-east-1:000000000000:rule/ithildin-test-${r.name}"
      target_rule   = "ithildin-test-${r.name}"
      target        = "arn:aws:sqs:us-east-1:000000000000:ithildin-test-${r.reader}"
      event_pattern = r.pattern
    })])
    error_message = "A rule is not named ithildin-test- and its entry's name, does not carry its entry's pattern as written, or is not aimed at its reader's queue."
  }

  # The smoke check writes under this pattern, so its whole rule is pinned here as a
  # literal rather than read back from the file.
  assert {
    condition = jsonencode(module.routes.rules["smoke-483ea2f8cd44"].event_pattern) == jsonencode({
      source        = ["aws.s3"]
      "detail-type" = ["Object Created"]
      detail = {
        bucket = { name = ["ithildin-runtime-test"] }
        object = { key = [{ wildcard = "runs/ci/*/smoke/*" }] }
      }
    })
    error_message = "The smoke route's rule does not match S3's Object Created events for the keys the smoke check writes on the test bucket."
  }
}

run "each_reader_has_its_queues_with_the_routes_files_settings_and_admits_only_its_rules" {
  command = plan

  assert {
    condition = module.routes.queues == {
      for q in local.routes_file.queues : q.reader => {
        queue       = "ithildin-test-${q.reader}"
        dead_letter = "ithildin-test-${q.reader}-dlq"
      }
    }
    error_message = "A reader's queues are not named ithildin-test-<reader> and ithildin-test-<reader>-dlq, or a reader in routes.json has none."
  }

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
      for q in local.routes_file.queues : q.reader => {
        retention             = 3600
        dead_letter_retention = 3600
        visibility_timeout    = q.visibility_timeout_seconds
        sse                   = true
        dead_letter_sse       = true
        max_receive_count     = q.max_receive_count
      }
    }
    error_message = "A reader's queues do not carry routes.json's visibility timeout and receive count, one hour's retention, and SSE-SQS."
  }

  # The rollback drill in docs/what-a-person-runs.md changes this value, so the drill's
  # pull request changes it here too.
  assert {
    condition     = module.routes.queue_settings["smoke"].visibility_timeout == 30
    error_message = "The smoke queue's visibility timeout is not 30 seconds."
  }

  assert {
    condition = alltrue([for q in local.routes_file.queues :
      module.routes.queue_settings[q.reader].dead_letter_target == "arn:aws:sqs:us-east-1:000000000000:ithildin-test-${q.reader}-dlq"
    ])
    error_message = "A reader's queue does not redrive to its own dead-letter queue."
  }

  assert {
    condition = alltrue([for q in local.routes_file.queues :
      jsonencode(module.routes.queue_settings[q.reader].queue_policy.Statement) == jsonencode([{
        Sid       = "AdmitOnlyThisReadersRules"
        Effect    = "Allow"
        Principal = { Service = "events.amazonaws.com" }
        Action    = "sqs:SendMessage"
        Resource  = "arn:aws:sqs:us-east-1:000000000000:ithildin-test-${q.reader}"
        Condition = { ArnEquals = { "aws:SourceArn" = sort([
          for r in local.routes_file.rules : "arn:aws:events:us-east-1:000000000000:rule/ithildin-test-${r.name}" if r.reader == q.reader
        ]) } }
      }])
    ])
    error_message = "A reader's queue policy admits more than EventBridge sending for that reader's own rules."
  }

  assert {
    condition = alltrue([for q in local.routes_file.queues :
      module.routes.queue_settings[q.reader].policy_queue_url == "https://sqs.us-east-1.amazonaws.com/000000000000/ithildin-test-${q.reader}"
    ])
    error_message = "A reader's queue policy is attached to another queue."
  }

  assert {
    condition = alltrue([for q in local.routes_file.queues :
      module.routes.queue_settings[q.reader].redrive_allow_queue == "https://sqs.us-east-1.amazonaws.com/000000000000/ithildin-test-${q.reader}-dlq"
      && module.routes.queue_settings[q.reader].redrive_allow == { redrivePermission = "byQueue", sourceQueueArns = ["arn:aws:sqs:us-east-1:000000000000:ithildin-test-${q.reader}"] }
    ])
    error_message = "A reader's dead-letter queue does not admit redrives from that reader's queue alone."
  }
}
