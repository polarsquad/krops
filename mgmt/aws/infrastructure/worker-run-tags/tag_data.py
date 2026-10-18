#!/usr/bin/env python3
"""Validate AWS JSON and pass exact lifecycle values to Bash as NUL fields."""
from datetime import datetime
import json
import re
import sys

KEYS = ('krops.io/run-id', 'krops.io/revision', 'krops.io/expires-at', 'krops.io/run-kind')
# CAPA v2.13.1 Go RE2 \s permits tab/LF/FF/CR/space, excluding vertical tab.
VALUE = re.compile(r'[a-zA-Z0-9\t\n\f\r +=._:/@-]{1,256}', re.ASCII)
EXPIRY = re.compile(r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z', re.ASCII)


def cluster_fields(response):
    tags = response['cluster']['tags']
    if not isinstance(tags, dict):
        raise ValueError('cluster tags must be an object')
    values = []
    for key in KEYS:
        value = tags.get(key)
        if not isinstance(value, str) or not VALUE.fullmatch(value):
            raise ValueError(f'{key} must be a nonempty CAPA-compatible ASCII tag value')
        values.append(value)
    expiry = values[2]
    if expiry != 'never':
        if not EXPIRY.fullmatch(expiry):
            raise ValueError('expires-at must be never or an exact UTC timestamp')
        datetime.strptime(expiry, '%Y-%m-%dT%H:%M:%SZ')
    payload = json.dumps([{'Key': key, 'Value': value} for key, value in zip(KEYS, values)])
    return [*values, payload]


def instance_fields(response):
    reservations = response['Reservations']
    if not isinstance(reservations, list):
        raise ValueError('Reservations must be an array')
    instances = []
    for reservation in reservations:
        batch = reservation['Instances']
        if not isinstance(batch, list):
            raise ValueError('Instances must be an array')
        instances.extend(batch)
    if not instances:
        return []
    if len(instances) != 1:
        raise ValueError('expected exactly one worker instance')
    instance = instances[0]
    tags = {}
    for tag in instance.get('Tags', []):
        key, value = tag['Key'], tag['Value']
        if not isinstance(key, str) or not isinstance(value, str) or key in tags:
            raise ValueError('invalid or duplicate instance tag')
        tags[key] = value
    fields = [instance['InstanceId'], instance['State']['Name'],
              tags.get('aws:eks:cluster-name', ''), *(tags.get(key, '') for key in KEYS)]
    if any(not isinstance(value, str) or '\0' in value for value in fields):
        raise ValueError('instance fields must be NUL-free strings')
    return fields


def main():
    try:
        response = json.load(sys.stdin)
        fields = {'cluster': cluster_fields, 'instance': instance_fields}[sys.argv[1]](response)
        if fields:
            sys.stdout.buffer.write(b'\0'.join(value.encode('utf-8') for value in fields) + b'\0')
    except (ValueError, KeyError, TypeError, IndexError) as error:
        print(f'Invalid AWS tag data: {error}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
