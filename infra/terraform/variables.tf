variable "environment" {
  type    = string
  default = "production"
  validation {
    condition     = contains(["production", "staging"], var.environment)
    error_message = "Use production or staging."
  }
}

variable "domain" {
  type    = string
  default = "zavliq.com"
}

variable "github_repository" {
  type        = string
  description = "Exact owner/repository permitted to use deployment OIDC credentials."
  validation {
    condition     = can(regex("^[A-Za-z0-9-]+/[A-Za-z0-9_.-]+$", var.github_repository))
    error_message = "Supply the exact GitHub owner/repository without wildcards."
  }
}

variable "github_oidc_subject_prefix" {
  type        = string
  description = "Exact immutable sub_claim_prefix returned by GitHub's repository OIDC customization API."
  validation {
    condition     = can(regex("^repo:[A-Za-z0-9-]+@[1-9][0-9]*/[A-Za-z0-9_.-]+@[1-9][0-9]*$", var.github_oidc_subject_prefix))
    error_message = "Supply the repository's verified immutable OIDC subject prefix, including owner and repository IDs."
  }
}

variable "github_oidc_provider_arn" {
  type        = string
  description = "Existing token.actions.githubusercontent.com IAM OIDC provider ARN."
}

variable "admin_cidrs" {
  type        = list(string)
  description = "Operator source IPv4 CIDRs permitted to SSH. CI adds/removes its own /32 temporarily."
  validation {
    condition     = length(var.admin_cidrs) > 0 && alltrue([for cidr in var.admin_cidrs : can(cidrnetmask(cidr)) && !endswith(cidr, "/0")])
    error_message = "Supply specific IPv4 CIDRs; unrestricted SSH is forbidden."
  }
}

variable "lightsail_key_pair_name" {
  type        = string
  description = "An existing us-east-1 Lightsail SSH key pair; no private key is stored in Terraform."
}

variable "alert_email" {
  type        = string
  description = "Operator email for production host-availability notifications; the account budget is managed separately."
}

variable "route53_zone_id" {
  type        = string
  default     = ""
  description = "Existing public hosted-zone ID; blank leaves DNS management outside this module."
}
