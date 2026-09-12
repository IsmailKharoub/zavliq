# Independent temporary synthetic environment. Never share its state with the
# production module or promote its localhost Matrix namespace to production.
data "aws_caller_identity" "current" {}

locals {
  name = "zavliq-staging"
}

resource "aws_lightsail_instance" "app" {
  name              = local.name
  availability_zone = "us-east-1a"
  blueprint_id      = "ubuntu_24_04"
  bundle_id         = "medium_3_0"
  key_pair_name     = var.lightsail_key_pair_name
  user_data         = "bash -c 'echo ${base64encode(file("${path.module}/../scripts/cloud-init.sh"))} | base64 -d | bash'"
  lifecycle {
    ignore_changes = [user_data]
  }
}

resource "aws_lightsail_static_ip" "app" {
  name = "${local.name}-ip"
}

resource "aws_lightsail_static_ip_attachment" "app" {
  static_ip_name = aws_lightsail_static_ip.app.id
  instance_name  = aws_lightsail_instance.app.id
}

resource "aws_lightsail_instance_public_ports" "app" {
  instance_name = aws_lightsail_instance.app.name
  port_info {
    from_port = 22
    to_port   = 22
    protocol  = "tcp"
    cidrs     = var.admin_cidrs
  }
}

resource "aws_s3_bucket" "backups" {
  bucket        = "${local.name}-backups-${data.aws_caller_identity.current.account_id}"
  force_destroy = false
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
