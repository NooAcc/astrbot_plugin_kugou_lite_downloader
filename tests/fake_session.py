"""假 aiohttp 会话与响应，用于离线测试下载与协议请求。

`FakeSession.get/post` 是同步调用（与 aiohttp 一致），返回一个可用
`async with` 进入的上下文管理器，并记录每次调用的参数。
"""

from __future__ import annotations

import json as _json


class FakeResponse:
    def __init__(self, chunks=None, status=200, headers=None, exc=None, payload=None):
        self._chunks = list(chunks or [])
        self.status = status
        self.headers = dict(headers or {})
        self._exc = exc
        self._payload = payload
        self.content = self

    async def __aenter__(self):
        if self._exc is not None:
            raise self._exc
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def iter_chunked(self, size):
        for chunk in self._chunks:
            yield chunk
        if self._exc is not None:
            raise self._exc

    async def text(self):
        if self._payload is None:
            return ""
        return _json.dumps(self._payload, ensure_ascii=False)

    async def json(self):
        return self._payload


class FakeSession:
    """`responses` 里可以放 `FakeResponse`，也可以直接放异常实例表示连接失败。"""

    def __init__(self, responses=None):
        self.responses = list(responses or [])
        self.calls: list[dict] = []

    def _take(self) -> FakeResponse:
        if not self.responses:
            return FakeResponse([], status=500)
        item = self.responses.pop(0)
        if isinstance(item, BaseException):
            return FakeResponse(exc=item)
        return item

    def get(self, url, **kwargs):
        self.calls.append({"method": "get", "url": url, "kw": kwargs})
        return self._take()

    def post(self, url, **kwargs):
        self.calls.append({"method": "post", "url": url, "kw": kwargs})
        return self._take()


def json_response(payload, status=200, headers=None):
    return FakeResponse([], status=status, headers=headers, payload=payload)


def reading(urls):
    """把若干次调用对应的 URL 抽出来，便于断言重定向/备用地址链。"""
    return [call["url"] for call in urls]
