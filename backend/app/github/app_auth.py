import base64
import time

import httpx
import jwt

GITHUB_API = "https://api.github.com"


def load_private_key(raw: str) -> str:
    """Accept PEM text (with real or escaped newlines) or base64 of the PEM."""
    raw = raw.strip()
    if "BEGIN" in raw:
        return raw.replace("\\n", "\n")
    return base64.b64decode(raw).decode()


def create_app_jwt(app_id: str, private_key: str, now: int | None = None) -> str:
    now = int(time.time()) if now is None else now
    payload = {"iat": now - 60, "exp": now + 540, "iss": app_id}
    return jwt.encode(payload, load_private_key(private_key), algorithm="RS256")


class GitHubAppAuth:
    """Short-lived installation tokens, cached in memory until shortly before expiry."""

    def __init__(self, app_id: str, private_key: str, client: httpx.AsyncClient | None = None) -> None:
        self._app_id = app_id
        self._private_key = private_key
        self._client = client or httpx.AsyncClient(base_url=GITHUB_API, timeout=15)
        self._cache: dict[int, tuple[str, float]] = {}

    async def installation_token(self, installation_id: int) -> str:
        cached = self._cache.get(installation_id)
        if cached and cached[1] - time.time() > 60:
            return cached[0]
        app_jwt = create_app_jwt(self._app_id, self._private_key)
        resp = await self._client.post(
            f"/app/installations/{installation_id}/access_tokens",
            headers={
                "Authorization": f"Bearer {app_jwt}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        resp.raise_for_status()
        data = resp.json()
        token = data["token"]
        expires_at = time.mktime(time.strptime(data["expires_at"], "%Y-%m-%dT%H:%M:%SZ")) - time.timezone
        self._cache[installation_id] = (token, expires_at)
        return token
