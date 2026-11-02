"""Unit tests for src/handler.py. AWS clients are mocked; no AWS access needed."""
import base64
import json
import logging
import os
import sys
from unittest import mock

import pytest

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ["DDNS_SECRET_NAME"] = "/ddns/test"
os.environ["DDNS_HOSTNAME"] = "home.example.com"

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import handler  # noqa: E402

METHOD_ARN = "arn:aws:execute-api:us-east-1:123456789012:abc123/dev/GET/update"
ZONES = {
    "HostedZones": [
        {"Id": "/hostedzone/ZEXAMPLE", "Name": "example.com.", "Config": {"PrivateZone": False}},
        {"Id": "/hostedzone/ZOTHER", "Name": "other.com.", "Config": {"PrivateZone": False}},
    ]
}


@pytest.fixture(autouse=True)
def aws(monkeypatch):
    r53 = mock.MagicMock()
    r53.get_paginator.return_value.paginate.return_value = [ZONES]
    r53.list_resource_record_sets.return_value = {"ResourceRecordSets": []}
    sm = mock.MagicMock()
    sm.get_secret_value.return_value = {
        "SecretString": json.dumps({"username": "ddnsuser", "password": "s3cret:pw"})
    }
    monkeypatch.setattr(handler, "route53", r53)
    monkeypatch.setattr(handler, "secretsmanager", sm)
    monkeypatch.setenv("DDNS_HOSTNAME", "home.example.com")
    if hasattr(handler, "_secret_cache"):
        handler._secret_cache.clear()
    return r53, sm


def basic(user, pw):
    return "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()


def auth_event(header, name="Authorization"):
    return {"headers": {name: header} if header is not None else None, "methodArn": METHOD_ARN}


def update_event(hostname="home.example.com", myip="192.0.2.10", auth=None):
    params = {}
    if hostname is not None:
        params["hostname"] = hostname
    if myip is not None:
        params["myip"] = myip
    return {
        "headers": {"User-Agent": "inadyn/2.9", "Authorization": auth or basic("ddnsuser", "s3cret:pw")},
        "queryStringParameters": params,
        "path": "/update",
        "requestContext": {"identity": {"sourceIp": "198.51.100.7"}},
    }


# --- authorizer -----------------------------------------------------------

def test_authorizer_allows_valid_credentials():
    res = handler.authorizer_handler(auth_event(basic("ddnsuser", "s3cret:pw")), None)
    assert res["policyDocument"]["Statement"][0]["Effect"] == "Allow"


def test_authorizer_policy_covers_whole_api_so_cached_allow_works_on_any_path():
    res = handler.authorizer_handler(auth_event(basic("ddnsuser", "s3cret:pw")), None)
    assert res["policyDocument"]["Statement"][0]["Resource"] == (
        "arn:aws:execute-api:us-east-1:123456789012:abc123/dev/*/*"
    )


def test_authorizer_accepts_lowercase_header_name():
    res = handler.authorizer_handler(auth_event(basic("ddnsuser", "s3cret:pw"), "authorization"), None)
    assert res["policyDocument"]["Statement"][0]["Effect"] == "Allow"


@pytest.mark.parametrize("header", [
    None, "", "Bearer abc", "Basic", "Basic !!!notbase64", basic("ddnsuser", "wrong"),
    basic("other", "s3cret:pw"), "Basic " + base64.b64encode(b"nocolon").decode(),
])
def test_authorizer_rejects_bad_credentials_with_401(header):
    with pytest.raises(handler.Unauthorized, match="^Unauthorized$"):
        handler.authorizer_handler(auth_event(header), None)


def test_authorizer_uses_constant_time_compare(monkeypatch):
    calls = []
    real = handler.hmac.compare_digest
    monkeypatch.setattr(handler.hmac, "compare_digest", lambda a, b: calls.append(1) or real(a, b))
    handler.authorizer_handler(auth_event(basic("ddnsuser", "s3cret:pw")), None)
    assert len(calls) == 2


def test_authorizer_caches_secret(aws):
    _, sm = aws
    for _ in range(3):
        handler.authorizer_handler(auth_event(basic("ddnsuser", "s3cret:pw")), None)
    assert sm.get_secret_value.call_count == 1


def test_authorizer_never_logs_credentials(caplog):
    caplog.set_level(logging.DEBUG)
    hdr = basic("ddnsuser", "s3cret:pw")
    handler.authorizer_handler(auth_event(hdr), None)
    with pytest.raises(handler.Unauthorized):
        handler.authorizer_handler(auth_event(basic("ddnsuser", "guess")), None)
    assert hdr.split()[1] not in caplog.text
    assert "s3cret" not in caplog.text and "guess" not in caplog.text


# --- update handler -------------------------------------------------------

def test_update_good(aws):
    r53, _ = aws
    res = handler.lambda_handler(update_event(), None)
    assert res == {"statusCode": 200, "body": "good 192.0.2.10"}
    change = r53.change_resource_record_sets.call_args.kwargs
    assert change["HostedZoneId"] == "ZEXAMPLE"
    assert change["ChangeBatch"]["Changes"][0]["ResourceRecordSet"]["Name"] == "home.example.com"


def test_update_nochg(aws):
    r53, _ = aws
    r53.list_resource_record_sets.return_value = {"ResourceRecordSets": [
        {"Name": "home.example.com.", "Type": "A", "ResourceRecords": [{"Value": "192.0.2.10"}]}]}
    assert handler.lambda_handler(update_event(), None)["body"] == "nochg 192.0.2.10"
    r53.change_resource_record_sets.assert_not_called()


def test_update_defaults_to_source_ip(aws):
    assert handler.lambda_handler(update_event(myip=None), None)["body"] == "good 198.51.100.7"


def test_update_hostname_case_and_trailing_dot_normalised(aws):
    assert handler.lambda_handler(update_event(hostname="HOME.Example.com."), None)["statusCode"] == 200


def test_update_hostname_from_path_fallback(aws):
    ev = update_event(hostname=None)
    ev["path"] = "/update/home.example.com"
    assert handler.lambda_handler(ev, None)["statusCode"] == 200


@pytest.mark.parametrize("host", ["evil.example.com", "www.other.com", "example.com", "home.example.com.evil.com"])
def test_update_refuses_hostnames_not_configured(aws, host):
    r53, _ = aws
    res = handler.lambda_handler(update_event(hostname=host), None)
    assert res == {"statusCode": 400, "body": "nohost"}
    r53.change_resource_record_sets.assert_not_called()


@pytest.mark.parametrize("ip", ["not-an-ip", "999.1.1.1", "2001:db8::1", "1.2.3.4<script>", "1.2.3.4\n"])
def test_update_rejects_invalid_ip(aws, ip):
    r53, _ = aws
    res = handler.lambda_handler(update_event(myip=ip), None)
    assert res == {"statusCode": 400, "body": "badreq"}
    r53.change_resource_record_sets.assert_not_called()


def test_update_fails_closed_without_configured_hostname(aws, monkeypatch):
    r53, _ = aws
    monkeypatch.delenv("DDNS_HOSTNAME")
    assert handler.lambda_handler(update_event(), None)["statusCode"] == 500
    r53.change_resource_record_sets.assert_not_called()


def test_update_missing_user_agent(aws):
    ev = update_event()
    ev["headers"].pop("User-Agent")
    assert handler.lambda_handler(ev, None)["body"] == "badagent"


def test_update_handles_null_headers(aws):
    ev = update_event()
    ev["headers"] = None
    assert handler.lambda_handler(ev, None)["body"] == "badagent"


def test_update_never_logs_authorization_header(aws, caplog):
    caplog.set_level(logging.DEBUG)
    ev = update_event()
    handler.lambda_handler(ev, None)
    assert ev["headers"]["Authorization"].split()[1] not in caplog.text
