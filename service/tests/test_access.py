"""Origin authentication with real RSA signatures and mocked public JWKS."""
import io
import json
import time
from unittest.mock import patch

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from starlette.testclient import TestClient

from rmzotero.claude.server import create_app
from rmzotero.config import AccessConfig, Config

TEAM = "https://example.cloudflareaccess.com"
HOST = "zoteromcp.example.com"
LEGACY = "rmzoteroclaude.zsrobinson.com"
SECRET = "test-only-legacy-credential"
BODY = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
    "protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}}
HEADERS = {"accept": "application/json, text/event-stream"}


@pytest.fixture
def keys():
    return [rsa.generate_private_key(public_exponent=65537, key_size=2048) for _ in range(2)]


def token(key, **changes):
    claims = dict(iss=TEAM, aud="test-audience", exp=time.time()+300, nbf=time.time()-1, email="test@example.com")
    claims.update(changes)
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": "first"})


def app(access=True, **kw):
    cfg = AccessConfig(TEAM, "test-audience", [HOST], kw.pop("emails", [])) if access else AccessConfig()
    return create_app(None, SECRET, cfg, **kw)


def jwks(key, kid="first"):
    return {"keys": [{**jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key(), as_dict=True), "kid": kid}]}


def post(client, host=HOST, path="/mcp", assertion=None, **headers):
    h = {**HEADERS, "host": host, **headers}
    if assertion is not None:
        h["cf-access-jwt-assertion"] = assertion
    return client.post(path, json=BODY, headers=h)


def test_valid_cache_rotation(keys):
    with patch("jwt.jwks_client.urllib.request.OpenerDirector.open", side_effect=lambda *a, **k: io.BytesIO(json.dumps(jwks(keys[0])).encode())) as fetch:
        with TestClient(app()) as client:
            for _ in range(2):
                assert post(client, assertion=token(keys[0])).json()["result"]["serverInfo"]["name"] == "rmzotero"
            assert fetch.call_count == 1
            fetch.side_effect = lambda *a, **k: io.BytesIO(json.dumps(jwks(keys[1], "rotated")).encode())
            client.app.jwks._last_successful_fetch = 0
            rotated = jwt.encode(dict(iss=TEAM, aud="test-audience", exp=time.time()+300), keys[1],
                                 algorithm="RS256", headers={"kid": "rotated"})
            assert post(client, assertion=rotated).status_code == 200
            assert fetch.call_count == 2


@pytest.mark.parametrize("change", [{"aud": "wrong"}, {"iss": "https://wrong.cloudflareaccess.com"},
                                    {"exp": time.time()-120}, {"nbf": time.time()+120}, {"exp": None}])
def test_invalid_claims(keys, change):
    with patch("jwt.jwks_client.urllib.request.OpenerDirector.open", return_value=io.BytesIO(json.dumps(jwks(keys[0])).encode())):
        with TestClient(app()) as client:
            assert post(client, assertion=token(keys[0], **change)).status_code == 401


def test_bad_signature_missing_malformed_and_jwks_failure(keys):
    with patch("jwt.jwks_client.urllib.request.OpenerDirector.open", return_value=io.BytesIO(json.dumps(jwks(keys[0])).encode())):
        with TestClient(app()) as client:
            for assertion in (None, "malformed", token(keys[1])):
                r = post(client, assertion=assertion)
                assert r.status_code == 401 and r.text == "Unauthorized"
    with patch("jwt.jwks_client.urllib.request.OpenerDirector.open", side_effect=OSError("private network details")):
        with TestClient(app()) as client:
            assert post(client, assertion=token(keys[0])).text == "Unauthorized"


def test_routes_and_legacy():
    with TestClient(app()) as client:
        assert post(client).status_code == 401
        assert post(client, path=f"/mcp/{SECRET}").status_code == 404
        assert post(client, host="evil.example.com").status_code == 404
        assert post(client, host=LEGACY).status_code == 404
        assert post(client, host=LEGACY, path=f"/mcp/{SECRET}").status_code == 200
        assert post(client, host=LEGACY, path=f"/mcp/{SECRET}", origin="https://evil.example.com").status_code == 403
    with TestClient(app(legacy_enabled=False)) as client:
        assert post(client, host=LEGACY, path=f"/mcp/{SECRET}").status_code == 404
    with TestClient(app(False)) as client:
        assert post(client, host=LEGACY, path=f"/mcp/{SECRET}").status_code == 200
        assert post(client, host=LEGACY).status_code == 404
        assert post(client).status_code == 404


def test_email_allowlist(keys):
    with patch("jwt.jwks_client.urllib.request.OpenerDirector.open", return_value=io.BytesIO(json.dumps(jwks(keys[0])).encode())):
        with TestClient(app(emails=["test@example.com"])) as client:
            assert post(client, assertion=token(keys[0])).status_code == 200
            assert post(client, assertion=token(keys[0], email="other@example.com")).status_code == 403


def test_duplicate_headers_and_forwarded_host():
    with TestClient(app()) as client:
        assert post(client, host="evil.example.com", **{"x-forwarded-host": HOST}).status_code == 404
        r = client.post("/mcp", json=BODY, headers=[("host", HOST), ("host", LEGACY)])
        assert r.status_code == 404
        r = client.post("/mcp", json=BODY, headers=[("host", HOST),
                        ("cf-access-jwt-assertion", "one"), ("cf-access-jwt-assertion", "two")])
        assert r.status_code == 401


def test_configuration(tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    path.write_text('[access]\nteam_domain="example.cloudflareaccess.com"\naudience="test"\nhostnames=["example.com"]\n')
    monkeypatch.setenv("RMZOTERO_ACCESS_HOSTNAMES", "one.example.com,two.example.com")
    monkeypatch.setenv("RMZOTERO_MCP_LEGACY_ENABLED", "false")
    cfg = Config.load(path)
    assert cfg.access.hostnames == ["one.example.com", "two.example.com"]
    assert cfg.access.team_domain == TEAM and not cfg.mcp_legacy_enabled
    with pytest.raises(ValueError):
        AccessConfig(team_domain=TEAM)
