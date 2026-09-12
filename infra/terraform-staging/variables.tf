variable "lightsail_key_pair_name" {
  type        = string
  description = "Existing us-east-1 key pair. Private key material never enters Terraform."
}

variable "admin_cidrs" {
  type        = list(string)
  description = "Exact operator IPv4 /32 addresses. All application ingress remains closed."
  validation {
    condition     = length(var.admin_cidrs) > 0 && alltrue([for cidr in var.admin_cidrs : can(cidrnetmask(cidr)) && endswith(cidr, "/32")])
    error_message = "Private staging permits only explicit IPv4 /32 SSH sources."
  }
}
