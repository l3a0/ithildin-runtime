variable "name" {
  description = "The bucket's name."
  type        = string
  nullable    = false

  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$", var.name))
    error_message = "name must be an S3 bucket name."
  }
}

variable "allowed_principal_arns" {
  description = "Every principal the policy lets touch the bucket at all, matched on aws:PrincipalArn. Every other principal is refused every S3 action."
  type        = list(string)
  nullable    = false

  validation {
    condition     = length(var.allowed_principal_arns) > 0
    error_message = "allowed_principal_arns must name at least one principal, or nobody can reach the bucket."
  }
}

variable "runtime_principal_arns" {
  description = "The runtime's principals, which the policy refuses any delete and any write that carries neither conditional header."
  type        = list(string)
  nullable    = false
}

variable "settings_principal_arns" {
  description = "The principals that may change the bucket's policy, versioning, Object Lock, lifecycle and notifications. On a runtime bucket that is the owner's alone."
  type        = list(string)
  nullable    = false

  validation {
    condition     = length(var.settings_principal_arns) > 0
    error_message = "settings_principal_arns must name at least one principal, or the policy can never change again."
  }
}

variable "owner_principal_arns" {
  description = "The owner's principals, refused a write under leases/accounts/ that lacks If-Match."
  type        = list(string)
  nullable    = false
}

variable "operator_principal_arns" {
  description = "The operator's principals, refused a write under leases/accounts/ that lacks If-Match, and any version delete."
  type        = list(string)
  default     = []
  nullable    = false
}

variable "switch_clearer_arns" {
  description = "The principals that may delete an account's switch, accounts/<account>/switch.json."
  type        = list(string)
  nullable    = false
}

variable "stop_clearer_arns" {
  description = "The principals that may delete an account's fired loss stop, accounts/<account>/stop.json."
  type        = list(string)
  nullable    = false
}

variable "off_switch_clearer_arns" {
  description = "The principals that may delete a strategy's off switch, accounts/<account>/strategies/<strategy>/off.json."
  type        = list(string)
  nullable    = false
}

variable "max_retention_days" {
  description = "When set, the policy refuses any write that sets an Object Lock retention date further out than this many days. Null sets no cap."
  type        = number
  default     = null
}

variable "lifecycle_rules" {
  description = "The bucket's lifecycle rules. An empty prefix covers the whole bucket, and a null day count leaves that expiry out."
  type = list(object({
    id                       = string
    prefix                   = string
    expire_current_days      = optional(number)
    expire_noncurrent_days   = optional(number)
    abort_incomplete_upload  = optional(number)
    expire_old_delete_marker = optional(bool, false)
  }))
  nullable = false

  # S3 refuses an expiration that sets both a day count and the delete-marker cleanup.
  validation {
    condition     = alltrue([for r in var.lifecycle_rules : !(r.expire_old_delete_marker && r.expire_current_days != null)])
    error_message = "A lifecycle rule cannot both expire current versions by age and remove old delete markers. Give each its own rule."
  }
}
