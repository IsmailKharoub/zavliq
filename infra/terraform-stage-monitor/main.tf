terraform {
  required_version = ">= 1.7, < 2.0"
  required_providers {
    aws     = { source = "hashicorp/aws", version = "~> 6.0" }
    archive = { source = "hashicorp/archive", version = "~> 2.7" }
  }
}

provider "aws" {
  region = "us-east-1"
  default_tags {
    tags = { Project = "zavliq", Environment = "staging", ManagedBy = "terraform" }
  }
}

variable "backup_bucket" {
  type = string
  validation {
    condition     = can(regex("^zavliq-staging-backups-[0-9]{12}$", var.backup_bucket))
    error_message = "Use the existing private staging backup bucket."
  }
}

variable "alert_topic_arn" {
  type = string
  validation {
    condition     = can(regex("^arn:aws:sns:us-east-1:[0-9]{12}:zavliq-staging-alerts$", var.alert_topic_arn))
    error_message = "Use the existing staging alerts topic."
  }
}

data "archive_file" "monitor" {
  type        = "zip"
  source_file = "${path.module}/../staging/cloud_monitor.py"
  output_path = "${path.module}/monitor.zip"
}

resource "aws_iam_role" "monitor" {
  name               = "zavliq-private-stage-monitor"
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{ Effect = "Allow", Principal = { Service = "lambda.amazonaws.com" }, Action = "sts:AssumeRole" }] })
}

resource "aws_cloudwatch_log_group" "monitor" {
  name              = "/aws/lambda/zavliq-private-stage-monitor"
  retention_in_days = 7
}

resource "aws_iam_role_policy" "monitor" {
  role = aws_iam_role.monitor.id
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = ["s3:GetObject"], Resource = ["arn:aws:s3:::${var.backup_bucket}/status/health.json", "arn:aws:s3:::${var.backup_bucket}/daily/zavliq-*.tar.age"] },
    { Effect = "Allow", Action = ["s3:ListBucket"], Resource = "arn:aws:s3:::${var.backup_bucket}", Condition = { StringEquals = { "s3:prefix" = "daily/" } } },
    { Effect = "Allow", Action = ["cloudwatch:PutMetricData"], Resource = "*", Condition = { StringEquals = { "cloudwatch:namespace" = "ZavliqStaging" } } },
    { Effect = "Allow", Action = ["logs:CreateLogStream", "logs:PutLogEvents"], Resource = "${aws_cloudwatch_log_group.monitor.arn}:*" }
  ] })
}

resource "aws_lambda_function" "monitor" {
  function_name    = "zavliq-private-stage-monitor"
  role             = aws_iam_role.monitor.arn
  filename         = data.archive_file.monitor.output_path
  source_code_hash = data.archive_file.monitor.output_base64sha256
  handler          = "cloud_monitor.handler"
  runtime          = "python3.13"
  memory_size      = 128
  timeout          = 30
  environment {
    variables = { ZAVLIQ_BACKUP_BUCKET = var.backup_bucket }
  }
  depends_on = [aws_iam_role_policy.monitor, aws_cloudwatch_log_group.monitor]
}

resource "aws_cloudwatch_event_rule" "monitor" {
  name                = "zavliq-private-stage-monitor"
  schedule_expression = "rate(5 minutes)"
}

resource "aws_cloudwatch_event_target" "monitor" {
  rule = aws_cloudwatch_event_rule.monitor.name
  arn  = aws_lambda_function.monitor.arn
}

resource "aws_lambda_permission" "monitor" {
  statement_id  = "ScheduledPrivateProbe"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.monitor.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.monitor.arn
}

resource "aws_cloudwatch_metric_alarm" "monitor" {
  for_each            = toset(["HostHealthy", "BackupFresh", "CapabilityValid"])
  alarm_name          = "zavliq-private-stage-${each.value}"
  alarm_description   = "Private staging ${each.value} failed or telemetry stopped. Capability expires after 48 hours."
  comparison_operator = "LessThanThreshold"
  evaluation_periods  = 2
  metric_name         = each.value
  namespace           = "ZavliqStaging"
  period              = 300
  statistic           = "Minimum"
  threshold           = 1
  treat_missing_data  = "breaching"
  dimensions          = { Environment = "private-staging" }
  alarm_actions       = [var.alert_topic_arn]
  ok_actions          = [var.alert_topic_arn]
}

output "function_name" {
  value = aws_lambda_function.monitor.function_name
}
