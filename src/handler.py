# src/handler.py

import base64
import json
import os
import logging
import boto3

# Configure logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Initialize Boto3 clients
route53 = boto3.client('route53')
secretsmanager = boto3.client('secretsmanager')

def find_hosted_zone_id(hostname):
    """
    Finds the Hosted Zone ID that best matches the given hostname.
    It returns the ID of the longest matching public hosted zone.
    """
    paginator = route53.get_paginator('list_hosted_zones')
    response_iterator = paginator.paginate()

    best_match_id = None
    best_match_length = 0
    # Ensure hostname ends with a dot for consistent comparison with Route 53 zone names
    normalized_hostname = hostname if hostname.endswith('.') else f"{hostname}."

    for page in response_iterator:
        for zone in page['HostedZones']:
            # We are only interested in public hosted zones
            if zone['Config']['PrivateZone']:
                continue

            zone_name = zone['Name']
            # Check if the hostname ends with the zone name
            if normalized_hostname.endswith(zone_name):
                # The longer the zone name, the more specific the match
                if len(zone_name) > best_match_length:
                    best_match_length = len(zone_name)
                    best_match_id = zone['Id'].split('/')[-1] # Extract just the ID

    if best_match_id:
        logger.info(f"Found best matching hosted zone ID '{best_match_id}' for hostname '{hostname}'")
    else:
        logger.warning(f"No suitable hosted zone found for hostname '{hostname}'")
    return best_match_id

def lambda_handler(event, context):
    """
    Updates a Route 53 'A' record, emulating a No-IP compatible DDNS provider.
    """
    logger.info(f"Received event: {json.dumps(event)}")

    headers_lower = {k.lower(): v for k, v in event.get('headers', {}).items()}

    # --- 1. Authentication ---
    try:
        # Get credentials from Secrets Manager
        secret_name = os.environ['DDNS_SECRET_NAME']
        secret_value = secretsmanager.get_secret_value(SecretId=secret_name)
        ddns_creds = json.loads(secret_value['SecretString'])
        ddns_user = ddns_creds['username']
        ddns_pass = ddns_creds['password']

        # Get credentials from request
        auth_header = headers_lower.get('authorization')
        if not auth_header or not auth_header.lower().startswith('basic '):
            logger.warning("Missing or invalid Authorization header")
            return {'statusCode': 401, 'body': 'badauth'}

        auth_creds_b64 = auth_header.split(' ')[1]
        decoded_creds = base64.b64decode(auth_creds_b64).decode('utf-8')
        username, password = decoded_creds.split(':', 1)

        # Compare credentials
        if username != ddns_user or password != ddns_pass:
            logger.warning("Invalid username or password")
            return {'statusCode': 401, 'body': 'badauth'}

    except Exception as e:
        logger.error(f"Authentication error: {e}")
        return {'statusCode': 401, 'body': 'badauth'}

    # --- 2. User-Agent Check ---
    user_agent = headers_lower.get('user-agent')
    if not user_agent:
        logger.warning("Missing User-Agent header")
        return {'statusCode': 400, 'body': 'badagent'}


    # --- 3. Get Parameters ---
    hostname = None
    new_ip = None
    http_method = event.get('httpMethod')

    if http_method == 'GET':
        params = event.get('queryStringParameters', {})
        hostname = params.get('hostname')
        new_ip = params.get('myip', event.get('requestContext', {}).get('http', {}).get('sourceIp'))
        if not hostname or not new_ip:
            logger.error("Missing 'hostname' or 'myip' query parameters for GET request.")
            return {'statusCode': 400, 'body': 'badreq'}
    elif http_method == 'PUT':
        try:
            if 'body' not in event or not event['body']:
                logger.error("Missing request body for PUT request.")
                return {'statusCode': 400, 'body': 'badreq'}

            body = json.loads(event['body'])
            hostname = body.get('hostname')
            # Prioritize 'myip' from body, fallback to source IP
            new_ip = body.get('myip', event.get('requestContext', {}).get('http', {}).get('sourceIp'))
            if not hostname or not new_ip:
                logger.error("Missing 'hostname' or 'myip' parameters in request body for PUT request.")
                return {'statusCode': 400, 'body': 'badreq'}

        except json.JSONDecodeError:
            logger.error("Invalid JSON in request body for PUT request.")
            return {'statusCode': 400, 'body': 'badreq'}
        except Exception as e:
            logger.error(f"Error parsing request body for PUT request: {e}")
            return {'statusCode': 400, 'body': 'badreq'}
    else:
        logger.error(f"Unsupported HTTP method: {http_method}")
        return {'statusCode': 405, 'body': 'methodnotallowed'}

    # Dynamically find the Hosted Zone ID
    hosted_zone_id = find_hosted_zone_id(hostname)
    if not hosted_zone_id:
        logger.error(f"Could not determine Hosted Zone ID for hostname: {hostname}")
        return {'statusCode': 500, 'body': '911'} # Internal server error

    # --- 4. Check Current DNS Record ---
    try:
        response = route53.list_resource_record_sets(
            HostedZoneId=hosted_zone_id,
            StartRecordName=hostname,
            StartRecordType='A',
            MaxItems='1'
        )
        record_sets = response.get('ResourceRecordSets', [])
        if record_sets and record_sets[0]['Name'] == f"{hostname}.":
            current_ip = record_sets[0]['ResourceRecords'][0]['Value']
            if current_ip == new_ip:
                logger.info(f"IP address for {hostname} is already {new_ip}. No change needed.")
                return {'statusCode': 200, 'body': f"nochg {new_ip}"}
    except Exception as e:
        logger.warning(f"Could not retrieve current DNS record for {hostname}: {e}")
        # Proceed to update anyway

    # --- 5. Update DNS Record ---
    logger.info(f"Attempting to update {hostname} to {new_ip} in zone {hosted_zone_id}")
    try:
        route53.change_resource_record_sets(
            HostedZoneId=hosted_zone_id,
            ChangeBatch={
                'Comment': 'Dynamic DNS update from UniFi',
                'Changes': [
                    {
                        'Action': 'UPSERT',
                        'ResourceRecordSet': {
                            'Name': hostname,
                            'Type': 'A',
                            'TTL': 300,
                            'ResourceRecords': [{'Value': new_ip}],
                        },
                    }
                ],
            },
        )
        logger.info(f"Route 53 update successful for {hostname} to {new_ip}")
        return {'statusCode': 200, 'body': f"good {new_ip}"}

    except Exception as e:
        logger.error(f"Route 53 update failed: {e}")
        return {'statusCode': 500, 'body': '911'}
