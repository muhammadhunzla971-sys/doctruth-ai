import time

import httpx
import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app.github.app_auth import GitHubAppAuth, create_app_jwt, load_private_key
import base64

KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PEM = KEY.private_bytes(
    serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
).decode()
PUB = KEY.public_key()


def test_jwt_is_signed_for_app():
    token = create_app_jwt("123", PEM, now=int(time.time()))
    data = jwt.decode(token, PUB, algorithms=["RS256"], options={"verify_iat": False})
    assert data["iss"] == "123" and data["exp"] - data["iat"] == 600


def test_key_formats():
    assert load_private_key(PEM) == PEM.strip()
    assert load_private_key(base64.b64encode(PEM.encode()).decode()) == PEM
    assert "\n" in load_private_key(PEM.replace("\n", "\\n"))


async def test_installation_token_cached():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(201, json={"token": "ghs_x", "expires_at": "2099-01-01T00:00:00Z"})

    client = httpx.AsyncClient(base_url="https://api.github.com", transport=httpx.MockTransport(handler))
    auth = GitHubAppAuth("123", PEM, client=client)
    assert await auth.installation_token(5) == "ghs_x"
    assert await auth.installation_token(5) == "ghs_x"
    assert len(calls) == 1
    assert calls[0].headers["authorization"].startswith("Bearer ")
