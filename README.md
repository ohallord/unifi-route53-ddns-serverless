# Serverless Dynamic DNS (DDNS) for UniFi & Inadyn Clients

This project provides a serverless, secure, and cost-effective Dynamic DNS (DDNS) endpoint on AWS. It is designed to be compatible with `inadyn`-based clients, most notably the Ubiquiti UniFi Dream Machine (UDM) series, by emulating the popular No-IP update protocol.

Instead of running a client script on your local network, your UniFi gateway can directly and securely update a Route 53 DNS record with its new public IP address.

## Features

*   **Serverless**: No servers to manage. The solution runs entirely on API Gateway and Lambda.
*   **Secure**: Authentication is handled by the Lambda function using credentials securely stored in **AWS Secrets Manager**.
*   **UniFi Native**: Works directly with the Dynamic DNS configuration in the UniFi Network application. No custom scripts or containers needed on your network.
*   **Infrastructure as Code**: All AWS resources (Lambda, API Gateway, IAM Roles, Secrets Manager Secret) are defined and deployed using the Serverless Framework.

## Architecture

1.  **UniFi Gateway**: Your UniFi UDM/USG detects a change in its public WAN IP address.
2.  **DDNS Update Request**: The UniFi device sends a No-IP compatible `GET` request to your secure API Gateway endpoint. The request includes a Basic Auth header with your DDNS username and password.
3.  **API Gateway**: Receives the request and triggers the AWS Lambda function.
4.  **AWS Lambda**: The Python Lambda function (`src/handler.py`) performs the following steps:
    *   Retrieves the secure credentials from **AWS Secrets Manager**.
    *   Validates the Basic Auth header from the request against the stored credentials.
    *   Checks the current IP address in Route 53 to avoid unnecessary updates.
    *   If the IP has changed, it uses `boto3` to update the specified 'A' record in your Route 53 Hosted Zone.
    *   Returns a No-IP compatible response code (e.g., `good`, `nochg`, `badauth`) that the UniFi client understands.

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
*   **Python 3.x**: Required for the Lambda runtime.

## Configuration and Deployment

### Step 1: Configure `serverless.yml`

Open the `serverless.yml` file. You must update two sections:

1.  **`custom` section**:
    ```yaml
    custom:
      hostname: 'home.yourdomain.com'     # <-- IMPORTANT: Replace with the FQDN you want to update
    ```
    *   **`hostname`**: The full domain name (e.g., `ddns.example.com`) for your serverless DDNS setup that the Lambda will update. The Hosted Zone ID will be dynamically determined by the Lambda function based on this hostname.

2.  **`resources` section (Credentials)**:
    This section defines the AWS Secrets Manager secret that will store your DDNS credentials. The `SecretString` will now dynamically fetch the username and password, ideally passed in at deploy time.

    ```yaml
    resources:
      Resources:
        DDNSCredentialsSecret:
          Type: AWS::SecretsManager::Secret
          Properties:
            # ...
            SecretString: '{"username": "${param:ddnsUsername}", "password": "${param:ddnsPassword}"}' # <-- IMPORTANT: Credentials passed at deploy time
    ```

### Step 2: Deploy the AWS Resources with Credentials

Instead of hardcoding your username and password, you can pass them securely during deployment using either command-line parameters or environment variables.

#### Option A: Using Command-Line Parameters (Recommended for manual deployments)

Pass the credentials directly in the `serverless deploy` command using the `--param` flag.

```bash
serverless deploy \
  --param="ddnsUsername=your_real_username" \
  --param="ddnsPassword=your_super_secret_password"
```

#### Option B: Using Environment Variables (Recommended for CI/CD and automation)

Set local environment variables in your shell before running the deploy command.

```bash
export DDNS_USERNAME="your_real_username"
export DDNS_PASSWORD="your_super_secret_password"
serverless deploy
```
*Note: Ensure your shell environment variables are not committed to version control.*

### Step 3: Secure Your Credentials (Recommended)

For enhanced security, you should update your secret directly in the AWS console after the first deployment and remove the hardcoded `SecretString` from `serverless.yml`.

1.  Go to the [AWS Secrets Manager console](https://console.aws.amazon.com/secretsmanager/).
2.  Find the secret named `/ddns/aws-python-ddns/dev` (the name is based on the `serverless.yml` configuration).
3.  Click "Retrieve secret value" and then "Edit". Update the username and password.
4.  (Optional but recommended) Go back to `serverless.yml`, remove the `SecretString` line from the `DDNSCredentialsSecret` resource, and redeploy. This ensures your password is not stored in your template file.

### Step 4: Configure Your UniFi Gateway

In your UniFi Network application, navigate to **Settings > Internet > [Your WAN Network] > Dynamic DNS**. Create a new Dynamic DNS entry with the following settings:

*   **Service**: Select `noip`.
*   **Hostname**: The hostname you configured in `serverless.yml` (e.g., `home.yourdomain.com`).
*   **Username**: The username you set in your AWS Secret.
*   **Password**: The password you set in your AWS Secret.
*   **Server**: The server part of your API Gateway endpoint URL, including the path. **Do not include `https://`**.
    *   Example: `adu4crry4l.execute-api.us-east-1.amazonaws.com/dev/update`

Save the settings. Your UniFi gateway will now automatically update your Route 53 record whenever its public IP changes.

## Configuring `inadyn`

If you are using `inadyn` directly (e.g., on a device that supports a custom provider configuration), you can use a configuration similar to this. This example leverages the `%h` (hostname) and `%i` (IP address) variables provided by `inadyn` in the `ddns-path`.

Replace `YOUR_API_GATEWAY_ENDPOINT`, `your.example.com`, `your_username`, and `your_password` with your actual values. Remember that `YOUR_API_GATEWAY_ENDPOINT` should be the full domain of your API Gateway (e.g., `u679stz5ig.execute-api.us-east-1.amazonaws.com`).

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
*   `YOUR_USERNAME` and `YOUR_PASSWORD` with the credentials from your AWS Secret.
*   `YOUR_HOSTNAME` with the hostname you are updating.
*   `YOUR_IP` with the IP address you want to set.
*   `YOUR_ENDPOINT_URL` with the full API Gateway endpoint URL.

```bash
curl -X GET \
  -H "User-Agent: UniFi/1.0" \
  --user "YOUR_USERNAME:YOUR_PASSWORD" \
  "YOUR_ENDPOINT_URL?hostname=YOUR_HOSTNAME&myip=YOUR_IP"
```

**Example:**
```bash
curl -X GET \
  -H "User-Agent: UniFi/1.0" \
  --user "myddnsuser:MySecurePassword123" \
  "https://adu4crry4l.execute-api.us-east-1.amazonaws.com/dev/update?hostname=home.yourdomain.com&myip=8.8.8.8"
```

You should receive a response like `good 8.8.8.8` if the update was successful, or `nochg 8.8.8.8` if the IP address was already up to date.

## Cleaning Up

If you wish to remove all deployed AWS resources, navigate to the project's root directory and run:

```bash
serverless remove
```
This will tear down the CloudFormation stack and all associated resources, including the Lambda function, API Gateway, and the Secrets Manager secret.
# unifi-route53-ddns-serverless
# unifi-route53-ddns-serverless
