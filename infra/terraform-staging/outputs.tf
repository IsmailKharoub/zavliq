output "instance_name" {
  value = aws_lightsail_instance.app.name
}

output "static_ip" {
  value = aws_lightsail_static_ip.app.ip_address
}

output "backup_bucket" {
  value = aws_s3_bucket.backups.id
}
