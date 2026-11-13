"""封面抓取与落盘缓存：SSRF、重定向、大小/像素上限、缓存命中。"""

import asyncio
import io
import os
import struct
import zlib

from PIL import Image

from cover_cache import CoverCache
from fake_session import FakeResponse, FakeSession


def _png(w=2, h=2, mode="RGB"):
    buf = io.BytesIO()
    Image.new(mode, (w, h)).save(buf, format="PNG")
    return buf.getvalue()


def _png_header_only(w, h):
    """只写签名 + IHDR，声明巨大尺寸（像素炸弹：文件很小，解码才爆）。"""

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)


def _public_resolver(host):
    return ["93.184.216.34"]


def _cache(tmp_path, session, resolver=_public_resolver):
    return CoverCache(session, str(tmp_path / "covers"), resolver=resolver)


def _resp(body=b"", status=200, headers=None):
    return FakeResponse([body], status=status, headers=headers or {})


def test_second_call_hits_disk_without_request(tmp_path):
    session = FakeSession([_resp(_png())])
    cache = _cache(tmp_path, session)
    url = "https://imge.kugou.com/stdmusic/240/a.jpg"

    first = asyncio.run(cache.ensure(url))
    second = asyncio.run(cache.ensure(url))

    assert first is not None and first == second
    assert os.path.getsize(first) > 0
    assert len(session.calls) == 1          # 第二次命中磁盘，不再发请求


def test_rejects_host_outside_allowlist(tmp_path):
    session = FakeSession([_resp(_png())])
    cache = _cache(tmp_path, session)

    assert asyncio.run(cache.ensure("https://evil.example/x.jpg")) is None
    assert session.calls == []              # 不允许的主机连请求都不发


def test_rejects_redirect_to_private_host(tmp_path):
    def resolver(host):
        return ["127.0.0.1"] if host == "127.0.0.1" else ["93.184.216.34"]

    session = FakeSession([_resp(status=302, headers={"Location": "http://127.0.0.1/x"})])
    cache = _cache(tmp_path, session, resolver=resolver)

    assert asyncio.run(cache.ensure("https://imge.kugou.com/a.jpg")) is None
    assert len(session.calls) == 1          # 跳转目标未通过校验，不发起第二次请求


def test_rejects_too_many_redirects(tmp_path):
    loop = [_resp(status=302, headers={"Location": "https://imge.kugou.com/loop.jpg"})
            for _ in range(10)]
    session = FakeSession(loop)
    cache = _cache(tmp_path, session)

    assert asyncio.run(cache.ensure("https://imge.kugou.com/loop.jpg")) is None
    assert len(session.calls) <= 7          # max_redirects=5 → 有限次后放弃，不无限循环


def test_oversize_content_length_returns_none(tmp_path):
    over = 8 * 1024 * 1024 + 1              # 超过 CoverLimits.max_bytes 默认值
    session = FakeSession([_resp(_png(), headers={"Content-Length": str(over)})])
    cache = _cache(tmp_path, session)

    assert asyncio.run(cache.ensure("https://imge.kugou.com/big.jpg")) is None


def test_non_200_returns_none(tmp_path):
    session = FakeSession([_resp(b"nope", status=500)])
    cache = _cache(tmp_path, session)

    assert asyncio.run(cache.ensure("https://imge.kugou.com/x.jpg")) is None


def test_non_image_body_returns_none(tmp_path):
    session = FakeSession([_resp(b"not an image at all")])
    cache = _cache(tmp_path, session)

    assert asyncio.run(cache.ensure("https://imge.kugou.com/x.jpg")) is None


def test_pixel_bomb_is_rejected(tmp_path):
    session = FakeSession([_resp(_png_header_only(20000, 20000))])
    cache = _cache(tmp_path, session)

    assert asyncio.run(cache.ensure("https://imge.kugou.com/bomb.png")) is None


def test_alpha_png_cover_is_accepted(tmp_path):
    session = FakeSession([_resp(_png(mode="RGBA"))])
    cache = _cache(tmp_path, session)

    assert asyncio.run(cache.ensure("https://imge.kugou.com/a.png")) is not None


def test_ensure_many_returns_only_successes(tmp_path):
    good = "https://imge.kugou.com/good.jpg"
    bad = "https://imge.kugou.com/bad.jpg"
    session = FakeSession([_resp(_png()), _resp(b"boom", status=500)])
    cache = _cache(tmp_path, session)

    result = asyncio.run(cache.ensure_many([good, bad]))

    assert good in result and bad not in result


def test_ensure_many_fetches_repeated_url_once(tmp_path):
    """两行结果是同一张封面时，并发发起也只该抓一次。

    用每次取块前让出事件循环的响应制造真实交叠：若不去重，两次 ``ensure`` 会同时
    开始、都看不到对方的落盘结果，于是抓两次、并共用同一个 ``.part`` 临时文件。
    """

    class _YieldingResponse(FakeResponse):
        async def iter_chunked(self, size):
            await asyncio.sleep(0)              # 让出控制权，制造并发交叠
            async for chunk in super().iter_chunked(size):
                yield chunk

    url = "https://imge.kugou.com/same.jpg"
    session = FakeSession([_YieldingResponse([_png()]), _YieldingResponse([_png()])])
    cache = _cache(tmp_path, session)

    result = asyncio.run(cache.ensure_many([url, url, url]))

    assert result == {url: cache.path_for(url)}
    assert len(session.calls) == 1
