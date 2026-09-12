output "public_ip" { value = aws_lightsail_static_ip.app.ip_address }
output "instance_name" { value = aws_lightsail_instance.app.name }
output "backup_bucket" { value = aws_s3_bucket.backups.id }
output "github_role_arn" { value = aws_iam_role.github.arn }
output "origin" { value = "https://${var.domain}" }
