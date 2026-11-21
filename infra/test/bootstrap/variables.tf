variable "state_bucket" {
  description = "The S3 bucket that holds both configurations' state."
  type        = string
  nullable    = false

  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$", var.state_bucket))
    error_message = "state_bucket must be an S3 bucket name."
  }
}

variable "budget_email" {
  description = "The address the budget alarm writes to. Pass it with -var on the command line."
  type        = string
  nullable    = false
  sensitive   = true

  validation {
    condition     = can(regex("^[^@\\s]+@[^@\\s]+$", var.budget_email))
    error_message = "budget_email must be an email address."
  }
}

variable "adopt_existing" {
  description = "Import the existing state bucket. The tests set it to false, because an import crashes tofu test."
  type        = bool
  default     = true
  nullable    = false
}
