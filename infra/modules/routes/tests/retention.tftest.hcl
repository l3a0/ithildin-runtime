# Each queue keeps messages for its own input's time, and a dead-letter queue keeps them
# at least as long as its source, because a moved message keeps its first enqueue time.

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

variables {
  name_prefix = "example"
  bucket      = "example-bucket"
  queues = [{
    reader                     = "smoke"
    visibility_timeout_seconds = 30
    max_receive_count          = 3
  }]
  rules = [{
    name    = "smoke-000000000000"
    reader  = "smoke"
    pattern = { source = ["aws.s3"], "detail-type" = ["Object Created"], detail = { bucket = { name = ["example-bucket"] }, object = { key = [{ prefix = "runs/" }] } } }
  }]
}

run "each_queue_keeps_messages_for_its_own_time" {
  command = plan

  variables {
    message_retention_seconds     = 3600
    dead_letter_retention_seconds = 7200
  }

  assert {
    condition     = output.queue_settings["smoke"].retention == 3600 && output.queue_settings["smoke"].dead_letter_retention == 7200
    error_message = "A queue or its dead-letter queue does not keep messages for the time its own input gives."
  }
}

run "a_dead_letter_queue_may_keep_messages_as_long_as_its_source" {
  command = plan

  variables {
    message_retention_seconds     = 3600
    dead_letter_retention_seconds = 3600
  }
}

run "a_dead_letter_queue_that_keeps_messages_less_long_is_refused" {
  command = plan

  variables {
    message_retention_seconds     = 3600
    dead_letter_retention_seconds = 3599
  }

  expect_failures = [var.dead_letter_retention_seconds]
}
