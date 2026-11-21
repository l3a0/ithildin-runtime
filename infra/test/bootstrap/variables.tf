variable "state_bucket" {
  description = "The S3 bucket that holds both configurations' state."
  type        = string
  nullable    = false

  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$", var.state_bucket))
    error_message = "state_bucket must be an S3 bucket name."
  }
}

variable "budget_limit" {
  description = "The monthly budget in US dollars. 5, by the owner's ruling. The budget alarm's drill lowers it for up to a day, as docs/what-a-person-runs.md's \"Drill the budget alarm\" says."
  type        = string
  default     = "5"
  nullable    = false
}

variable "adopt_existing" {
  description = "Import the existing state bucket. The tests set it to false, because an import crashes tofu test."
  type        = bool
  default     = true
  nullable    = false
}
