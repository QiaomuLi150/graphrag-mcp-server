"""Local development token issuer (RS256). NOT part of the serving image.

    python -m tools.dev_token init                 # create .local/dev_jwt_{private,public}.pem
    python -m tools.dev_token mint [--scope graph:read] [--sub dev-client] [--ttl 3600]

The server trusts the matching public key via GRAPHRAG_AUTH_PUBLIC_KEY. Real deployments
use an OIDC issuer (JWKS), never this script.
"""

from __future__ import annotations

import argparse
import os
import time
from pathlib import Path

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

LOCAL = Path(os.environ.get("GRAPHRAG_KEY_DIR", ".local"))
ISSUER = "https://dev-issuer.local"
AUDIENCE = "graphrag-mcp-local"


def init() -> None:
    LOCAL.mkdir(parents=True, exist_ok=True)
    priv = LOCAL / "dev_jwt_private.pem"
    if priv.exists():
        print(f"{priv} already exists; keeping it")
        return
    k = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    priv.write_bytes(
        k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    )
    priv.chmod(0o600)
    (LOCAL / "dev_jwt_public.pem").write_bytes(
        k.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    )
    print("created", priv)


def mint(scope: str, sub: str, ttl: int, audience: str, issuer: str) -> str:
    key = (LOCAL / "dev_jwt_private.pem").read_bytes()
    now = int(time.time())
    return jwt.encode(
        {"iss": issuer, "aud": audience, "sub": sub, "scope": scope, "iat": now, "exp": now + ttl},
        key,
        algorithm="RS256",
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init")
    m = sub.add_parser("mint")
    m.add_argument("--scope", default="graph:read")
    m.add_argument("--sub", default="dev-client")
    m.add_argument("--ttl", type=int, default=3600)
    m.add_argument("--audience", default=AUDIENCE)
    m.add_argument("--issuer", default=ISSUER)
    a = ap.parse_args()
    if a.cmd == "init":
        init()
    else:
        print(mint(a.scope, a.sub, a.ttl, a.audience, a.issuer))


if __name__ == "__main__":
    main()
