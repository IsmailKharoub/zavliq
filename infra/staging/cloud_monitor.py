"""Read-only private-stage telemetry monitor. Never reads messaging databases."""
import json
import os
import re
import time

import boto3
from botocore.config import Config

SDK_CONFIG = Config(connect_timeout=1, read_timeout=2, retries={'mode': 'standard', 'total_max_attempts': 1})


def health_metrics(report, now):
    checked = report.get('checked_at')
    issued = report.get('capability_issued_at')
    expiry = report.get('capability_expires_at')
    times_valid = all(type(value) is int for value in (checked, issued, expiry))
    fresh = times_valid and -60 <= now - checked <= 180
    capability = times_valid and 0 < expiry - issued <= 172800 and issued <= now and expiry - now >= 3600
    return {'HostHealthy': int(fresh and report.get('ok') is True), 'CapabilityValid': int(capability)}


def backup_fresh(metadata, now):
    try:
        created = int(metadata['created-at'])
        return bool(re.fullmatch(r'[a-f0-9]{64}', metadata.get('sha256', ''))) and -60 <= now - created <= 86400
    except (ValueError, KeyError, TypeError):
        return False


def handler(event, context):
    now = int(time.time())
    bucket = os.environ['ZAVLIQ_BACKUP_BUCKET']
    s3 = boto3.client('s3', config=SDK_CONFIG)
    metrics = {'HostHealthy': 0, 'CapabilityValid': 0, 'BackupFresh': 0}
    try:
        response = s3.get_object(Bucket=bucket, Key='status/health.json')
        try:
            if response['ContentLength'] > 65536:
                raise ValueError('Health object exceeds limit')
            body = response['Body'].read(65537)
        finally:
            response['Body'].close()
        if len(body) > 65536:
            raise ValueError('Health object exceeds limit')
        metrics.update(health_metrics(json.loads(body), now))
    except Exception as error:
        print(json.dumps({'check': 'health_object', 'error_class': type(error).__name__}))
    try:
        objects = s3.list_objects_v2(Bucket=bucket, Prefix='daily/', MaxKeys=1000)
        candidates = [item for item in objects.get('Contents', []) if re.fullmatch(r'daily/zavliq-\d{8}T\d{6}Z\.tar\.age', item['Key']) and 0 < item['Size'] <= 256 * 1024 * 1024]
        for item in sorted(candidates, key=lambda item: item['Key'], reverse=True)[:4]:
            if context.get_remaining_time_in_millis() < 5000:
                break
            try:
                metadata = s3.head_object(Bucket=bucket, Key=item['Key']).get('Metadata', {})
                if backup_fresh(metadata, now):
                    metrics['BackupFresh'] = 1
                    break
            except Exception as error:
                print(json.dumps({'check': 'backup_head', 'error_class': type(error).__name__}))
    except Exception as error:
        print(json.dumps({'check': 'backup_objects', 'error_class': type(error).__name__}))
    boto3.client('cloudwatch', config=SDK_CONFIG).put_metric_data(Namespace='ZavliqStaging', MetricData=[
        {'MetricName': name, 'Value': value, 'Unit': 'Count', 'Dimensions': [{'Name': 'Environment', 'Value': 'private-staging'}]}
        for name, value in metrics.items()
    ])
    report = {'checked_at': now, **metrics}
    print(json.dumps(report))
    return report
