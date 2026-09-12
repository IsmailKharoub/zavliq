output "public_ip" { value = aws_lightsail_static_ip.app.ip_address }
output "instance_name" { value = local.active_instance.name }
output "instance_arn" { value = local.active_instance.arn }
output "active_instance" { value = var.active_instance }
output "original_instance_name" { value = aws_lightsail_instance.app.name }
output "replacement_instance_name" { value = var.replacement_instance_name }
output "replacement_ingress_phase" { value = var.active_instance == "replacement" ? var.replacement_ingress_phase : null }
output "backup_bucket" { value = aws_s3_bucket.backups.id }
output "github_role_arn" { value = aws_iam_role.github.arn }
output "origin" { value = "https://${var.domain}" }
