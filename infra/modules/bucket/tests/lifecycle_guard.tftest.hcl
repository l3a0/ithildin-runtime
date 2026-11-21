# The module refuses every lifecycle rule but the three a runtime bucket may carry,
# unless the caller says every blob in the bucket is regenerable. A bucket policy cannot stop a
# lifecycle rule, so this check is what keeps a later environment's journal safe from
# one. Every run plans, because test cleanup cannot destroy a prevent_destroy resource.

mock_provider "aws" {}

variables {
  name                    = "example-runtime"
  allowed_principal_arns  = ["arn:aws:iam::000000000000:role/runtime"]
  runtime_principal_arns  = ["arn:aws:iam::000000000000:role/runtime"]
  settings_principal_arns = ["arn:aws:iam::000000000000:role/owner"]
  owner_principal_arns    = ["arn:aws:iam::000000000000:role/owner"]
  switch_clearer_arns     = ["arn:aws:iam::000000000000:role/owner"]
  stop_clearer_arns       = ["arn:aws:iam::000000000000:role/owner"]
  off_switch_clearer_arns = ["arn:aws:iam::000000000000:role/owner"]
}

run "a_runtime_buckets_own_rules_plan" {
  command = plan

  variables {
    lifecycle_rules = [
      { id = "heartbeats", prefix = "heartbeats/", expire_current_days = 7 },
      { id = "old-leases", prefix = "leases/", expire_noncurrent_days = 7 },
    ]
  }

  # The resource carries each rule's own prefix and expiry kind, so a rule the guard
  # accepted is the rule the bucket gets.
  assert {
    condition = jsonencode(output.lifecycle_rules) == jsonencode([
      { id = "heartbeats", status = "Enabled", prefix = "heartbeats/", current = 7, marker = false, noncurrent = null, uploads = null },
      { id = "old-leases", status = "Enabled", prefix = "leases/", current = null, marker = null, noncurrent = 7, uploads = null },
    ])
    error_message = "The lifecycle the bucket gets is not the two rules the guard accepted, with their own prefixes and expiry kinds."
  }
}

run "a_rule_over_the_whole_bucket_is_refused" {
  command = plan

  variables {
    lifecycle_rules = [{ id = "everything", prefix = "", expire_current_days = 30 }]
  }

  expect_failures = [aws_s3_bucket_lifecycle_configuration.this]
}

run "a_rule_reaching_the_journal_is_refused" {
  command = plan

  variables {
    lifecycle_rules = [{ id = "journal", prefix = "journal/", expire_noncurrent_days = 1 }]
  }

  expect_failures = [aws_s3_bucket_lifecycle_configuration.this]
}

run "a_rule_above_a_recorded_prefix_is_refused" {
  command = plan

  variables {
    lifecycle_rules = [{ id = "r", prefix = "r", expire_current_days = 1 }]
  }

  expect_failures = [aws_s3_bucket_lifecycle_configuration.this]
}

run "a_rule_inside_a_runs_prefix_is_refused" {
  command = plan

  variables {
    lifecycle_rules = [{ id = "ci-runs", prefix = "runs/ci/", expire_current_days = 1 }]
  }

  expect_failures = [aws_s3_bucket_lifecycle_configuration.this]
}

run "expiring_a_current_lease_is_refused" {
  command = plan

  variables {
    lifecycle_rules = [{ id = "leases", prefix = "leases/", expire_current_days = 1 }]
  }

  expect_failures = [aws_s3_bucket_lifecycle_configuration.this]
}

run "a_regenerable_bucket_may_expire_everything" {
  command = plan

  variables {
    regenerable_only   = true
    max_retention_days = 1
    lifecycle_rules    = [{ id = "everything", prefix = "", expire_current_days = 1 }]
  }
}

run "a_bucket_marked_regenerable_without_the_one_day_cap_is_refused" {
  command = plan

  variables {
    regenerable_only   = true
    max_retention_days = 2
    lifecycle_rules    = [{ id = "everything", prefix = "", expire_current_days = 1 }]
  }

  expect_failures = [aws_s3_bucket.this]
}

run "a_bucket_marked_regenerable_with_no_cap_is_refused" {
  command = plan

  variables {
    regenerable_only = true
    lifecycle_rules  = []
  }

  expect_failures = [aws_s3_bucket.this]
}

run "aborting_unfinished_uploads_plans" {
  command = plan

  variables {
    lifecycle_rules = [{ id = "uploads", prefix = "", abort_incomplete_upload = 1 }]
  }
}

run "an_empty_list_of_clearers_refuses_everyone" {
  command = plan

  variables {
    lifecycle_rules     = []
    switch_clearer_arns = []
  }

  assert {
    condition = [for s in jsondecode(aws_s3_bucket_policy.this.policy).Statement : s if s.Sid == "RefuseSwitchDeletes"] == [{
      Sid       = "RefuseSwitchDeletes"
      Effect    = "Deny"
      Principal = "*"
      Action    = ["s3:DeleteObject", "s3:DeleteObjectVersion"]
      Resource  = ["arn:aws:s3:::example-runtime/accounts/*/switch.json"]
    }]
    error_message = "An empty list of those who may clear the switch does not refuse every principal."
  }
}

run "a_rule_reaching_the_simulated_broker_is_refused" {
  command = plan

  variables {
    lifecycle_rules = [{ id = "r", prefix = "sim/", expire_current_days = 1 }]
  }

  expect_failures = [aws_s3_bucket_lifecycle_configuration.this]
}

run "a_rule_reaching_the_account_controls_is_refused" {
  command = plan

  variables {
    lifecycle_rules = [{ id = "r", prefix = "accounts/", expire_current_days = 1 }]
  }

  expect_failures = [aws_s3_bucket_lifecycle_configuration.this]
}

run "a_rule_reaching_the_release_records_is_refused" {
  command = plan

  variables {
    lifecycle_rules = [{ id = "r", prefix = "release/", expire_noncurrent_days = 1 }]
  }

  expect_failures = [aws_s3_bucket_lifecycle_configuration.this]
}

run "a_short_prefix_over_the_leases_is_refused" {
  command = plan

  # A noncurrent expiry, which the leases/ clause would accept, so only the prefix
  # decides.
  variables {
    lifecycle_rules = [{ id = "r", prefix = "leases", expire_noncurrent_days = 1 }]
  }

  expect_failures = [aws_s3_bucket_lifecycle_configuration.this]
}

run "a_one_letter_prefix_is_refused" {
  command = plan

  variables {
    lifecycle_rules = [{ id = "r", prefix = "a", expire_noncurrent_days = 1 }]
  }

  expect_failures = [aws_s3_bucket_lifecycle_configuration.this]
}

run "removing_old_delete_markers_under_leases_is_refused" {
  command = plan

  variables {
    lifecycle_rules = [{ id = "r", prefix = "leases/", expire_old_delete_marker = true }]
  }

  expect_failures = [aws_s3_bucket_lifecycle_configuration.this]
}

run "an_operator_is_refused_version_deletes_and_unconditional_account_lease_writes" {
  command = plan

  variables {
    lifecycle_rules         = []
    operator_principal_arns = ["arn:aws:iam::000000000000:role/operator"]
  }

  assert {
    condition = [for s in jsondecode(aws_s3_bucket_policy.this.policy).Statement : s if s.Sid == "RefuseOperatorVersionDeletes"] == [{
      Sid       = "RefuseOperatorVersionDeletes"
      Effect    = "Deny"
      Principal = "*"
      Action    = "s3:DeleteObjectVersion"
      Resource  = ["arn:aws:s3:::example-runtime/*"]
      Condition = { ArnLike = { "aws:PrincipalArn" = ["arn:aws:iam::000000000000:role/operator"] } }
    }]
    error_message = "The operator is not refused every version delete."
  }

  assert {
    condition = [for s in jsondecode(aws_s3_bucket_policy.this.policy).Statement : s.Condition if s.Sid == "RefusePeoplesAccountLeaseWritesWithoutIfMatch"] == [{
      Null    = { "s3:if-match" = "true" }
      ArnLike = { "aws:PrincipalArn" = ["arn:aws:iam::000000000000:role/owner", "arn:aws:iam::000000000000:role/operator"] }
    }]
    error_message = "The owner and the operator are not both refused a write under leases/accounts/ without If-Match."
  }
}

run "a_rule_expiring_old_versions_across_the_whole_bucket_is_refused" {
  command = plan

  variables {
    lifecycle_rules = [{ id = "r", prefix = "", expire_noncurrent_days = 1 }]
  }

  expect_failures = [aws_s3_bucket_lifecycle_configuration.this]
}

run "a_prefix_short_of_the_heartbeats_folder_is_refused" {
  command = plan

  # A current expiry, which the heartbeats/ clause would accept, so only the prefix
  # decides.
  variables {
    lifecycle_rules = [{ id = "r", prefix = "heartbeats", expire_current_days = 1 }]
  }

  expect_failures = [aws_s3_bucket_lifecycle_configuration.this]
}

run "an_allowed_rule_beside_a_refused_one_does_not_pass_it" {
  command = plan

  # Every rule is checked, so one allowed rule cannot carry a refused one.
  variables {
    lifecycle_rules = [
      { id = "heartbeats", prefix = "heartbeats/", expire_current_days = 7 },
      { id = "everything", prefix = "", expire_current_days = 30 },
    ]
  }

  expect_failures = [aws_s3_bucket_lifecycle_configuration.this]
}

run "a_rule_on_the_release_blobs_is_refused" {
  command = plan

  # The key holds leases/ inside it, so only a check on the start of the prefix refuses it.
  variables {
    lifecycle_rules = [{ id = "r", prefix = "accounts/a/releases/", expire_noncurrent_days = 1 }]
  }

  expect_failures = [aws_s3_bucket_lifecycle_configuration.this]
}

run "a_rule_with_heartbeats_inside_its_prefix_is_refused" {
  command = plan

  # The key holds heartbeats/ inside it, so only a check on the start of the prefix refuses it.
  variables {
    lifecycle_rules = [{ id = "r", prefix = "journal/heartbeats/", expire_current_days = 1 }]
  }

  expect_failures = [aws_s3_bucket_lifecycle_configuration.this]
}

run "a_refused_rule_before_an_allowed_one_is_refused" {
  command = plan

  # The same pair as the run above it in reverse order, so a guard that reads only
  # the last rule fails here.
  variables {
    lifecycle_rules = [
      { id = "everything", prefix = "", expire_current_days = 30 },
      { id = "heartbeats", prefix = "heartbeats/", expire_current_days = 7 },
    ]
  }

  expect_failures = [aws_s3_bucket_lifecycle_configuration.this]
}

run "a_one_day_cap_does_not_mark_a_bucket_regenerable" {
  command = plan

  # Only regenerable_only lifts the guard. A retention cap alone does not.
  variables {
    max_retention_days = 1
    lifecycle_rules    = [{ id = "everything", prefix = "", expire_current_days = 1 }]
  }

  expect_failures = [aws_s3_bucket_lifecycle_configuration.this]
}
