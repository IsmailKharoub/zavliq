"""External availability, host-health and off-host backup monitoring Lambda."""
import datetime as dt
import json
import os
import time
import urllib.request
import boto3


def read_json(origin, path):
    with urllib.request.urlopen(origin + path, timeout=8) as response:
        return json.load(response)


def handler(event, context):
    origin = os.environ['ZAVLIQ_ORIGIN']
    values = {'Available': 0, 'BackupFresh': 0, 'HostHealthy': 0}
    try:
        read_json(origin, '/health')
        versions = read_json(origin, '/_matrix/client/versions')
        values['Available'] = int(bool(versions.get('versions')))
    except Exception:
        pass
    try:
        report = read_json(origin, '/_zavliq/health')
        values['HostHealthy'] = int(report.get('ok') is True and time.time() - report.get('checked_at', 0) < 300)
    except Exception:
        pass
    try:
        newest = None
        for page in boto3.client('s3').get_paginator('list_objects_v2').paginate(Bucket=os.environ['ZAVLIQ_BACKUP_BUCKET'], Prefix='daily/'):
            for obj in page.get('Contents', []):
                if obj['Key'].endswith('.tar.age') and obj['Size'] > 100:
                    # Re-uploading an old archive must not refresh its recovery age.
                    name = obj['Key'].rsplit('/', 1)[-1]
                    created = dt.datetime.strptime(name[7:-8], '%Y%m%dT%H%M%SZ').replace(tzinfo=dt.timezone.utc)
                    newest = max(newest or created, created)
        values['BackupFresh'] = int(newest is not None and (dt.datetime.now(dt.timezone.utc) - newest).total_seconds() < 86400)
    except Exception:
        pass
    boto3.client('cloudwatch').put_metric_data(Namespace='Zavliq', MetricData=[
        {'MetricName': name, 'Value': value, 'Unit': 'Count', 'Dimensions': [{'Name': 'Environment', 'Value': os.environ['ZAVLIQ_ENVIRONMENT']}]} for name, value in values.items()
    ])
    print(json.dumps(values))
    return values
