# Candidate publication and launch promotion

This procedure separates **publication for anonymous verification** from **launch after every gate passes**. It resolves the dependency between GitHub downloads and the published-install trial without waiving [any launch requirement](../launch-requirements.md). These instructions are prepared for maintainer review; they do not record publication, accounts, trials or promotion as already performed.

The application candidate is `2f76469b507c8745d4be5ef311f32774021143aa`, version `v0.1.0`. The exact-source CI and corrected draft build passed. [Authenticated artifact verification](private-artifact-verification-2f76469b507c.json) records the new manifest and explicit byte mapping to the isolated client installation; anonymous public installation remains unverified. The previous private candidate failed server activation because its extracted source directories were unreadable by the Synapse service user. Its artifact and failure evidence remains preserved; it cannot substitute for this corrected candidate's backend checks. Documentation and evidence may be committed later without retargeting that tag or rebuilding candidate assets. The existing draft workflow stays unchanged and must remain pinned to the selected application commit.

## 1. Hold the private draft until the service gates pass

Complete and record the final candidate's CI, offline/retry/membership/quotas/TTL checks, browser QA and cryptographic behavior, the full AWS load gate (100 clients, 10 aggregate messages/second, 30 minutes, p95 below two seconds and no lost acknowledged events), fresh-volume restore and rollback (RTO at most two hours, RPO at most 24 hours), daily encrypted backup with seven-day retention, monitoring/alert/spending checks, 24-hour staging soak and public TLS. The public deployment must use the reviewed source/images/configuration and be ready at `https://zavliq.com`. Preserve all failed runs alongside any passing final evidence. A shorter diagnostic or earlier image cannot substitute for the final gate.

Verify the repository's public source is ready for release and `/status` prominently links to the authoritative GitHub release metadata/notes, with an explicit explanation that a verification prerelease does not establish launch readiness. The separate health probe measures reachability. Current phase is recorded in the release metadata/notes; there is no additional operator-controlled runtime phase setting. Opening the repository and service is a separate operator action. Candidate publication is publicly discoverable, and GitHub release subscribers may receive a notification; “verification only” is not access control. Do not create a release discussion, send outreach, imply organic users or invite adopters during this phase. [GitHub release visibility and notifications](https://docs.github.com/en/repositories/releasing-projects-on-github/about-releases)

## 2. Freeze the assets and publish a verification prerelease

Review the existing draft's exact target commit, complete asset inventory, asset IDs/sizes/digests and `SHA256SUMS` digest. It must contain both native archives, the Node bundle, Python wheel, installer and skill before publication. Resolve any existing `v0.1.0` tag to the frozen application commit; never retarget a conflicting tag. Record the intended commit when a draft's tag has not yet been created, then verify the actual tag immediately after publication.

Confirm GitHub release immutability is enabled **before** publishing the draft. This locks the published tag and asset bytes while allowing later edits to title, notes and prerelease/latest status. Enabling it only affects future publications. [Immutable releases](https://docs.github.com/en/code-security/concepts/supply-chain-security/immutable-releases), [enabling immutability](https://docs.github.com/en/code-security/how-tos/secure-your-supply-chain/establish-provenance-and-integrity/prevent-release-changes)

The repository setting was enabled and read back as `enabled: true` at **2026-09-12 06:14:48 UTC**. The repository remained private and the existing draft target and asset IDs/digests were unchanged; no release was published. [Recorded setting change](immutable-releases-enabled-20260912.json). Recheck the setting after any visibility change and immediately before publication; the draft's assets become immutable only when it is published.

Complete every required evidence field in [candidate.md](candidate.md), leaving public-install/model checks explicitly pending. Root reviews those exact notes and the asset inventory before the future publication action. Publish the **existing** `v0.1.0` draft as a prerelease, explicitly not latest. Do not dispatch a new build, upload files, move the tag, create a replacement release or change package versions. The reviewed CLI operation is:

```sh
gh release edit v0.1.0 --repo IsmailKharoub/zavliq \
  --draft=false --prerelease=true --latest=false \
  --title 'Zavliq v0.1.0 — verification prerelease' \
  --notes-file /ABSOLUTE/REVIEWED/candidate-notes.md
```

Immediately record the resulting release ID, tag commit, immutable status, asset IDs/sizes/digests, prerelease status and publication time. Verify them against the reviewed inventory. Explicit versioned downloads work for a published prerelease; this workflow does not use a `latest` redirect or rename the tag to `v0.1.0-rc1`. See [GitHub's release editor](https://cli.github.com/manual/gh_release_edit).

## 3. Measure the actual public-install path

Run the [published-release onboarding procedure](../operations/onboarding-trials.md#published-release-verification) against the exact `v0.1.0` release and reviewed manifest digest. First confirm the published downloads are accessible without GitHub credentials and the service origin is exactly `https://zavliq.com`. Use fresh per-trial install roots and the actually downloaded binary, wheel, Node MCP bundle and skill; any checkout fallback fails the attempt.

Run all ten numbered attempts across the two providers and SDK/MCP, preserving every outcome. Installation and model time count toward each five-minute limit. At least nine must complete the independently verified exchange, with successful coverage of both providers/interfaces. The prerelease flag does not change the existing runner's exact-version contract. Keep its model allowlist and prompts unchanged: no shell/file/network tools, hidden helpers or corrective hints. The operator performs only the documented installation and deterministic peer setup.

Keep the existing cumulative $2 Bedrock ledger, including earlier failures and unknown reservations. Check the remaining balance and current prices before calls. If funds are insufficient, stop rather than reset the ledger or silently raise the ceiling. Plan the ordinary three-registrations-per-IP/hour admission windows for the fixture peer and ten fresh identities; do not pre-enroll trial identities or relax public limits. Repeat an anonymous install/import/MCP-readiness smoke on the other supported platform without models or accounts. Earlier local 10/10 results remain explicitly scoped to preinstalled clients.

Keep the deployed candidate stable during the trials. A service/configuration change that affects a gate needs a recorded impact review and appropriate rerun; backend, load or crypto results from an earlier candidate cannot be silently carried forward. Keep the release marked as a prerelease and its authoritative notes labeled verification-only throughout this interval. The static `/status` link continues to direct users to that record.

## 4. Promote metadata only after every gate passes

Review the complete requirements and actual final evidence. Resolve remaining gaps; installation success does not replace load, recovery, E2EE, TLS, monitoring or soak evidence. Create final notes containing the canonical URL, exact version/commit/manifest, platform scope, all gate evidence, preserved failures, limitations, upgrade/rollback guidance and a promotion timestamp. Link later evidence by its own exact commit without changing the application tag. Root reviews those notes before promotion.

Recheck the release ID, tag, immutable flag, asset IDs and manifest digest against the publication inventory. Then update only the same release's title, notes and prerelease/latest flags:

```sh
gh release edit v0.1.0 --repo IsmailKharoub/zavliq \
  --prerelease=false --latest=true \
  --title 'Zavliq v0.1.0' \
  --notes-file /ABSOLUTE/REVIEWED/launch-notes.md
```

Verify that all tag/asset/hash identities still match and append the promotion result to the evidence. The updated GitHub release metadata/notes now record launch, and `/status` continues to point to that authoritative record; no site phase variable or static-copy replacement is required. This step requires no rebuild, upload, retag or install migration. The first genuine service identities and adopter outreach remain post-launch work requiring their existing explicit scope; controlled fixture identities must never be represented as organic users.

## Failed candidates

An unsuccessful attempt stays in its numbered result and the cumulative ledger. If the same unchanged assets can be retried for a documented external/transient reason, use a separate approved run and preserve the failed run; never cherry-pick a passing ten by deleting failures. If an asset, packaged skill, installer or binary needs correction, leave `v0.1.0` unlaunched and describe the defect in its notes. Produce a new version, run the relevant backend/product gates and repeat a complete matching published-install trial set. Do not overwrite, delete/recreate or reuse the published tag, even if no outside downloads are known. Immutable assets also mean post-publication evidence must be linked from notes; it cannot be appended as a new release asset.
