import base64

import httpx
import pytest

from app.github.client import GitHubClient


class Auth:
    async def installation_token(self, installation_id):
        return "tok"


def client_with(handler):
    http = httpx.AsyncClient(base_url="https://api.github.com", transport=httpx.MockTransport(handler))
    return GitHubClient(Auth(), 1, http)


async def test_file_text_decodes_base64_and_sends_auth():
    seen = {}

    def handler(req):
        seen["auth"] = req.headers["authorization"]
        return httpx.Response(200, json={"encoding": "base64", "size": 3, "content": base64.b64encode(b"hi\n").decode()})

    assert await client_with(handler).get_file_text("a/b", "x.py", "sha") == "hi\n"
    assert seen["auth"] == "token tok"


async def test_missing_file_and_oversized_file_return_none():
    assert await client_with(lambda r: httpx.Response(404)).get_file_text("a/b", "x.py", "sha") is None
    big = lambda r: httpx.Response(200, json={"encoding": "base64", "size": 10**7, "content": ""})  # noqa: E731
    assert await client_with(big).get_file_text("a/b", "x.py", "sha") is None


async def test_server_errors_raise_so_the_job_is_retried():
    with pytest.raises(httpx.HTTPStatusError):
        await client_with(lambda r: httpx.Response(500)).get_pr("a/b", 1)


async def test_compare_paginates_until_a_short_page():
    pages = []

    def handler(req):
        page = int(req.url.params["page"])
        pages.append(page)
        files = [{"filename": f"f{i}.py"} for i in range(100 if page == 1 else 5)]
        return httpx.Response(200, json={"files": files, "commits": []})

    out = await client_with(handler).compare("a/b", "x", "y")
    assert pages == [1, 2] and len(out["files"]) == 105


async def test_review_threads_graphql_and_error_handling():
    ok = {"data": {"repository": {"pullRequest": {"reviewThreads": {"nodes": [{"isResolved": True}]}}}}}
    assert await client_with(lambda r: httpx.Response(200, json=ok)).review_threads("a/b", 1) == [{"isResolved": True}]
    bad = {"errors": [{"message": "nope"}]}
    with pytest.raises(RuntimeError):
        await client_with(lambda r: httpx.Response(200, json=bad)).review_threads("a/b", 1)
