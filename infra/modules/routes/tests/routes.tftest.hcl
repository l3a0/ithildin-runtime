# What the module builds from a queues list and a rules list, and what it refuses: a
# rule whose reader has no queue, a queue no rule feeds, a rule name EventBridge would
# refuse, a pattern for a kind no route hears, another source or another bucket, and
# two entries that would make one resource. Each run
# that expects a refusal passes inputs that break that one check alone.

mock_provider "aws" {
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

override_resource {
  target = aws_sqs_queue.this["book"]
  values = {
    arn = "arn:aws:sqs:us-east-1:000000000000:example-book"
    id  = "https://sqs.us-east-1.amazonaws.com/000000000000/example-book"
  }
}

override_resource {
  target = aws_cloudwatch_event_rule.this["book-000000000001"]
  values = {
    arn = "arn:aws:events:us-east-1:000000000000:rule/example-book-000000000001"
  }
}

override_resource {
  target = aws_cloudwatch_event_rule.this["book-000000000002"]
  values = {
    arn = "arn:aws:events:us-east-1:000000000000:rule/example-book-000000000002"
  }
}

variables {
  name_prefix                   = "example"
  bucket                        = "example-bucket"
  message_retention_seconds     = 3600
  dead_letter_retention_seconds = 3600
  queues = [
    { reader = "book", visibility_timeout_seconds = 30, max_receive_count = 3 },
    { reader = "smoke", visibility_timeout_seconds = 30, max_receive_count = 3 },
  ]
  rules = [
    { name = "book-000000000002", reader = "book", pattern = { source = ["aws.s3"], "detail-type" = ["Object Deleted"], detail = { bucket = { name = ["example-bucket"] }, object = { key = [{ wildcard = "b/*" }] } } } },
    { name = "book-000000000001", reader = "book", pattern = { source = ["aws.s3"], "detail-type" = ["Object Created"], detail = { bucket = { name = ["example-bucket"] }, object = { key = [{ wildcard = "a/*" }] } } } },
    { name = "smoke-000000000000", reader = "smoke", pattern = { source = ["aws.s3"], "detail-type" = ["Object Created"], detail = { bucket = { name = ["example-bucket"] }, object = { key = [{ wildcard = "s/*" }] } } } },
  ]
}

run "a_reader_with_several_rules_gets_one_queue_that_admits_each_of_them" {
  command = plan

  assert {
    condition     = keys(output.queues) == ["book", "smoke"]
    error_message = "The module did not make one queue per entry of queues."
  }

  assert {
    condition     = keys(output.rules) == ["book-000000000001", "book-000000000002", "smoke-000000000000"]
    error_message = "The module did not make one rule per entry of rules."
  }

  assert {
    condition = output.queue_settings["book"].queue_policy.Statement[0].Condition.ArnEquals["aws:SourceArn"] == [
      "arn:aws:events:us-east-1:000000000000:rule/example-book-000000000001",
      "arn:aws:events:us-east-1:000000000000:rule/example-book-000000000002",
    ]
    error_message = "The book queue's policy does not admit exactly book's two rules."
  }

  assert {
    condition = (
      output.rules["book-000000000001"].target == "arn:aws:sqs:us-east-1:000000000000:example-book"
      && output.rules["book-000000000002"].target == "arn:aws:sqs:us-east-1:000000000000:example-book"
      && output.rules["smoke-000000000000"].target == "arn:aws:sqs:us-east-1:000000000000:mock"
    )
    error_message = "A rule is not aimed at its own reader's queue."
  }

  assert {
    condition = jsonencode(output.rules["book-000000000002"].event_pattern) == jsonencode({
      source        = ["aws.s3"]
      "detail-type" = ["Object Deleted"]
      detail        = { bucket = { name = ["example-bucket"] }, object = { key = [{ wildcard = "b/*" }] } }
    })
    error_message = "A rule does not carry its entry's pattern as written."
  }

  assert {
    condition     = output.rules["book-000000000001"].name == "example-book-000000000001"
    error_message = "A rule is not named the prefix, a hyphen and its entry's name."
  }
}

run "a_rule_whose_reader_has_no_queue_is_refused" {
  command = plan

  variables {
    queues = [{ reader = "smoke", visibility_timeout_seconds = 30, max_receive_count = 3 }]
    rules = [
      { name = "smoke-000000000000", reader = "smoke", pattern = { source = ["aws.s3"], "detail-type" = ["Object Created"], detail = { bucket = { name = ["example-bucket"] }, object = { key = [{ wildcard = "s/*" }] } } } },
      { name = "risk-000000000000", reader = "risk", pattern = { source = ["aws.s3"], "detail-type" = ["Object Created"], detail = { bucket = { name = ["example-bucket"] }, object = { key = [{ wildcard = "r/*" }] } } } },
    ]
  }

  expect_failures = [var.rules]
}

run "a_rule_name_over_64_characters_is_refused" {
  command = plan

  # "example-" is 8 characters, so a name of 57 makes 65.
  variables {
    queues = [{ reader = "smoke", visibility_timeout_seconds = 30, max_receive_count = 3 }]
    rules  = [{ name = "smoke-${join("", [for i in range(51) : "a"])}", reader = "smoke", pattern = { source = ["aws.s3"], "detail-type" = ["Object Created"], detail = { bucket = { name = ["example-bucket"] }, object = { key = [{ wildcard = "s/*" }] } } } }]
  }

  expect_failures = [var.rules]
}

run "a_rule_name_of_64_characters_is_taken" {
  command = plan

  variables {
    queues = [{ reader = "smoke", visibility_timeout_seconds = 30, max_receive_count = 3 }]
    rules  = [{ name = "smoke-${join("", [for i in range(50) : "a"])}", reader = "smoke", pattern = { source = ["aws.s3"], "detail-type" = ["Object Created"], detail = { bucket = { name = ["example-bucket"] }, object = { key = [{ wildcard = "s/*" }] } } } }]
  }
}

run "a_kind_no_route_hears_is_refused" {
  command = plan

  variables {
    queues = [{ reader = "smoke", visibility_timeout_seconds = 30, max_receive_count = 3 }]
    rules  = [{ name = "smoke-000000000000", reader = "smoke", pattern = { source = ["aws.s3"], "detail-type" = ["other"], detail = { bucket = { name = ["example-bucket"] }, object = { key = [{ wildcard = "s/*" }] } } } }]
  }

  expect_failures = [var.rules]
}

run "two_rules_with_one_name_are_refused" {
  command = plan

  variables {
    queues = [{ reader = "smoke", visibility_timeout_seconds = 30, max_receive_count = 3 }]
    rules = [
      { name = "smoke-000000000000", reader = "smoke", pattern = { source = ["aws.s3"], "detail-type" = ["Object Created"], detail = { bucket = { name = ["example-bucket"] }, object = { key = [{ wildcard = "s/*" }] } } } },
      { name = "smoke-000000000000", reader = "smoke", pattern = { source = ["aws.s3"], "detail-type" = ["Object Created"], detail = { bucket = { name = ["example-bucket"] }, object = { key = [{ wildcard = "t/*" }] } } } },
    ]
  }

  expect_failures = [var.rules]
}

run "two_queues_with_one_reader_are_refused" {
  command = plan

  variables {
    queues = [
      { reader = "smoke", visibility_timeout_seconds = 30, max_receive_count = 3 },
      { reader = "smoke", visibility_timeout_seconds = 31, max_receive_count = 3 },
    ]
    rules = [{ name = "smoke-000000000000", reader = "smoke", pattern = { source = ["aws.s3"], "detail-type" = ["Object Created"], detail = { bucket = { name = ["example-bucket"] }, object = { key = [{ wildcard = "s/*" }] } } } }]
  }

  expect_failures = [var.queues]
}

run "a_queue_no_rule_feeds_is_refused" {
  command = plan

  variables {
    rules = [{ name = "smoke-000000000000", reader = "smoke", pattern = { source = ["aws.s3"], "detail-type" = ["Object Created"], detail = { bucket = { name = ["example-bucket"] }, object = { key = [{ wildcard = "s/*" }] } } } }]
  }

  expect_failures = [var.rules]
}

run "a_rule_name_with_a_character_eventbridge_refuses_is_refused" {
  command = plan

  variables {
    queues = [{ reader = "smoke", visibility_timeout_seconds = 30, max_receive_count = 3 }]
    rules  = [{ name = "smoke/000000000000", reader = "smoke", pattern = { source = ["aws.s3"], "detail-type" = ["Object Created"], detail = { bucket = { name = ["example-bucket"] }, object = { key = [{ wildcard = "s/*" }] } } } }]
  }

  expect_failures = [var.rules]
}

run "a_pattern_on_another_bucket_is_refused" {
  command = plan

  variables {
    queues = [{ reader = "smoke", visibility_timeout_seconds = 30, max_receive_count = 3 }]
    rules  = [{ name = "smoke-000000000000", reader = "smoke", pattern = { source = ["aws.s3"], "detail-type" = ["Object Created"], detail = { bucket = { name = ["other-bucket"] }, object = { key = [{ wildcard = "s/*" }] } } } }]
  }

  expect_failures = [var.rules]
}

run "a_pattern_on_another_source_is_refused" {
  command = plan

  variables {
    queues = [{ reader = "smoke", visibility_timeout_seconds = 30, max_receive_count = 3 }]
    rules  = [{ name = "smoke-000000000000", reader = "smoke", pattern = { source = ["custom.s3"], "detail-type" = ["Object Created"], detail = { bucket = { name = ["example-bucket"] }, object = { key = [{ wildcard = "s/*" }] } } } }]
  }

  expect_failures = [var.rules]
}
