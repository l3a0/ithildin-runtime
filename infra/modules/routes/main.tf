# For each queue it is given: the reader's queue, its dead-letter queue, and a queue
# policy that admits only the reader's own rules. For each rule: an EventBridge rule on
# the default bus that matches the bucket's events of the rule's kind by its key
# pattern, and a target aimed at its reader's queue. A rule leaves with its entry in
# `rules`, and a queue with its reader's entry in `queues`, on the next apply.
#
# The test environment calls it with infra/test/routes.json, which
# tests/support/routes_file.py writes from `routes_file` in runtime.loop. On shadow,
# paper and live rules only grow, as design.md's "Rules only grow, and a release checks
# them" says, so a caller there passes every rule any earlier release named, and never
# removes one. A rule's name changes whenever its pattern or its target does, so a
# changed route adds a rule rather than narrowing one.
#
# Queues use SSE-SQS, SQS's managed encryption. EventBridge's troubleshooting page says
# an encrypted target queue needs a customer-managed KMS key, and does not name SSE-SQS
# either way. The smoke check's delivery case showed EventBridge delivering S3's event
# to an SSE-SQS queue on main at 891df5c, in run 45030575106.

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
  description = "The bucket whose object events the rules match."
  type        = string
  nullable    = false
}

variable "queues" {
  description = "One entry per reader, as the routes file's `queues` list holds them."
  type = list(object({
    reader                     = string
    visibility_timeout_seconds = number
    max_receive_count          = number
  }))
  nullable = false

  validation {
    condition     = alltrue([for q in var.queues : can(regex("^[a-z0-9][a-z0-9-]{0,38}$", q.reader))])
    error_message = "Each queue's reader is 1 to 39 lowercase letters, digits and hyphens."
  }

  validation {
    condition     = length(distinct([for q in var.queues : q.reader])) == length(var.queues)
    error_message = "Two queues name the same reader."
  }
}

variable "rules" {
  description = "One entry per route, as the routes file's `rules` list holds them."
  type = list(object({
    name    = string
    reader  = string
    kind    = string
    pattern = any
  }))
  nullable = false

  validation {
    condition     = length(distinct([for r in var.rules : r.name])) == length(var.rules)
    error_message = "Two rules carry the same name."
  }

  validation {
    condition     = alltrue([for r in var.rules : contains(["Object Created", "Object Deleted"], r.kind)])
    error_message = "Each rule hears Object Created or Object Deleted, never anything else."
  }

  # EventBridge refuses a rule name over 64 characters, or one holding anything but
  # letters, digits, dots, hyphens and underscores.
  validation {
    condition     = alltrue([for r in var.rules : can(regex("^[.\\-_A-Za-z0-9]{1,64}$", "${var.name_prefix}-${r.name}"))])
    error_message = "A rule's full name, the prefix, a hyphen and its name, is over 64 characters or holds a character EventBridge refuses."
  }

  validation {
    condition     = alltrue([for r in var.rules : contains([for q in var.queues : q.reader], r.reader)])
    error_message = "A rule names a reader that has no queue."
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
  queues = { for q in var.queues : q.reader => q }
  rules  = { for r in var.rules : r.name => r }
}

resource "aws_sqs_queue" "dead_letter" {
  for_each = local.queues

  name                      = "${var.name_prefix}-${each.key}-dlq"
  message_retention_seconds = var.dead_letter_retention_seconds
  sqs_managed_sse_enabled   = true
}

resource "aws_sqs_queue" "this" {
  for_each = local.queues

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
  for_each = local.queues

  queue_url = aws_sqs_queue.dead_letter[each.key].id
  redrive_allow_policy = jsonencode({
    redrivePermission = "byQueue"
    sourceQueueArns   = [aws_sqs_queue.this[each.key].arn]
  })
}

resource "aws_cloudwatch_event_rule" "this" {
  for_each = local.rules

  name = "${var.name_prefix}-${each.key}"
  event_pattern = jsonencode({
    source        = ["aws.s3"]
    "detail-type" = [each.value.kind]
    detail = {
      bucket = { name = [var.bucket] }
      object = { key = each.value.pattern }
    }
  })
}

resource "aws_cloudwatch_event_target" "this" {
  for_each = local.rules

  rule = aws_cloudwatch_event_rule.this[each.key].name
  arn  = aws_sqs_queue.this[each.value.reader].arn
}

resource "aws_sqs_queue_policy" "this" {
  for_each = local.queues

  queue_url = aws_sqs_queue.this[each.key].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "AdmitOnlyThisReadersRules"
      Effect    = "Allow"
      Principal = { Service = "events.amazonaws.com" }
      Action    = "sqs:SendMessage"
      Resource  = aws_sqs_queue.this[each.key].arn
      Condition = {
        ArnEquals = {
          "aws:SourceArn" = sort([for name, r in local.rules : aws_cloudwatch_event_rule.this[name].arn if r.reader == each.key])
        }
      }
    }]
  })
}

output "queues" {
  description = "Each reader's queue and dead-letter queue name, by reader."
  value = {
    for reader in keys(local.queues) : reader => {
      queue       = aws_sqs_queue.this[reader].name
      dead_letter = aws_sqs_queue.dead_letter[reader].name
    }
  }
}

output "queue_settings" {
  description = "Each reader's queue settings and queue policy, read from the resources, by reader, which the tests read."
  value = {
    for reader in keys(local.queues) : reader => {
      retention             = aws_sqs_queue.this[reader].message_retention_seconds
      dead_letter_retention = aws_sqs_queue.dead_letter[reader].message_retention_seconds
      visibility_timeout    = aws_sqs_queue.this[reader].visibility_timeout_seconds
      sse                   = aws_sqs_queue.this[reader].sqs_managed_sse_enabled
      dead_letter_sse       = aws_sqs_queue.dead_letter[reader].sqs_managed_sse_enabled
      max_receive_count     = jsondecode(aws_sqs_queue.this[reader].redrive_policy).maxReceiveCount
      dead_letter_target    = jsondecode(aws_sqs_queue.this[reader].redrive_policy).deadLetterTargetArn
      queue_arn             = aws_sqs_queue.this[reader].arn
      queue_policy          = jsondecode(aws_sqs_queue_policy.this[reader].policy)
      policy_queue_url      = aws_sqs_queue_policy.this[reader].queue_url
      redrive_allow         = jsondecode(aws_sqs_queue_redrive_allow_policy.dead_letter[reader].redrive_allow_policy)
      redrive_allow_queue   = aws_sqs_queue_redrive_allow_policy.dead_letter[reader].queue_url
    }
  }
}

output "rules" {
  description = "Each rule's name, pattern, ARN and target, read from the resources, by the routes file's name, which the tests read."
  value = {
    for name in keys(local.rules) : name => {
      name          = aws_cloudwatch_event_rule.this[name].name
      arn           = aws_cloudwatch_event_rule.this[name].arn
      event_pattern = jsondecode(aws_cloudwatch_event_rule.this[name].event_pattern)
      target_rule   = aws_cloudwatch_event_target.this[name].rule
      target        = aws_cloudwatch_event_target.this[name].arn
    }
  }
}
