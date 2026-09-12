"""External availability, host-health and off-host backup monitoring Lambda."""
import datetime as dt
from contextlib import contextmanager
import json
import os
import re
import signal
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import boto3
from botocore.config import Config

SDK_CONFIG = Config(connect_timeout=1, read_timeout=2,
                    retries={'mode': 'standard', 'total_max_attempts': 1})
METRIC_RESERVE_MS = 5000
HTTP_TIMEOUT_SECONDS = 3
HTTP_TOTAL_BUDGET_MS = 9000
AWS_REQUEST_BUDGET_MS = 3000
MAX_JSON_BYTES = 65536


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def http_error_302(self, request, response, code, message, headers):
        response.close()
        raise ValueError('Health endpoint must not redirect')

    http_error_301 = http_error_303 = http_error_307 = http_error_308 = http_error_302


HTTP = urllib.request.build_opener(NoRedirect())


def require_probe_time(context, request_ms):
    if context.get_remaining_time_in_millis() < METRIC_RESERVE_MS + request_ms:
        raise TimeoutError('Probe budget exhausted; reserve remaining time for metrics')


@contextmanager
def http_deadline(context):
    # Python Lambda invokes the handler on the main thread of a Linux process.
    # Socket inactivity timeouts alone do not bound DNS, headers, or a slowly
    # progressing body. A timer covers the entire request, including those paths.
    require_probe_time(context, HTTP_TOTAL_BUDGET_MS)
    if threading.current_thread() is not threading.main_thread() or signal.getitimer(signal.ITIMER_REAL) != (0.0, 0.0):
        raise RuntimeError('HTTP deadline unavailable')

    def expired(signum, frame):
        raise TimeoutError('HTTP probe deadline exceeded')

    previous = signal.signal(signal.SIGALRM, expired)
    try:
        signal.setitimer(signal.ITIMER_REAL, HTTP_TOTAL_BUDGET_MS / 1000)
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def read_json(origin, path, context):
    parsed = urllib.parse.urlsplit(origin)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username is not None or parsed.password is not None or parsed.path or parsed.query or parsed.fragment:
        raise ValueError('Probe origin must be an HTTPS origin')
    with http_deadline(context):
        try:
            response = HTTP.open(origin + path, timeout=HTTP_TIMEOUT_SECONDS)
        except urllib.error.HTTPError as error:
            error.close()
            raise
        with response:
            length = response.headers.get('Content-Length')
            if length is not None and not 0 <= int(length) <= MAX_JSON_BYTES:
                raise ValueError('Probe JSON exceeds limit')
            body = bytearray()
            while True:
                require_probe_time(context, HTTP_TIMEOUT_SECONDS * 1000)
                chunk = response.read1(min(8192, MAX_JSON_BYTES + 1 - len(body)))
                if not chunk:
                    break
                body.extend(chunk)
                if len(body) > MAX_JSON_BYTES:
                    raise ValueError('Probe JSON exceeds limit')
            value = json.loads(body)
            if not isinstance(value, dict):
                raise ValueError('Probe JSON must be an object')
            return value


def host_healthy(report, now):
    checked = report.get('checked_at')
    return report.get('ok') is True and type(checked) is int and -60 <= now - checked < 300


def backup_fresh(objects, now):
    for obj in objects.get('Contents', []):
        try:
            key = obj.get('Key', '')
            if not re.fullmatch(r'daily/zavliq-[0-9]{8}T[0-9]{6}Z\.tar\.age', key) or obj.get('Size', 0) <= 100:
                continue
            created = dt.datetime.strptime(key[13:-8], '%Y%m%dT%H%M%SZ').replace(tzinfo=dt.timezone.utc).timestamp()
            # Filename creation time, not LastModified: old re-uploads and
            # implausibly future timestamps cannot make a backup fresh.
            if -60 <= now - created < 86400:
                return True
        except (TypeError, ValueError, AttributeError):
            continue
    return False


def failed(check, error):
    print(json.dumps({'check': check, 'error_class': type(error).__name__}))


def handler(event, context):
    origin = os.environ['ZAVLIQ_ORIGIN']
    values = {'Available': 0, 'BackupFresh': 0, 'HostHealthy': 0}
    try:
        read_json(origin, '/health', context)
        versions = read_json(origin, '/_matrix/client/versions', context)
        values['Available'] = int(bool(versions.get('versions')))
    except Exception as error:
        failed('availability', error)
    try:
        report = read_json(origin, '/_zavliq/health', context)
        values['HostHealthy'] = int(host_healthy(report, time.time()))
    except Exception as error:
        failed('host_health', error)
    try:
        require_probe_time(context, AWS_REQUEST_BUDGET_MS)
        now = time.time()
        cutoff = dt.datetime.fromtimestamp(now - 86400, dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        objects = boto3.client('s3', config=SDK_CONFIG).list_objects_v2(
            Bucket=os.environ['ZAVLIQ_BACKUP_BUCKET'], Prefix='daily/', MaxKeys=1000,
            StartAfter='daily/zavliq-' + cutoff + '.tar.age')
        # One recent-key page is ample for the twice-daily schedule. If an
        # unexpected/truncated listing supplies no fresh proof, publish zero.
        values['BackupFresh'] = int(backup_fresh(objects, time.time()))
    except Exception as error:
        failed('backups', error)
    try:
        boto3.client('cloudwatch', config=SDK_CONFIG).put_metric_data(Namespace='Zavliq', MetricData=[
            {'MetricName': name, 'Value': value, 'Unit': 'Count', 'Dimensions': [{'Name': 'Environment', 'Value': os.environ['ZAVLIQ_ENVIRONMENT']}]} for name, value in values.items()
        ])
    except Exception as error:
        failed('metric_emission', error)
        # A failed CloudWatch attempt must remain a failed invocation/missing
        # datapoint. The existing treat_missing_data=breaching alarms stay active.
        raise RuntimeError('METRIC_EMISSION_FAILED') from None
    print(json.dumps(values))
    return values
