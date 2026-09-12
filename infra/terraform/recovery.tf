# Optional recovery capacity is separately named and retained after cutover.
# Initial creation leaves the original host, static IP and IAM scope selected.
locals {
  replacement_https_cidrs = distinct(concat(var.replacement_verifier_cidrs, var.replacement_echo_cidr == null ? [] : [var.replacement_echo_cidr]))
  replacement_ports = var.replacement_ingress_phase == "public" ? [
    for port in local.public_ports : merge(port, { cidrs = ["0.0.0.0/0"] })
    ] : [
    # Caddy's port 80 is used for ACME HTTP-01 and redirects, not Matrix traffic.
    { port = 80, protocol = "tcp", cidrs = ["0.0.0.0/0"] },
    { port = 443, protocol = "tcp", cidrs = local.replacement_https_cidrs },
  ]
}

resource "aws_lightsail_instance" "replacement" {
  count             = var.replacement_instance_name == null ? 0 : 1
  name              = var.replacement_instance_name
  availability_zone = "us-east-1a"
  blueprint_id      = "ubuntu_24_04"
  bundle_id         = "medium_3_0"
  ip_address_type   = "ipv4"
  key_pair_name     = var.lightsail_key_pair_name
  user_data         = "bash -c 'echo ${base64encode(file("${path.module}/../scripts/cloud-init.sh"))} | base64 -d | bash'"
  lifecycle {
    prevent_destroy = true
    ignore_changes  = [user_data]
    precondition {
      condition     = var.environment == "production"
      error_message = "The recovery-host option is restricted to production."
    }
  }
}

resource "aws_lightsail_instance_public_ports" "replacement" {
  count         = var.replacement_instance_name == null ? 0 : 1
  instance_name = aws_lightsail_instance.replacement[0].name
  port_info {
    from_port = 22
    to_port   = 22
    protocol  = "tcp"
    cidrs     = var.admin_cidrs
  }
  dynamic "port_info" {
    for_each = var.active_instance == "replacement" ? local.replacement_ports : []
    content {
      from_port = port_info.value.port
      to_port   = port_info.value.port
      protocol  = port_info.value.protocol
      cidrs     = port_info.value.cidrs
    }
  }
  lifecycle {
    precondition {
      condition     = var.active_instance != "replacement" || var.replacement_ingress_phase != "verification" || (length(var.replacement_verifier_cidrs) > 0 && var.replacement_echo_cidr != null)
      error_message = "Canonical verification requires reviewed verifier and self-Echo IPv4 /32s; no HTTPS audience is inferred."
    }
    precondition {
      condition     = var.active_instance != "replacement" || var.replacement_ingress_phase != "public" || var.replacement_verification_confirmed
      error_message = "Public opening requires a separate confirmation of canonical TLS, original-device recovery, restored Echo and restore-complete checks."
    }
  }
}
