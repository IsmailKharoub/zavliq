# Website-only releases

The public application bundle remains immutable. A website release records its own source revision and gateway image under `/opt/zavliq/websites/WEBSITE_ID`; `/opt/zavliq/current`, `/etc/zavliq/compose.env`, the application revision, published v0.1.0 artifacts and the base public manifest stay unchanged. Only `/etc/zavliq/website-active.json` selects the optional gateway override.

`infra/production/website.py` packages, activates and rolls back this single service. `website_state.py` validates the override for the existing Compose wrapper and health check. Synapse, control, PostgreSQL and Echo retain their exact image pins, container IDs, start times and mounts. The tool neither restarts these services nor pauses writers, modifies timers, provisions identities or changes volumes. Gateway recreation briefly interrupts public HTTP connections; persistent messaging clients can reconnect. Staging is not involved.

## Prepare a reviewed artifact

Use a fresh Git archive from the separately reviewed website commit, with its full commit ID and SHA256. Do not replace the base source archive. The local default Docker builder must be exclusively assigned; `desktop-linux` is also accepted only when it resolves to the same daemon as that builder. Output targets `linux/amd64`, including when the local host uses emulation. Node and Caddy are selected through the reviewed immutable digest/expected image-ID input, never floating tag updates. These images and the exact original production gateway must already be cached.

The original gateway can be obtained from the retained, checksum-verified public bundle. For a gateway-only cache import, preserve the selected OCI index/manifest/config/layer graph and its legacy tag mapping, verify the resulting archive with `verify_docker_archive`, and load only that image. Retain the original archive and the extraction proof. Docker's `inspect --platform linux/amd64` can report a selected manifest ID while ordinary inspect reports its parent index ID; keep the original reviewed index pin and separately verify the selected architecture.

The generated Dockerfile copies old `/srv/assets` from the exact cached original gateway before copying the newly built site. Every previous content-addressed asset must remain byte-identical, allowing already-open consoles to load their previous worker/chunks. An upgrade over a later website also refuses to proceed unless all of that currently active website's assets remain in the new artifact. The first preparer inherits assets from the base gateway; a later release needing additional intermediate assets requires an explicit preparation update, not a bypass of this guard.

```sh
python3 -B infra/production/website.py prepare \
  --source /PRIVATE/new-website-source.tar.gz \
  --source-sha256 REVIEWED_SOURCE_SHA256 \
  --revision EXACT_WEBSITE_COMMIT \
  --base-manifest /PRIVATE/production-manifest.json \
  --base-manifest-sha256 REVIEWED_BASE_MANIFEST_SHA256 \
  --web-bases /PRIVATE/reviewed-web-bases.json \
  --web-bases-sha256 REVIEWED_BASE_PINS_SHA256 \
  --output /PRIVATE/FRESH_PREPARED_DIRECTORY \
  --exclusive-builder
```

The completed manifest is written last and records source, generated Dockerfile, Caddy configuration, base pins, preparer, full static-file hashes, preserved asset hashes, exact new gateway image and image-archive hash. Image smoke checks use no network or mounts. Failed preparations are retained and must not be silently reused. Review the resulting `website-manifest.json` and its independent hash before copying its directory to `/opt/zavliq/websites/WEBSITE_ID` with root ownership and no group/other write permission.

## Install tools and activate

Preserve prior operator-tool bytes and hashes. Install reviewed `website_state.py` and `website.py` first, then the compatible `prepare_bundle.py`, `activate.py` and `restore.py` under `/opt/zavliq/production-tools/` as root-owned files without group/other write access. Install `activate.py` after its dependencies, so a scheduled health invocation never imports a missing module. Verify every installed hash; do not copy these tools into a frozen application or website bundle. The inactive full-deployment workflow also checks the new dependencies, and ordinary full activation refuses an active website override until an explicit reconciliation has been reviewed.

The stats collector is installed separately through `infra/stats`. It writes only its strict aggregate schema to `/var/lib/zavliq/stats.json`. The existing gateway read-only status mount exposes that file only through the exact `/_zavliq/stats` route, with `Cache-Control: no-store`. No browser query can access a database, a private classifier, another status file or a credential. A snapshot must already be valid and recent before website activation.

```sh
sudo python3 -B /opt/zavliq/production-tools/website.py activate \
  --website-id REVIEWED_WEBSITE_ID \
  --manifest-sha256 REVIEWED_WEBSITE_MANIFEST_SHA256 \
  --base-manifest-sha256 CURRENT_BASE_MANIFEST_SHA256 \
  --expected-current-website none
```

For a subsequent website transition, replace `none` with the exact active website manifest hash. The tool validates the merged Compose configuration, loads the verified gateway-only archive and runs `up -d --no-deps --no-build gateway` with exact IDs and `pull_policy: never`. It checks canonical TLS/discovery, the exact newly packaged `/stats` HTML, the aggregate schema and snapshot freshness (15 minutes, with 60 seconds of future-clock tolerance), and unchanged backend containers. Production health resolves the same gateway pin and includes stats freshness in its public HTTPS check. A stale collector does not turn into zero usage or a healthy data feed.

Failures retain a private deployment report and attempt a gateway-only rollback. The failed/activating marker remains until the previous gateway is actually healthy. After an interrupted transition, ordinary activation is refused; explicit rollback accepts only its journaled predecessor. A crash during rollback leaves the same bounded recovery path available. A failed interrupted recovery does not restart a known failed website automatically.

```sh
# Return to the immutable base gateway after the first website release.
sudo python3 -B /opt/zavliq/production-tools/website.py rollback \
  --website-id base --manifest-sha256 none \
  --base-manifest-sha256 CURRENT_BASE_MANIFEST_SHA256 \
  --expected-current-website ACTIVE_OR_INTERRUPTED_WEBSITE_MANIFEST_SHA256
```

A journaled earlier website uses its exact ID/hash instead of `base`/`none`. Gateway rollback cannot restore data, recall accepted messages or downgrade database schemas. Clients with a page from the withdrawn website may need to reload after rollback.

## Off-host retention and fresh-host recovery

Preserve each reviewed completed website directory and manifest independently in the existing private artifact bucket. Use a fresh key below `release-bundles/websites/WEBSITE_REVISION/MANIFEST_SHA256/`, outside the expiring `daily/` backup prefix. Archive only the website package, never runtime stores or private classifier/credentials. Record archive SHA256 and byte length; use private access, TLS and AES256 server-side encryption, verify S3 SHA256 checksum and size, and refuse to overwrite a mismatched object. Keep the current website and its required predecessor packages. The source Git commit alone does not replace the verified image package.

Routine database backups intentionally retain the base `compose.env`, secrets and writer stores; they do **not** contain the website-active marker, website image/package, stats collector or classifier. Restore remains a two-part procedure:

1. Establish the final snapshot's exact four writer-image pins against the unchanged base bundle. Record the separately active gateway/website manifest as an additional fact. The restore input's `images` field identifies the reviewed **base restore image set**, including its original gateway; do not mislabel it as an observation of the serving website gateway. Do not substitute a new gateway ID into the base manifest or restore receipt to make it pass.
2. Restore the base bundle and original device/server state on the isolated replacement through the existing recovery procedure. Complete its original-device/backend verification and adoption requirements using that base website. A new website cannot bypass an unfinished backend restore because activation requires the base deployment to be ready.
3. Independently retrieve/check the retained website package, reinstall the reviewed website operator dependencies, and restore the separately protected classifier/collector configuration. The collector takes a fresh snapshot; do not restore old aggregate data as if it were current. Reapply the same reviewed website using the gateway-only tool with `expected-current-website none`.
4. Verify the intended current website/stats routes and content before considering the replacement's final UI check complete or announcing its website recovery. Retain both backend-recovery and website-reapplication evidence.

This is an explicit recovery ordering, not a claim that an existing server backup automatically preserves the newer website. The new website path has offline transition tests; an actual fresh-host reapplication remains a separate verification fact until performed.
