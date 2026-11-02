# Serverless Dynamic DNS (DDNS) for UniFi & Inadyn Clients

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE) ![Python](https://img.shields.io/badge/python-3.11-blue.svg) ![AWS Lambda](https://img.shields.io/badge/AWS-Lambda%20%7C%20API%20Gateway%20%7C%20Route%2053-orange.svg) ![Serverless Framework](https://img.shields.io/badge/IaC-Serverless%20Framework-red.svg)

> Self-hosted dynamic DNS for UniFi Dream Machine (UDM, UDM Pro, UDR, UCG) and USG gateways using AWS Route 53, with no DDNS subscription, no container and no script on your network. A serverless No-IP-compatible endpoint (Lambda + API Gateway + Secrets Manager) that also works with any inadyn client. It's a free alternative to No-IP, DynDNS and DuckDNS for people who already host their domain in Route 53.

This project provides a serverless, secure, and cost-effective Dynamic DNS (DDNS) endpoint on AWS. It is designed to be compatible with `inadyn`-based clients, most notably the Ubiquiti UniFi Dream Machine (UDM) series, by emulating the popular No-IP update protocol.

Instead of running a client script on your local network, your UniFi gateway can directly and securely update a Route 53 DNS record with its new public IP address.

## Features

*   **Serverless**: No servers to manage. The solution runs entirely on API Gateway and Lambda.
*   **Secure**: HTTP Basic credentials are checked by a Lambda authorizer against a generated password in **AWS Secrets Manager**. Only the one configured hostname can be updated, and the IAM role enforces that limit too.
*   **UniFi Native**: Works directly with the Dynamic DNS configuration in the UniFi Network application. No custom scripts or containers needed on your network.
*   **Infrastructure as Code**: All AWS resources (Lambda, API Gateway, IAM Roles, Secrets Manager Secret) are defined and deployed using the Serverless Framework.

## Architecture

1.  **UniFi Gateway**: Your UniFi UDM/USG detects a change in its public WAN IP address.
2.  **DDNS Update Request**: The UniFi device sends a No-IP compatible `GET` request to your secure API Gateway endpoint. The request includes a Basic Auth header with your DDNS username and password.
3.  **API Gateway**: Receives the request and triggers the AWS Lambda function.
4.  **AWS Lambda**: The Python Lambda function (`src/handler.py`) performs the following steps:
    *   A Lambda **authorizer** validates the Basic Auth header against credentials in **AWS Secrets Manager** (constant-time comparison; failures return `401`).
    *   The update function checks that the hostname matches the configured one and that the IP is a valid IPv4 address.
    *   Checks the current IP address in Route 53 to avoid unnecessary updates.
    *   If the IP has changed, it uses `boto3` to update the specified 'A' record in your Route 53 Hosted Zone.
    *   Returns a No-IP compatible response code (e.g., `good`, `nochg`, `nohost`, `badreq`) that the UniFi client understands.

## Prerequisites

Before you begin, ensure you have the following installed:

*   **Node.js & npm**: [Download and install Node.js](https://nodejs.org/) (which includes npm). The Serverless Framework requires Node.js.
*   **Serverless Framework CLI**:
    ```bash
    npm install -g serverless
    ```
*   **AWS CLI**: [Install and configure the AWS CLI](https://docs.aws.amazon.com/cli/latest/userguide/cli-chap-install.html). Ensure your AWS credentials are configured with permissions to create Lambda functions, API Gateway endpoints, IAM roles, and modify Route 53 and Secrets Manager resources.
    ```bash
    aws configure
    ```
*   **Python 3.11+**: Matches the Lambda runtime; needed only to run the tests.

## Configuration and Deployment

### Step 1: Deploy

Two parameters are required:

*   **`hostName`**: the fully qualified name to update, lowercase and without a trailing dot (e.g. `home.example.com`). Its parent zone must be a public hosted zone in the same AWS account. **This is the only record the endpoint can change.** Requests for any other hostname are refused (`nohost`), and the Lambda's IAM role can only `UPSERT` an `A` record with this exact name.
*   **`ddnsUsername`**: the username your gateway will send.

```bash
serverless deploy \
  --param="hostName=home.example.com" \
  --param="ddnsUsername=your_username"
```

You do not choose the password. Secrets Manager generates a 32-character one during the first deploy, so it never appears in `serverless.yml`, the CloudFormation template, or the deployment bucket.

### Step 2: Retrieve the Generated Password

```bash
aws secretsmanager get-secret-value \
  --secret-id /ddns/aws-python-ddns/dev \
  --query SecretString --output text
```

To use your own password instead, run `aws secretsmanager put-secret-value` against the same secret. Later deploys won't overwrite it. The authorizer caches credentials for up to 5 minutes, so a new password can take that long to take effect.

> **Upgrading from an earlier version?** Earlier versions passed `ddnsPassword` as a deploy parameter, which stored it in plaintext in the CloudFormation template. The first deploy of this version replaces it with a generated password, so retrieve the new one (above) and update your gateway. The `ddnsPassword` parameter is no longer used.

### Step 3: Configure Your UniFi Gateway

In your UniFi Network application, navigate to **Settings > Internet > [Your WAN Network] > Dynamic DNS**. Create a new Dynamic DNS entry with the following settings:

*   **Service**: Select `noip`.
*   **Hostname**: The `hostName` you deployed with (e.g., `home.example.com`).
*   **Username**: The `ddnsUsername` you deployed with.
*   **Password**: The password from Step 2.
*   **Server**: The server part of your API Gateway endpoint URL, including the path. **Do not include `https://`**.
    *   Example: `abc123defg.execute-api.us-east-1.amazonaws.com/dev/update`

Save the settings. Your UniFi gateway will now automatically update your Route 53 record whenever its public IP changes.

## Configuring `inadyn`

If you are using `inadyn` directly (e.g., on a device that supports a custom provider configuration), you can use a configuration similar to this. This example leverages the `%h` (hostname) and `%i` (IP address) variables provided by `inadyn` in the `ddns-path`.

Replace `YOUR_API_GATEWAY_ENDPOINT`, `your.example.com`, `your_username`, and `your_password` with your actual values. Remember that `YOUR_API_GATEWAY_ENDPOINT` should be the full domain of your API Gateway (e.g., `abc123defg.execute-api.us-east-1.amazonaws.com`).

```ini
custom https://YOUR_API_GATEWAY_ENDPOINT {
    hostname = "your.example.com"
    username = "your_username"
    password = "your_password"
    ddns-server = "YOUR_API_GATEWAY_ENDPOINT"
    ddns-path = "/dev/update?hostname=%h&myip=%i"
}
```

## Testing with `curl`

You can test your new DDNS endpoint from the command line using `curl`. This command emulates the request your UniFi gateway will send.

Replace the placeholder values with your actual information:
*   `YOUR_BASE64_AUTH_STRING` with the Base64 encoded string of your "username:password" (e.g., `echo -n "username:password" | base64`).
*   `YOUR_HOSTNAME` with the hostname you are updating.
*   `YOUR_IP` with the IP address you want to set.
*   `YOUR_ENDPOINT_URL` with the full API Gateway endpoint URL.

```bash
curl -X GET \
  -H "User-Agent: YourApp/1.0" \
  -H "Authorization: Basic YOUR_BASE64_AUTH_STRING" \
  "YOUR_ENDPOINT_URL?hostname=YOUR_HOSTNAME&myip=YOUR_IP"
```

**Example:**
```bash
curl -v -X GET \
  'https://YOUR_API_GATEWAY_ID.execute-api.YOUR_REGION.amazonaws.com/dev/update?hostname=your.domain.com&myip=192.0.2.1' \
  -H 'Authorization: Basic YOUR_BASE64_ENCODED_CREDENTIALS' \
  -H 'User-Agent: YourApp/1.0'
```

You should receive a response like `good 192.0.2.1` if the update was successful, or `nochg 192.0.2.1` if the IP address was already up to date.

## Project Origin and References

This project is inspired by and compatible with `inadyn`, a small and simple Dynamic DNS (DDNS) client.

*   **inadyn Project Home**: [https://troglobit.com/projects/inadyn/](https://troglobit.com/projects/inadyn/)
*   **inadyn GitHub Repository**: [https://github.com/troglobit/inadyn](https://github.com/troglobit/inadyn)
*   **inadyn Custom Providers Documentation**: [https://github.com/troglobit/inadyn?tab=readme-ov-file#custom-ddns-providers](https://github.com/troglobit/inadyn?tab=readme-ov-file#custom-ddns-providers)

## Security

*   Only IPv4 `A` records are supported. `myip` must be a valid IPv4 address; if it is omitted, the caller's source IP is used.
*   Request events are never logged in full, so credentials stay out of CloudWatch. Logs are kept for 30 days.
*   Everyone with the URL can attempt to log in. Use a long password (the generated one is fine) and consider adding [API Gateway throttling](https://docs.aws.amazon.com/apigateway/latest/developerguide/api-gateway-request-throttling.html) or AWS WAF rate limiting.

To report a vulnerability, see [SECURITY.md](SECURITY.md).

## Development

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
pytest && ruff check .
```

## Cleaning Up

If you wish to remove all deployed AWS resources, navigate to the project's root directory and run:

```bash
serverless remove
```
This will tear down the CloudFormation stack and all associated resources, including the Lambda function, API Gateway, and the Secrets Manager secret.
