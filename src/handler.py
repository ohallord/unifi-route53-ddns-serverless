# src/handler.py
"""No-IP / dyndns2 compatible DDNS endpoint backed by Route 53.

Security notes:
- Request events are never logged in full: they carry the Basic auth header.
- Only the hostname configured at deploy time (DDNS_HOSTNAME) may be updated.
- Credentials are compared in constant time.
"""

import base64
import binascii
import hmac
import ipaddress
import json
import logging
import os
import re
import time

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

route53 = boto3.client('route53')
secretsmanager = boto3.client('secretsmanager')

SECRET_CACHE_TTL_SECONDS = 300
_secret_cache = {}

# RFC 1123 hostname: labels of 1-63 alnum/hyphen chars, not starting/ending with '-'.
_HOSTNAME_RE = re.compile(
    r'^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$'
)
_PATH_HOSTNAME_RE = re.compile(r'([a-zA-Z0-9.-]+\.[a-zA-Z]{2,})')


class Unauthorized(Exception):  # noqa: N818
    """API Gateway maps a Lambda error whose message is exactly 'Unauthorized' to HTTP 401."""

    def __init__(self):
        super().__init__('Unauthorized')


def _headers_lower(event):
    return {k.lower(): v for k, v in (event.get('headers') or {}).items()}


def _normalize_hostname(hostname):
    if not isinstance(hostname, str):
        return None
    hostname = hostname.strip().rstrip('.').lower()
    return hostname if _HOSTNAME_RE.match(hostname) else None


def _get_credentials():
    """Fetch DDNS credentials from Secrets Manager, cached per container."""
    now = time.monotonic()
    cached = _secret_cache.get('creds')
    if cached and now - cached[0] < SECRET_CACHE_TTL_SECONDS:
        return cached[1]
    secret = secretsmanager.get_secret_value(SecretId=os.environ['DDNS_SECRET_NAME'])
    data = json.loads(secret['SecretString'])
    creds = (str(data['username']), str(data['password']))
    _secret_cache['creds'] = (now, creds)
    return creds


def _api_wide_resource(method_arn):
    """arn:...:apiId/stage/VERB/path -> arn:...:apiId/stage/*/*

    The authorizer result is cached by Authorization header, so the policy must
    cover every method/path of the stage or cached Allows fail on other paths.
    """
    arn_prefix, _, path = method_arn.partition(':execute-api:')
    region_account_api, _, rest = path.partition('/')
    stage = rest.split('/', 1)[0]
    return f"{arn_prefix}:execute-api:{region_account_api}/{stage}/*/*"


def _allow_policy(principal_id, method_arn):
    return {
        'principalId': principal_id,
        'policyDocument': {
            'Version': '2012-10-17',
            'Statement': [{
                'Action': 'execute-api:Invoke',
                'Effect': 'Allow',
                'Resource': _api_wide_resource(method_arn),
            }],
        },
    }


def authorizer_handler(event, context):
    """API Gateway REQUEST authorizer performing HTTP Basic authentication.

    Raising 'Unauthorized' makes API Gateway return 401, which DDNS clients
    report as an authentication failure.
    """
    auth_header = _headers_lower(event).get('authorization') or ''
    scheme, _, encoded = auth_header.partition(' ')
    if scheme.lower() != 'basic' or not encoded.strip():
        logger.warning("Missing or non-Basic Authorization header")
        raise Unauthorized

    try:
        decoded = base64.b64decode(encoded.strip(), validate=True).decode('utf-8')
    except (binascii.Error, UnicodeDecodeError):
        logger.warning("Malformed Basic credentials")
        raise Unauthorized from None
    username, sep, password = decoded.partition(':')
    if not sep:
        logger.warning("Malformed Basic credentials")
        raise Unauthorized

    try:
        expected_user, expected_pass = _get_credentials()
    except Exception as e:  # fail closed
        logger.error(f"Could not load DDNS credentials: {type(e).__name__}")
        raise Unauthorized from None

    user_ok = hmac.compare_digest(username.encode(), expected_user.encode())
    pass_ok = hmac.compare_digest(password.encode(), expected_pass.encode())
    if user_ok and pass_ok:
        logger.info("Authentication successful")
        return _allow_policy(expected_user, event['methodArn'])

    logger.warning("Invalid username or password")
    raise Unauthorized


def find_hosted_zone_id(hostname):
    """Return the ID of the longest public hosted zone that contains hostname."""
    paginator = route53.get_paginator('list_hosted_zones')
    best_match_id = None
    best_match_length = 0
    normalized = f"{hostname}."

    for page in paginator.paginate():
        for zone in page['HostedZones']:
            if zone['Config']['PrivateZone']:
                continue
            zone_name = zone['Name'].lower()
            # Match on a label boundary so "badexample.com." never matches "example.com."
            if (normalized == zone_name or normalized.endswith('.' + zone_name)) \
                    and len(zone_name) > best_match_length:
                best_match_length = len(zone_name)
                best_match_id = zone['Id'].split('/')[-1]

    if best_match_id:
        logger.info(f"Found hosted zone '{best_match_id}' for '{hostname}'")
    else:
        logger.warning(f"No public hosted zone found for '{hostname}'")
    return best_match_id


def lambda_handler(event, context):
    """Update a Route 53 'A' record, emulating a No-IP compatible DDNS provider."""
    headers = _headers_lower(event)
    params = event.get('queryStringParameters') or {}
    logger.info(
        "Update request: path=%s hostname=%s myip=%s",
        event.get('path'), params.get('hostname'), params.get('myip'),
    )

    if not headers.get('user-agent'):
        logger.warning("Missing User-Agent header")
        return {'statusCode': 400, 'body': 'badagent'}

    allowed_hostname = _normalize_hostname(os.environ.get('DDNS_HOSTNAME'))
    if not allowed_hostname:
        logger.error("DDNS_HOSTNAME is not configured or invalid")
        return {'statusCode': 500, 'body': '911'}

    # Standard dyndns2: 'hostname' query param. Fallback (inadyn custom): parse from path.
    raw_hostname = params.get('hostname')
    if not raw_hostname:
        match = _PATH_HOSTNAME_RE.search(event.get('path') or '')
        raw_hostname = match.group(1) if match else None

    raw_ip = params.get('myip') or (event.get('requestContext') or {}).get('identity', {}).get('sourceIp')

    if not raw_hostname or not raw_ip:
        logger.error("Missing 'hostname' or 'myip' parameters")
        return {'statusCode': 400, 'body': 'badreq'}

    hostname = _normalize_hostname(raw_hostname)
    if hostname != allowed_hostname:
        logger.warning("Refusing update for hostname that is not configured")
        return {'statusCode': 400, 'body': 'nohost'}

    try:
        new_ip = str(ipaddress.IPv4Address(raw_ip))
    except (ipaddress.AddressValueError, ValueError):
        logger.warning("Invalid IPv4 address in request")
        return {'statusCode': 400, 'body': 'badreq'}

    hosted_zone_id = find_hosted_zone_id(hostname)
    if not hosted_zone_id:
        return {'statusCode': 500, 'body': '911'}

    try:
        response = route53.list_resource_record_sets(
            HostedZoneId=hosted_zone_id,
            StartRecordName=hostname,
            StartRecordType='A',
            MaxItems='1',
        )
        record_sets = response.get('ResourceRecordSets', [])
        if record_sets and record_sets[0]['Name'].lower() == f"{hostname}." \
                and record_sets[0].get('Type') == 'A':
            current_ip = record_sets[0]['ResourceRecords'][0]['Value']
            if current_ip == new_ip:
                logger.info(f"{hostname} is already {new_ip}; no change")
                return {'statusCode': 200, 'body': f"nochg {new_ip}"}
    except Exception as e:
        logger.warning(f"Could not read current record for {hostname}: {e}")

    logger.info(f"Updating {hostname} to {new_ip} in zone {hosted_zone_id}")
    try:
        route53.change_resource_record_sets(
            HostedZoneId=hosted_zone_id,
            ChangeBatch={
                'Comment': 'Dynamic DNS update',
                'Changes': [{
                    'Action': 'UPSERT',
                    'ResourceRecordSet': {
                        'Name': hostname,
                        'Type': 'A',
                        'TTL': 300,
                        'ResourceRecords': [{'Value': new_ip}],
                    },
                }],
            },
        )
        return {'statusCode': 200, 'body': f"good {new_ip}"}
    except Exception as e:
        logger.error(f"Route 53 update failed: {e}")
        return {'statusCode': 500, 'body': '911'}
