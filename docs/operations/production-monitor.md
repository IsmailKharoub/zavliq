# Production monitoring bounds

`infra/scripts/cloud-monitor.py` is the separate public production Lambda. Its source changes require the production Terraform plan and deployment review; local tests do not establish deployed monitoring or SNS delivery. The private-stage monitor remains separate.

The existing 40-second invocation, five-minute schedule, `Zavliq` namespace, `Environment` dimension, and three metrics remain unchanged:

- `Available`: the configured HTTPS origin's `/health` returns a JSON object, and `/_matrix/client/versions` advertises versions.
- `HostHealthy`: `/_zavliq/health` reports `ok: true` with an integer timestamp less than five minutes old, allowing at most 60 seconds of clock skew into the future.
- `BackupFresh`: the configured backup bucket contains a matching archive less than 24 hours old, allowing the same 60-second clock skew.

Each HTTPS probe has a three-second socket timeout, a nine-second deadline covering connection, headers, and the entire response, and a 64 KiB JSON limit. Redirects fail the probe, including redirects to another HTTPS URL. Python's default HTTPS certificate verification is retained. A probe starts only when its full deadline plus a five-second metric reserve remains; body reads also recheck the remaining invocation time. Response streams are closed on completion and failure.

The complete HTTP deadline uses `SIGALRM` in the Python Lambda's main Linux thread. If invoked on another thread or with an existing real-time alarm, HTTP probes fail closed without replacing that alarm. The prior signal handler is restored after each probe. A process/runtime failure can still prevent metric publication; the unchanged alarms treat missing datapoints as breaching.

S3 and CloudWatch use a one-second connect timeout, two-second read timeout, and one total request attempt. S3 gets one page of at most 1,000 keys from the existing bucket's `daily/` prefix, starting after the 24-hour timestamp cutoff. This matches `fetch-backup.py` uploads exactly: `daily/zavliq-YYYYMMDDTHHMMSSZ.tar.age`. An archive must have a valid timestamp and exceed 100 bytes. Checksum sidecars, arbitrary objects, malformed dates, stale reuploads, and implausibly future dates do not count. `LastModified` does not establish freshness.

A fresh matching archive in that page is sufficient evidence. If a truncated page contains no such archive, `BackupFresh` is zero even when an omitted later page might contain one. This conservative failure avoids both unbounded pagination and a false success. The monitor uses only the existing `s3:ListBucket` grant; it does not download, decrypt, or establish the recoverability of the backup. Restore drills provide that separate proof.

Failed or skipped probes remain zero. After the probes, the handler attempts one CloudWatch publication containing all three metrics. A publication failure raises `METRIC_EMISSION_FAILED`; it never returns a successful monitoring result. Logs contain only check names, exception class names, and metric values. AWS socket timeouts and reserved time make a bounded publication attempt possible; they cannot guarantee CloudWatch availability or delivery under an interrupted runtime. Existing missing-data alarms cover absent telemetry without additional IAM grants.

The isolated tests mock AWS clients and HTTPS transport, exercise the actual redirect handler and whole-request timer, and include a slowly progressing response, exhausted invocation budgets, truncated S3 listings, stale backup timestamps, and failed metric emission. Run with boto3 installed from `infra/staging/requirements.txt`:

```sh
python -m unittest discover -s infra/staging/tests -p 'test_production_monitor.py' -v
```

The test file lives in the existing boto3-enabled CI test step. It does not invoke AWS, resolve public hosts, or require credentials.
