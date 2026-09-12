data "aws_caller_identity" "current" {}

locals {
  name          = "zavliq-${var.environment}"
  backup_bucket = "${local.name}-backups-${data.aws_caller_identity.current.account_id}"
  # A replacement is opt-in. The original resource and its disks stay in state.
  active_instance = var.active_instance == "replacement" && var.replacement_instance_name != null ? one(aws_lightsail_instance.replacement) : aws_lightsail_instance.app
  public_ports = [
    { port = 80, protocol = "tcp" },
    { port = 443, protocol = "tcp" },
    { port = 443, protocol = "udp" },
  ]
}

resource "aws_lightsail_instance" "app" {
  name              = local.name
  availability_zone = "us-east-1a"
  blueprint_id      = "ubuntu_24_04"
  bundle_id         = "medium_3_0"
  key_pair_name     = var.lightsail_key_pair_name
  user_data         = "bash -c 'echo ${base64encode(file("${path.module}/../scripts/cloud-init.sh"))} | base64 -d | bash'"
  lifecycle {
    prevent_destroy = true
    ignore_changes  = [user_data]
  }
}

resource "aws_lightsail_static_ip" "app" {
  name = "${local.name}-ip"
}

resource "aws_lightsail_static_ip_attachment" "app" {
  static_ip_name = aws_lightsail_static_ip.app.id
  instance_name  = local.active_instance.id
  # Close the original host's public ingress before transferring the address.
  depends_on = [aws_lightsail_instance_public_ports.app, aws_lightsail_instance_public_ports.replacement]
  lifecycle {
    precondition {
      condition     = var.active_instance == "original" || (var.replacement_instance_name != null && var.replacement_cutover_confirmed)
      error_message = "Replacement cutover requires an explicitly named host, a final consistent snapshot taken after the public-write fence, stopped original core and successful private restored-core checks."
    }
  }
}

resource "aws_lightsail_instance_public_ports" "app" {
  instance_name = aws_lightsail_instance.app.name
  port_info {
    from_port = 22
    to_port   = 22
    protocol  = "tcp"
    cidrs     = var.admin_cidrs
  }
  dynamic "port_info" {
    for_each = var.active_instance == "original" ? local.public_ports : []
    content {
      from_port = port_info.value.port
      to_port   = port_info.value.port
      protocol  = port_info.value.protocol
      cidrs     = ["0.0.0.0/0"]
    }
  }
}

resource "aws_route53_record" "app" {
  count   = var.route53_zone_id == "" ? 0 : 1
  zone_id = var.route53_zone_id
  name    = var.domain
  type    = "A"
  ttl     = 300
  records = [aws_lightsail_static_ip.app.ip_address]
}

resource "aws_s3_bucket" "backups" {
  bucket        = local.backup_bucket
  force_destroy = false
  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_s3_bucket_public_access_block" "backups" {
  bucket                  = aws_s3_bucket.backups.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "backups" {
  bucket = aws_s3_bucket.backups.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "backups" {
  bucket = aws_s3_bucket.backups.id
  rule {
    id     = "seven-day-retention"
    status = "Enabled"
    filter { prefix = "daily/" }
    expiration { days = 7 }
    abort_incomplete_multipart_upload { days_after_initiation = 1 }
  }
}

resource "aws_s3_bucket_policy" "backups" {
  bucket = aws_s3_bucket.backups.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "RequireTLS"
      Effect    = "Deny"
      Principal = "*"
      Action    = "s3:*"
      Resource  = [aws_s3_bucket.backups.arn, "${aws_s3_bucket.backups.arn}/*"]
      Condition = { Bool = { "aws:SecureTransport" = "false" } }
    }]
  })
}

resource "aws_iam_role" "github" {
  name = "${local.name}-github"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Federated = var.github_oidc_provider_arn }
      Action    = "sts:AssumeRoleWithWebIdentity"
      Condition = { StringEquals = {
        "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com"
        "token.actions.githubusercontent.com:sub" = "${var.github_oidc_subject_prefix}:environment:${var.environment}"
      } }
    }]
  })
  lifecycle {
    precondition {
      condition     = replace(var.github_oidc_subject_prefix, "/@[0-9]+/", "") == "repo:${var.github_repository}"
      error_message = "The verified OIDC subject prefix must belong to the configured GitHub repository."
    }
  }
}

resource "aws_iam_role_policy" "github" {
  role = aws_iam_role.github.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = ["lightsail:GetInstanceAccessDetails", "lightsail:GetInstance", "lightsail:OpenInstancePublicPorts", "lightsail:CloseInstancePublicPorts"], Resource = local.active_instance.arn },
      # GetOperation has no resource-level IAM support; limit the read to our fixed region.
      # https://docs.aws.amazon.com/service-authorization/latest/reference/list_lightsail.html
      { Effect = "Allow", Action = ["lightsail:GetOperation"], Resource = "*", Condition = { StringEquals = { "aws:RequestedRegion" = "us-east-1" } } },
      { Effect = "Allow", Action = ["s3:PutObject", "s3:GetObject"], Resource = "${aws_s3_bucket.backups.arn}/daily/*" },
      { Effect = "Allow", Action = ["s3:ListBucket"], Resource = aws_s3_bucket.backups.arn, Condition = { StringLike = { "s3:prefix" = "daily/*" } } }
    ]
  })
}

# The account-wide $100 budget covers production and staging together. It is
# owned by ../terraform-notifications as zavliq-account-monthly; this module
# must not create another budget or take over that module's state.
