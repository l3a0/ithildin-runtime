# The module refuses a lifecycle rule that would expire recorded data, unless the
# caller says every blob in the bucket is regenerable. A bucket policy cannot stop a
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
    regenerable_only = true
    lifecycle_rules  = [{ id = "everything", prefix = "", expire_current_days = 1 }]
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
