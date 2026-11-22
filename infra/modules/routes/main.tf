# For each route it is given: a queue, its dead-letter queue, an EventBridge rule on
# the default bus that matches the bucket's Object Created events by the route's key
# pattern, and a queue policy that admits only that rule. A route that leaves the list
# takes its rule and queues with it on the next apply.
#
# The test environment calls it with infra/test/routes.json alone, which #81's
# generator writes. On shadow, paper and live rules only grow, as design.md's "Rules
# only grow, and a release checks them" says, so a caller there passes every route any
# earlier release named, and never removes one.
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
  description = "How long each queue keeps a message."
  type        = number
  nullable    = false
}

# On a standard queue a message keeps its first enqueue time when it moves to the
# dead-letter queue, so a dead-letter queue that alarms on its depth keeps messages
# longer than its source.
variable "dead_letter_retention_seconds" {
  description = "How long each dead-letter queue keeps a message. At least message_retention_seconds."
  type        = number
  nullable    = false

  validation {
    condition     = var.dead_letter_retention_seconds >= var.message_retention_seconds
    error_message = "A dead-letter queue must keep messages at least as long as its source queue."
  }
}

locals {
  routes = { for r in var.routes : r.reader => r }
}

resource "aws_sqs_queue" "dead_letter" {
  for_each = local.routes

  name                      = "${var.name_prefix}-${each.key}-dlq"
  message_retention_seconds = var.dead_letter_retention_seconds
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

output "queue_settings" {
  description = "Each route's queue settings and queue policy, read from the resources, by reader, which the tests read."
  value = {
    for reader in keys(local.routes) : reader => {
      retention             = aws_sqs_queue.this[reader].message_retention_seconds
      dead_letter_retention = aws_sqs_queue.dead_letter[reader].message_retention_seconds
      visibility_timeout    = aws_sqs_queue.this[reader].visibility_timeout_seconds
      sse                   = aws_sqs_queue.this[reader].sqs_managed_sse_enabled
      dead_letter_sse       = aws_sqs_queue.dead_letter[reader].sqs_managed_sse_enabled
      max_receive_count     = jsondecode(aws_sqs_queue.this[reader].redrive_policy).maxReceiveCount
      dead_letter_target    = jsondecode(aws_sqs_queue.this[reader].redrive_policy).deadLetterTargetArn
      rule_target           = aws_cloudwatch_event_target.this[reader].arn
      queue_arn             = aws_sqs_queue.this[reader].arn
      queue_policy          = jsondecode(aws_sqs_queue_policy.this[reader].policy)
      policy_queue_url      = aws_sqs_queue_policy.this[reader].queue_url
      redrive_allow         = jsondecode(aws_sqs_queue_redrive_allow_policy.dead_letter[reader].redrive_allow_policy)
      redrive_allow_queue   = aws_sqs_queue_redrive_allow_policy.dead_letter[reader].queue_url
    }
  }
}

output "event_patterns" {
  description = "Each route's EventBridge pattern, by reader, which the tests read."
  value       = { for reader, rule in aws_cloudwatch_event_rule.this : reader => rule.event_pattern }
}
