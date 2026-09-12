# Offline provider mocks only: no credentials, live plans, state or AWS calls.
# Run with Terraform 1.15.x used for this deployment.
mock_provider "aws" {
  override_during = plan
  mock_data "aws_caller_identity" {
    defaults = { account_id = "111122223333" }
  }
}
mock_provider "archive" {
  override_during = plan
}

override_resource {
  target          = aws_lightsail_instance.app
  override_during = plan
  values = {
    id  = "zavliq-production"
    arn = "arn:aws:lightsail:us-east-1:111122223333:Instance/11111111-1111-1111-1111-111111111111"
  }
}
override_resource {
  target          = aws_lightsail_instance.replacement[0]
  override_during = plan
  values = {
    id  = "zavliq-production-recovery-fixture"
    arn = "arn:aws:lightsail:us-east-1:111122223333:Instance/22222222-2222-2222-2222-222222222222"
  }
}

override_resource {
  target          = aws_s3_bucket.backups
  override_during = plan
  values = {
    id  = "zavliq-production-backups-111122223333"
    arn = "arn:aws:s3:::zavliq-production-backups-111122223333"
  }
}

variables {
  github_repository          = "IsmailKharoub/zavliq"
  github_oidc_subject_prefix = "repo:IsmailKharoub@123/zavliq@456"
  github_oidc_provider_arn   = "arn:aws:iam::111122223333:oidc-provider/token.actions.githubusercontent.com"
  admin_cidrs                = ["192.0.2.1/32"]
  lightsail_key_pair_name    = "fixture-operator"
  alert_email                = "operator@example.invalid"
}

run "original_is_unchanged_by_default" {
  command = plan
  assert {
    condition     = length(aws_lightsail_instance.replacement) == 0 && output.instance_name == "zavliq-production"
    error_message = "Default inputs must not create or select a replacement."
  }
  assert {
    condition     = toset([for p in aws_lightsail_instance_public_ports.app.port_info : "${p.protocol}:${p.from_port}"]) == toset(["tcp:22", "tcp:80", "tcp:443", "udp:443"]) && aws_lightsail_static_ip_attachment.app.instance_name == aws_lightsail_instance.app.id
    error_message = "The original retains existing ingress and static-IP attachment."
  }
  assert {
    condition     = jsondecode(aws_iam_role_policy.github.policy).Statement[0].Resource == aws_lightsail_instance.app.arn
    error_message = "The default credential and port permissions must authorize only the original exact ARN."
  }
  assert {
    condition     = toset(jsondecode(aws_iam_role_policy.github.policy).Statement[0].Action) == toset(["lightsail:GetInstanceAccessDetails", "lightsail:OpenInstancePublicPorts", "lightsail:CloseInstancePublicPorts"])
    error_message = "Only the three resource-scoped credential and port actions may use the exact active ARN."
  }
  assert {
    condition     = toset(jsondecode(aws_iam_role_policy.github.policy).Statement[1].Action) == toset(["lightsail:GetInstance", "lightsail:GetOperation"]) && jsondecode(aws_iam_role_policy.github.policy).Statement[1].Resource == "*" && jsondecode(aws_iam_role_policy.github.policy).Statement[1].Condition == { StringEquals = { "aws:RequestedRegion" = "us-east-1" } }
    error_message = "Only the two reads without resource-level IAM support use Resource:* and must retain the fixed regional condition."
  }
  assert {
    condition     = length(jsondecode(aws_iam_role_policy.github.policy).Statement) == 4 && toset(jsondecode(aws_iam_role_policy.github.policy).Statement[2].Action) == toset(["s3:PutObject", "s3:GetObject"]) && jsondecode(aws_iam_role_policy.github.policy).Statement[2].Resource == "${aws_s3_bucket.backups.arn}/daily/*" && jsondecode(aws_iam_role_policy.github.policy).Statement[3].Action == ["s3:ListBucket"] && jsondecode(aws_iam_role_policy.github.policy).Statement[3].Resource == aws_s3_bucket.backups.arn && jsondecode(aws_iam_role_policy.github.policy).Statement[3].Condition == { StringLike = { "s3:prefix" = "daily/*" } }
    error_message = "The read correction must not add statements or broaden existing backup prefix permissions."
  }
}

run "standby_does_not_transfer_address_or_iam" {
  command = plan
  variables {
    replacement_instance_name = "zavliq-production-recovery-fixture"
  }
  assert {
    condition     = length(aws_lightsail_instance.replacement) == 1 && output.instance_name == "zavliq-production"
    error_message = "Creating recovery capacity must not select it."
  }
  assert {
    condition     = toset([for p in aws_lightsail_instance_public_ports.replacement[0].port_info : "${p.protocol}:${p.from_port}"]) == toset(["tcp:22"]) && alltrue([for p in aws_lightsail_instance_public_ports.replacement[0].port_info : p.cidrs == toset(var.admin_cidrs)])
    error_message = "An unselected replacement permits only the operator SSH CIDRs."
  }
  assert {
    condition     = aws_lightsail_static_ip_attachment.app.instance_name == aws_lightsail_instance.app.id && jsondecode(aws_iam_role_policy.github.policy).Statement[0].Resource == aws_lightsail_instance.app.arn
    error_message = "Standby creation must not transfer the static IP or CI authority."
  }
}

run "cutover_requires_named_host" {
  command = plan
  variables {
    active_instance               = "replacement"
    replacement_cutover_confirmed = true
  }
  expect_failures = [aws_lightsail_static_ip_attachment.app]
}

run "cutover_requires_operator_confirmation" {
  command = plan
  variables {
    replacement_instance_name  = "zavliq-production-recovery-fixture"
    active_instance            = "replacement"
    replacement_verifier_cidrs = ["192.0.2.2/32"]
    replacement_echo_cidr      = "192.0.2.3/32"
  }
  expect_failures = [aws_lightsail_static_ip_attachment.app]
}

run "confirmed_cutover_selects_restricted_verification" {
  command = plan
  variables {
    replacement_instance_name     = "zavliq-production-recovery-fixture"
    active_instance               = "replacement"
    replacement_cutover_confirmed = true
    replacement_verifier_cidrs    = ["192.0.2.2/32"]
    replacement_echo_cidr         = "192.0.2.3/32"
  }
  assert {
    condition     = output.instance_name == "zavliq-production-recovery-fixture" && aws_lightsail_static_ip_attachment.app.instance_name == aws_lightsail_instance.replacement[0].id
    error_message = "Cutover must select the exact replacement for the existing static IP."
  }
  assert {
    condition     = toset([for p in aws_lightsail_instance_public_ports.app.port_info : "${p.protocol}:${p.from_port}"]) == toset(["tcp:22"]) && toset([for p in aws_lightsail_instance_public_ports.replacement[0].port_info : "${p.protocol}:${p.from_port}"]) == toset(["tcp:22", "tcp:80", "tcp:443"])
    error_message = "First cutover keeps HTTPS restricted and UDP443 closed."
  }
  assert {
    condition     = alltrue([for p in aws_lightsail_instance_public_ports.replacement[0].port_info : p.from_port != 443 || p.cidrs == toset(["192.0.2.2/32", "192.0.2.3/32"])]) && output.replacement_ingress_phase == "verification"
    error_message = "Canonical HTTPS must allow exactly the reviewed verifier and self-Echo host addresses."
  }
  assert {
    condition     = alltrue([for p in aws_lightsail_instance_public_ports.replacement[0].port_info : p.from_port != 80 || p.cidrs == toset(["0.0.0.0/0"])])
    error_message = "TCP80 remains available for ACME before public HTTPS opening."
  }
  assert {
    condition     = jsondecode(aws_iam_role_policy.github.policy).Statement[0].Resource == aws_lightsail_instance.replacement[0].arn && output.instance_arn == aws_lightsail_instance.replacement[0].arn
    error_message = "CI credential and port permissions must use only the exact replacement ARN, with no wildcard or dual-host grant."
  }
  assert {
    condition     = aws_lightsail_instance.app.name == "zavliq-production" && aws_s3_bucket.backups.bucket == "zavliq-production-backups-111122223333" && aws_lambda_function.monitor.function_name == "zavliq-production-monitor"
    error_message = "The original host and shared storage/monitor namespace must be retained."
  }
}

run "original_name_cannot_be_reused" {
  command = plan
  variables {
    replacement_instance_name = "zavliq-production"
  }
  expect_failures = [var.replacement_instance_name]
}

run "verification_requires_explicit_verifiers" {
  command = plan
  variables {
    replacement_instance_name     = "zavliq-production-recovery-fixture"
    active_instance               = "replacement"
    replacement_cutover_confirmed = true
    replacement_echo_cidr         = "192.0.2.3/32"
  }
  expect_failures = [aws_lightsail_instance_public_ports.replacement]
}

run "verification_requires_explicit_echo_address" {
  command = plan
  variables {
    replacement_instance_name     = "zavliq-production-recovery-fixture"
    active_instance               = "replacement"
    replacement_cutover_confirmed = true
    replacement_verifier_cidrs    = ["192.0.2.2/32"]
  }
  expect_failures = [aws_lightsail_instance_public_ports.replacement]
}

run "verification_refuses_network_wide_ingress" {
  command = plan
  variables {
    replacement_verifier_cidrs = ["0.0.0.0/0"]
  }
  expect_failures = [var.replacement_verifier_cidrs]
}

run "public_opening_requires_separate_verification_confirmation" {
  command = plan
  variables {
    replacement_instance_name     = "zavliq-production-recovery-fixture"
    active_instance               = "replacement"
    replacement_cutover_confirmed = true
    replacement_ingress_phase     = "public"
  }
  expect_failures = [aws_lightsail_instance_public_ports.replacement]
}

run "verified_public_opening_preserves_original_fence" {
  command = plan
  variables {
    replacement_instance_name          = "zavliq-production-recovery-fixture"
    active_instance                    = "replacement"
    replacement_cutover_confirmed      = true
    replacement_ingress_phase          = "public"
    replacement_verification_confirmed = true
  }
  assert {
    condition     = output.replacement_ingress_phase == "public" && toset([for p in aws_lightsail_instance_public_ports.replacement[0].port_info : "${p.protocol}:${p.from_port}"]) == toset(["tcp:22", "tcp:80", "tcp:443", "udp:443"])
    error_message = "Public opening is available only through the explicit later phase."
  }
  assert {
    condition     = alltrue([for p in aws_lightsail_instance_public_ports.replacement[0].port_info : p.from_port == 22 || p.cidrs == toset(["0.0.0.0/0"])]) && toset([for p in aws_lightsail_instance_public_ports.app.port_info : "${p.protocol}:${p.from_port}"]) == toset(["tcp:22"])
    error_message = "Public opening must not reopen the retained original host."
  }
  assert {
    condition     = jsondecode(aws_iam_role_policy.github.policy).Statement[0].Resource == aws_lightsail_instance.replacement[0].arn && aws_lightsail_static_ip_attachment.app.instance_name == aws_lightsail_instance.replacement[0].id
    error_message = "The public phase must preserve the selected exact instance and static IP."
  }
}
