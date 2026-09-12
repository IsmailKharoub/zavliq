# Copy to production.tfvars (ignored). Existing IDs are deliberately unspecified.
environment              = "production"
domain                   = "zavliq.com"
github_repository        = "OWNER/zavliq"
github_oidc_provider_arn = "arn:aws:iam::ACCOUNT:oidc-provider/token.actions.githubusercontent.com"
lightsail_key_pair_name  = "OPERATOR_KEY_NAME"
admin_cidrs              = ["203.0.113.10/32"]
alert_email              = "OPERATOR_EMAIL"
route53_zone_id          = ""
