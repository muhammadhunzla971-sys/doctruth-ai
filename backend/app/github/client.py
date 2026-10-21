"""Thin async GitHub REST/GraphQL client that authenticates as an app installation."""
import base64

import httpx

GITHUB_API = "https://api.github.com"
MAX_FILE_BYTES = 200_000

THREADS_QUERY = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      reviewThreads(first: 50) {
        nodes {
          isResolved isOutdated path line
          comments(first: 20) { nodes { body author { login } } }
        }
      }
    }
  }
}
"""


class GitHubClient:
    def __init__(self, auth, installation_id: int, http: httpx.AsyncClient | None = None) -> None:
        self._auth = auth
        self._installation_id = installation_id
        self._http = http or httpx.AsyncClient(base_url=GITHUB_API, timeout=20)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _request(self, method: str, url: str, *, accept: str = "application/vnd.github+json",
                       allow_404: bool = False, **kw) -> httpx.Response | None:
        token = await self._auth.installation_token(self._installation_id)
        resp = await self._http.request(
            method, url, headers={"Authorization": f"token {token}", "Accept": accept,
                                  "X-GitHub-Api-Version": "2022-11-28"}, **kw)
        if resp.status_code == 404 and allow_404:
            return None
        resp.raise_for_status()
        return resp

    async def _json(self, url: str, **kw):
        resp = await self._request("GET", url, **kw)
        return resp.json() if resp is not None else None

    async def get_pr(self, repo: str, number: int) -> dict:
        return await self._json(f"/repos/{repo}/pulls/{number}")

    async def get_commit(self, repo: str, sha: str) -> dict:
        return await self._json(f"/repos/{repo}/commits/{sha}")

    async def compare(self, repo: str, base: str, head: str) -> dict:
        """Files and commits between two refs (first 3 pages of 100 files)."""
        files: list[dict] = []
        data: dict = {}
        for page in (1, 2, 3):
            data = await self._json(f"/repos/{repo}/compare/{base}...{head}", params={"per_page": 100, "page": page})
            files += data.get("files", [])
            if len(data.get("files", [])) < 100:
                break
        return {"files": files, "commits": data.get("commits", [])}

    async def get_file_text(self, repo: str, path: str, ref: str) -> str | None:
        # A 404 here means the file should exist but we cannot read it (e.g. missing Contents permission): fail loudly.
        resp = await self._request("GET", f"/repos/{repo}/contents/{path}", params={"ref": ref})
        body = resp.json()
        if not isinstance(body, dict) or body.get("encoding") != "base64" or body.get("size", 0) > MAX_FILE_BYTES:
            return None
        return base64.b64decode(body["content"]).decode("utf-8", "replace")

    async def pr_commits(self, repo: str, number: int) -> list[dict]:
        return await self._json(f"/repos/{repo}/pulls/{number}/commits", params={"per_page": 100})

    async def review_threads(self, repo: str, number: int) -> list[dict]:
        owner, name = repo.split("/", 1)
        resp = await self._request("POST", "/graphql", json={
            "query": THREADS_QUERY, "variables": {"owner": owner, "name": name, "number": number}})
        data = resp.json()
        if data.get("errors"):
            raise RuntimeError(f"graphql error: {data['errors'][0].get('message', '')[:200]}")
        return data["data"]["repository"]["pullRequest"]["reviewThreads"]["nodes"]

    async def get_issue(self, repo: str, number: int) -> dict | None:
        return await self._json(f"/repos/{repo}/issues/{number}", allow_404=True)

    async def latest_release(self, repo: str) -> dict | None:
        return await self._json(f"/repos/{repo}/releases/latest", allow_404=True)

    async def list_tags(self, repo: str) -> list[dict]:
        return await self._json(f"/repos/{repo}/tags", params={"per_page": 10}) or []

    async def pulls_for_commit(self, repo: str, sha: str) -> list[dict]:
        return await self._json(f"/repos/{repo}/commits/{sha}/pulls", allow_404=True) or []

    async def get_tree(self, repo: str, sha: str) -> list[str]:
        """All file paths at a commit (one call; GitHub truncates only for huge repos)."""
        data = await self._json(f"/repos/{repo}/git/trees/{sha}", params={"recursive": 1})
        return [e["path"] for e in data.get("tree", []) if e.get("type") == "blob"]

    # --- writes (Phase 5) ---
    async def get_file(self, repo: str, path: str, ref: str) -> tuple[str, str] | None:
        """(text, blob sha) of a file, or None if it does not exist at ref."""
        resp = await self._request("GET", f"/repos/{repo}/contents/{path}", params={"ref": ref}, allow_404=True)
        if resp is None:
            return None
        body = resp.json()
        if not isinstance(body, dict) or body.get("encoding") != "base64":
            return None
        return base64.b64decode(body["content"]).decode("utf-8", "replace"), body["sha"]

    async def get_ref(self, repo: str, branch: str) -> dict | None:
        return await self._json(f"/repos/{repo}/git/ref/heads/{branch}", allow_404=True)

    async def create_ref(self, repo: str, branch: str, sha: str) -> None:
        await self._request("POST", f"/repos/{repo}/git/refs", json={"ref": f"refs/heads/{branch}", "sha": sha})

    async def reset_ref(self, repo: str, branch: str, sha: str) -> None:
        await self._request("PATCH", f"/repos/{repo}/git/refs/heads/{branch}", json={"sha": sha, "force": True})

    async def put_file(self, repo: str, path: str, text: str, message: str, branch: str, sha: str) -> None:
        await self._request("PUT", f"/repos/{repo}/contents/{path}", json={
            "message": message, "branch": branch, "sha": sha,
            "content": base64.b64encode(text.encode()).decode()})

    async def find_open_pr(self, repo: str, branch: str) -> dict | None:
        owner = repo.split("/", 1)[0]
        prs = await self._json(f"/repos/{repo}/pulls", params={"head": f"{owner}:{branch}", "state": "open"})
        return prs[0] if prs else None

    async def create_pr(self, repo: str, title: str, body: str, head: str, base: str) -> dict:
        resp = await self._request("POST", f"/repos/{repo}/pulls",
                                   json={"title": title, "body": body, "head": head, "base": base})
        return resp.json()

    async def update_pr(self, repo: str, number: int, title: str, body: str) -> dict:
        resp = await self._request("PATCH", f"/repos/{repo}/pulls/{number}", json={"title": title, "body": body})
        return resp.json()

    async def list_comments(self, repo: str, number: int) -> list[dict]:
        return await self._json(f"/repos/{repo}/issues/{number}/comments", params={"per_page": 100}) or []

    async def create_comment(self, repo: str, number: int, body: str) -> dict:
        resp = await self._request("POST", f"/repos/{repo}/issues/{number}/comments", json={"body": body})
        return resp.json()

    async def update_comment(self, repo: str, comment_id: int, body: str) -> dict:
        resp = await self._request("PATCH", f"/repos/{repo}/issues/comments/{comment_id}", json={"body": body})
        return resp.json()
