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

variable "replacement_instance_name" {
  type        = string
  default     = null
  nullable    = true
  description = "Explicitly reviewed, distinctly named recovery host. Null creates no replacement. Never reuse the original instance name."
  validation {
    condition     = var.replacement_instance_name == null ? true : (length(var.replacement_instance_name) <= 63 && can(regex("^zavliq-production-recovery-[a-z0-9]([a-z0-9-]*[a-z0-9])?$", var.replacement_instance_name)))
    error_message = "Use a distinct zavliq-production-recovery-<suffix> name; no name is selected by default."
  }
}

variable "active_instance" {
  type        = string
  default     = "original"
  description = "Explicit host selection for the existing static IP and exact GitHub instance IAM scope."
  validation {
    condition     = contains(["original", "replacement"], var.active_instance)
    error_message = "Select original or replacement explicitly."
  }
}

variable "replacement_cutover_confirmed" {
  type        = bool
  default     = false
  description = "Operator attestation that public ingress, Echo and maintenance writers were fenced before a verified final snapshot, original core is stopped, private restored-core checks passed, and no CI access journal is outstanding. Required to select replacement."
}

variable "replacement_ingress_phase" {
  type        = string
  default     = "verification"
  description = "Selected replacement starts with restricted canonical verification. Public opening is a separate reviewed change after verification."
  validation {
    condition     = contains(["verification", "public"], var.replacement_ingress_phase)
    error_message = "Use verification or public."
  }
}

variable "replacement_verifier_cidrs" {
  type        = list(string)
  default     = []
  description = "Explicit reviewed verifier IPv4 /32s allowed to reach canonical HTTPS during replacement verification. No addresses are selected by default."
  validation {
    condition     = alltrue([for cidr in var.replacement_verifier_cidrs : can(cidrnetmask(cidr)) && endswith(cidr, "/32")])
    error_message = "Verifier ingress accepts only explicit IPv4 /32 host addresses."
  }
}

variable "replacement_echo_cidr" {
  type        = string
  default     = null
  nullable    = true
  description = "Reviewed replacement Echo egress IPv4 /32 for its canonical HTTPS connection after static-IP attachment. Must be checked against actual routing."
  validation {
    condition     = var.replacement_echo_cidr == null ? true : (can(cidrnetmask(var.replacement_echo_cidr)) && endswith(var.replacement_echo_cidr, "/32"))
    error_message = "Echo ingress accepts only an explicit IPv4 /32 host address."
  }
}

variable "replacement_verification_confirmed" {
  type        = bool
  default     = false
  description = "Separate operator attestation that canonical TLS, original-device recovery, restored Echo and the production restore-complete checks passed. Required to open replacement HTTPS publicly."
}
