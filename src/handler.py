# src/handler.py

import base64
import json
import os
import logging
import boto3
import re

# Configure logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Initialize Boto3 clients
route53 = boto3.client('route53')
secretsmanager = boto3.client('secretsmanager')


def _generate_policy(principal_id, effect, resource):
    """
    Helper function to generate an IAM policy.
    """
    return {
        'principalId': principal_id,
        'policyDocument': {
            'Version': '2012-10-17',
            'Statement': [{
                'Action': 'execute-api:Invoke',
                'Effect': effect,
                'Resource': resource,
            }]
        }
    }


def authorizer_handler(event, context):
    """
    Handles API Gateway Lambda authorizer requests.
    This function performs HTTP Basic authentication.
    """
    logger.info(f"Authorizer event: {json.dumps(event)}")
    auth_header = event.get('headers', {}).get('Authorization')
    
    if not auth_header or not auth_header.lower().startswith('basic '):
        logger.warning("Missing or invalid Authorization header")
        # In case of no auth header, API Gateway returns a 401 Unauthorized
        # response. We don't need to explicitly return a policy.
        # However, if you want to return a custom response, you can do so here.
        # For simplicity, we let API Gateway handle it.
        # To deny, you can `raise Exception('Unauthorized')`
        return _generate_policy('user', 'Deny', event['methodArn'])

    try:
        # Get credentials from Secrets Manager
        secret_name = os.environ['DDNS_SECRET_NAME']
        secret_value = secretsmanager.get_secret_value(SecretId=secret_name)
        ddns_creds = json.loads(secret_value['SecretString'])
        ddns_user = ddns_creds['username']
        ddns_pass = ddns_creds['password']

        # Get credentials from request
        auth_creds_b64 = auth_header.split(' ')[1]
        decoded_creds = base64.b64decode(auth_creds_b64).decode('utf-8')
        username, password = decoded_creds.split(':', 1)

        # Compare credentials
        if username == ddns_user and password == ddns_pass:
            logger.info("Authentication successful")
            return _generate_policy(username, 'Allow', event['methodArn'])
        else:
            logger.warning("Invalid username or password")
            return _generate_policy(username, 'Deny', event['methodArn'])

    except Exception as e:
        logger.error(f"Authentication error in authorizer: {e}")
        # Raising an exception will cause a 500 Internal Server Error response
        # from API Gateway. For security, it's better to return a Deny policy.
        return _generate_policy('user', 'Deny', event['methodArn'])


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

    # --- 1. User-Agent Check ---
    user_agent = headers_lower.get('user-agent')
    if not user_agent:
        logger.warning("Missing User-Agent header")
        return {'statusCode': 400, 'body': 'badagent'}


    # --- 2. Get Parameters ---
    hostname = None
    new_ip = None
    params = event.get('queryStringParameters') if event.get('queryStringParameters') is not None else {}
    
    # Standard dyndns2 behavior: get 'hostname' from query params
    hostname = params.get('hostname')
    
    # Fallback for inadyn custom type: parse from path
    # e.g., path can be /update/your.hostname.com or /updatedrohalloran.net from user log
    if not hostname:
        path = event.get('path', '')
        # This regex will look for a domain name-like string in the path
        match = re.search(r'([a-zA-Z0-9.-]+\.[a-zA-Z]{2,})', path)
        if match:
            hostname = match.group(1)
            logger.info(f"Extracted hostname '{hostname}' from path '{path}'")

    # Get IP address
    # Inadyn can send 'myip' in query string
    # Also handle source IP from API Gateway context
    new_ip = params.get('myip', event.get('requestContext', {}).get('identity', {}).get('sourceIp'))

    if not hostname or not new_ip:
        error_msg = "Missing 'hostname' or 'myip' parameters."
        logger.error(error_msg)
        return {'statusCode': 400, 'body': 'badreq'}

    # Dynamically find the Hosted Zone ID
    hosted_zone_id = find_hosted_zone_id(hostname)
    if not hosted_zone_id:
        logger.error(f"Could not determine Hosted Zone ID for hostname: {hostname}")
        return {'statusCode': 500, 'body': '911'} # Internal server error

    # --- 3. Check Current DNS Record ---
    try:
        response = route53.list_resource_record_sets(
            HostedZoneId=hosted_zone_id,
            StartRecordName=hostname,
            StartRecordType='A',
            MaxItems='1'
        )
        record_sets = response.get('ResourceRecordSets', [])
        # Ensure the record found is an exact match for the hostname
        if record_sets and record_sets[0]['Name'] == f"{hostname}.":
            current_ip = record_sets[0]['ResourceRecords'][0]['Value']
            if current_ip == new_ip:
                logger.info(f"IP address for {hostname} is already {new_ip}. No change needed.")
                return {'statusCode': 200, 'body': f"nochg {new_ip}"}
    except Exception as e:
        logger.warning(f"Could not retrieve current DNS record for {hostname}: {e}")
        # Proceed to update anyway

    # --- 4. Update DNS Record ---
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
