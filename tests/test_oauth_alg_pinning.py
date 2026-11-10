#!/usr/bin/env python3
"""OIDC ID-token algorithm pinning regression (python-jose PYSEC-2026-4298 hardening).

The signing algorithm must come from server config, never the token header.

Run:  python -m tests.test_oauth_alg_pinning
"""

import base64
import hashlib
import hmac
import json
import os
import sys
import tempfile


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _token(alg: str, secret: bytes, claims: dict) -> str:
    head = _b64(json.dumps({"alg": alg, "kid": "k1", "typ": "JWT"}).encode())
    body = _b64(json.dumps(claims).encode())
    sig = _b64(hmac.new(secret, f"{head}.{body}".encode(), hashlib.sha256).digest())
    return f"{head}.{body}.{sig}"


def main() -> int:
    fd, db = tempfile.mkstemp(suffix=".db", prefix="lambda_oauth_alg_")
    os.close(fd)
    os.environ.update({"LAMBDA_ERP_DB": db, "LAMBDA_ERP_AUTO_DEMO": "0", "LAMBDA_ERP_PLUGINS": "",
                       "JWT_SECRET_KEY": "synthetic-secret", "OPENAI_API_KEY": "synthetic"})
    from fastapi import HTTPException
    from api import oauth

    failures = []

    def check(name, ok, detail=""):
        print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"  <- {detail}"))
        if not ok:
            failures.append(name)

    provider = next(iter(oauth.PROVIDERS))
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from jose import jwk as jose_jwk

    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                        serialization.NoEncryption()).decode()
    public_pem = private.public_key().public_bytes(serialization.Encoding.PEM,
                                                    serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    public_jwk = jose_jwk.construct(public_pem, "RS256").to_dict()
    oauth._jwk_for = lambda provider, kid: public_jwk
    oauth._client_id = lambda provider: "synthetic-client"

    seen = {}
    real_decode = oauth.jwt.decode

    def spy(token, key, algorithms=None, **kw):
        seen["algorithms"] = algorithms
        return real_decode(token, key, algorithms=algorithms, **kw)

    oauth.jwt.decode = spy

    for alg in ("HS256", "HS512", "none", "ES256"):
        seen.clear()
        try:
            oauth._verify_id_token(provider, _token(alg if alg != "none" else "HS256", b"k", {"sub": "x"})
                                   if alg != "none" else _b64(b'{"alg":"none"}') + "." + _b64(b"{}") + ".", "")
            ok = False
        except HTTPException as exc:
            ok = exc.status_code == 400
        check(f"header alg {alg} rejected with 400 before any key use", ok and "algorithms" not in seen, str(seen))

    import time
    from jose import jwt as jose_jwt
    good = jose_jwt.encode({"sub": "x", "aud": "synthetic-client", "iss": oauth.PROVIDERS[provider]["issuer"],
                            "exp": int(time.time()) + 300}, private_pem, algorithm="RS256", headers={"kid": "k1"})
    seen.clear()
    claims = oauth._verify_id_token(provider, good, "")
    check("valid RS256 token verifies", claims.get("sub") == "x", str(claims))
    check("decode receives the server-pinned algorithm list", seen.get("algorithms") == ["RS256"], str(seen))

    forged = jose_jwt.encode({"sub": "x", "aud": "synthetic-client", "iss": oauth.PROVIDERS[provider]["issuer"],
                              "exp": int(time.time()) + 300}, "wrong-key", algorithm="HS256", headers={"kid": "k1"})
    try:
        oauth._verify_id_token(provider, forged, "")
        ok = False
    except HTTPException as exc:
        ok = exc.status_code == 400
    check("HS256-signed forgery rejected", ok)

    print(f"\n{len(failures)} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
