data "archive_file" "monitor" {
  type        = "zip"
  source_file = "${path.module}/../scripts/cloud-monitor.py"
  output_path = "${path.module}/monitor.zip"
}

resource "aws_sns_topic" "alerts" { name = "${local.name}-alerts" }

resource "aws_sns_topic_subscription" "operator" {
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = var.alert_email
}

resource "aws_iam_role" "monitor" {
  name = "${local.name}-monitor"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "lambda.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
}

resource "aws_iam_role_policy" "monitor" {
  role = aws_iam_role.monitor.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = ["s3:ListBucket"], Resource = aws_s3_bucket.backups.arn },
      { Effect = "Allow", Action = ["cloudwatch:PutMetricData"], Resource = "*", Condition = { StringEquals = { "cloudwatch:namespace" = "Zavliq" } } },
      { Effect = "Allow", Action = ["logs:CreateLogStream", "logs:PutLogEvents"], Resource = "${aws_cloudwatch_log_group.monitor.arn}:*" }
    ]
  })
}

resource "aws_cloudwatch_log_group" "monitor" {
  name              = "/aws/lambda/${local.name}-monitor"
  retention_in_days = 7
}

resource "aws_lambda_function" "monitor" {
  function_name    = "${local.name}-monitor"
  role             = aws_iam_role.monitor.arn
  filename         = data.archive_file.monitor.output_path
  source_code_hash = data.archive_file.monitor.output_base64sha256
  handler          = "cloud-monitor.handler"
  runtime          = "python3.13"
  timeout          = 40
  memory_size      = 128
  environment {
    variables = {
      ZAVLIQ_ORIGIN        = "https://${var.domain}"
      ZAVLIQ_BACKUP_BUCKET = aws_s3_bucket.backups.id
      ZAVLIQ_ENVIRONMENT   = var.environment
    }
  }
  depends_on = [aws_iam_role_policy.monitor, aws_cloudwatch_log_group.monitor]
}

resource "aws_cloudwatch_event_rule" "monitor" {
  name                = "${local.name}-monitor"
  schedule_expression = "rate(5 minutes)"
}

resource "aws_cloudwatch_event_target" "monitor" {
  rule = aws_cloudwatch_event_rule.monitor.name
  arn  = aws_lambda_function.monitor.arn
}

resource "aws_lambda_permission" "monitor" {
  statement_id  = "ScheduledProbe"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.monitor.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.monitor.arn
}

resource "aws_cloudwatch_metric_alarm" "health" {
  for_each            = toset(["Available", "BackupFresh", "HostHealthy"])
  alarm_name          = "${local.name}-${each.value}"
  alarm_description   = "Zavliq ${each.value} failed or telemetry stopped. See operations runbook."
  comparison_operator = "LessThanThreshold"
  evaluation_periods  = 2
  metric_name         = each.value
  namespace           = "Zavliq"
  period              = 300
  statistic           = "Minimum"
  threshold           = 1
  treat_missing_data  = "breaching"
  dimensions          = { Environment = var.environment }
  alarm_actions       = [aws_sns_topic.alerts.arn]
  ok_actions          = [aws_sns_topic.alerts.arn]
}
