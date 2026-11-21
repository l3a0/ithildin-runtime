# For each route in a routes file: a queue, its dead-letter queue, an EventBridge rule
# on the default bus that matches the bucket's Object Created events by the route's key
# pattern, and a queue policy that admits only that rule. Every environment calls this
# module with the routes file its release carries, and the test environment calls it
# with infra/test/routes.json, which #81's generator writes.
#
# Queues use SSE-SQS, SQS's managed encryption. EventBridge's troubleshooting page says
# an encrypted target queue needs a customer-managed KMS key, and does not name SSE-SQS
# either way, so the first delivery check is what shows it works.

terraform {
  required_version = "~> 1.13"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }
}

variable "name_prefix" {
  description = "The prefix every queue and rule name starts with, such as ithildin-test."
  type        = string
  nullable    = false
}

variable "bucket" {
  description = "The bucket whose Object Created events the rules match."
  type        = string
  nullable    = false
}

variable "routes" {
  description = "The routes, as the routes file lists them."
  type = list(object({
    reader                     = string
    pattern                    = any
    visibility_timeout_seconds = number
    max_receive_count          = number
  }))
  nullable = false

  validation {
    condition     = alltrue([for r in var.routes : can(regex("^[a-z0-9][a-z0-9-]{0,38}$", r.reader))])
    error_message = "Each route's reader is 1 to 39 lowercase letters, digits and hyphens."
  }

  validation {
    condition     = length(distinct([for r in var.routes : r.reader])) == length(var.routes)
    error_message = "Two routes name the same reader."
  }
}

variable "message_retention_seconds" {
  description = "How long each queue and dead-letter queue keeps a message."
  type        = number
  nullable    = false
}

locals {
  routes = { for r in var.routes : r.reader => r }
}

resource "aws_sqs_queue" "dead_letter" {
  for_each = local.routes

  name                      = "${var.name_prefix}-${each.key}-dlq"
  message_retention_seconds = var.message_retention_seconds
  sqs_managed_sse_enabled   = true
}

resource "aws_sqs_queue" "this" {
  for_each = local.routes

  name                       = "${var.name_prefix}-${each.key}"
  message_retention_seconds  = var.message_retention_seconds
  visibility_timeout_seconds = each.value.visibility_timeout_seconds
  sqs_managed_sse_enabled    = true

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.dead_letter[each.key].arn
    maxReceiveCount     = each.value.max_receive_count
  })
}

resource "aws_sqs_queue_redrive_allow_policy" "dead_letter" {
  for_each = local.routes

  queue_url = aws_sqs_queue.dead_letter[each.key].id
  redrive_allow_policy = jsonencode({
    redrivePermission = "byQueue"
    sourceQueueArns   = [aws_sqs_queue.this[each.key].arn]
  })
}

resource "aws_cloudwatch_event_rule" "this" {
  for_each = local.routes

  name = "${var.name_prefix}-${each.key}"
  event_pattern = jsonencode({
    source        = ["aws.s3"]
    "detail-type" = ["Object Created"]
    detail = {
      bucket = { name = [var.bucket] }
      object = { key = each.value.pattern }
    }
  })
}

resource "aws_cloudwatch_event_target" "this" {
  for_each = local.routes

  rule = aws_cloudwatch_event_rule.this[each.key].name
  arn  = aws_sqs_queue.this[each.key].arn
}

resource "aws_sqs_queue_policy" "this" {
  for_each = local.routes

  queue_url = aws_sqs_queue.this[each.key].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "AdmitOnlyThisRoutesRule"
      Effect    = "Allow"
      Principal = { Service = "events.amazonaws.com" }
      Action    = "sqs:SendMessage"
      Resource  = aws_sqs_queue.this[each.key].arn
      Condition = { ArnEquals = { "aws:SourceArn" = aws_cloudwatch_event_rule.this[each.key].arn } }
    }]
  })
}

output "queues" {
  description = "Each route's queue and dead-letter queue name, by reader."
  value = {
    for reader in keys(local.routes) : reader => {
      queue       = aws_sqs_queue.this[reader].name
      dead_letter = aws_sqs_queue.dead_letter[reader].name
    }
  }
}

output "event_patterns" {
  description = "Each route's EventBridge pattern, by reader, which the tests read."
  value       = { for reader, rule in aws_cloudwatch_event_rule.this : reader => rule.event_pattern }
}
