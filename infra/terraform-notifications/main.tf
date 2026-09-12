terraform {
  required_version = ">= 1.7, < 2.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }
}

provider "aws" {
  region = "us-east-1"
  default_tags {
    tags = { Project = "zavliq", ManagedBy = "terraform" }
  }
}

variable "alert_email" {
  type        = string
  sensitive   = true
  description = "Authorized operator address. SNS confirmation is required before alerts can be delivered."
}

resource "aws_budgets_budget" "monthly" {
  name         = "zavliq-account-monthly"
  budget_type  = "COST"
  limit_amount = "100"
  limit_unit   = "USD"
  time_unit    = "MONTHLY"
  # Account-wide, including untagged traffic; these alerts are not a spending cap.
  dynamic "notification" {
    for_each = [50, 75, 90]
    content {
      comparison_operator        = "GREATER_THAN"
      threshold                  = notification.value
      threshold_type             = "ABSOLUTE_VALUE"
      notification_type          = "ACTUAL"
      subscriber_email_addresses = [var.alert_email]
    }
  }
}

resource "aws_sns_topic" "staging" {
  name = "zavliq-staging-alerts"
}

resource "aws_sns_topic_subscription" "operator" {
  topic_arn = aws_sns_topic.staging.arn
  protocol  = "email"
  endpoint  = var.alert_email
}

output "topic_arn" {
  value = aws_sns_topic.staging.arn
}

output "budget_name" {
  value = aws_budgets_budget.monthly.name
}
