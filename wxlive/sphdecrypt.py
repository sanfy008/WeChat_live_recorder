"""视频号媒体流解密 + 拉取。

微信视频号的媒体文件（finder.video.qq.com）返回时是**部分加密**的：
响应头 `X-encflag: 1` / `X-enclen: N` 表示前 N 字节被加密，之后是明文。
直接把它喂给 ffmpeg 会报 "Invalid data found when processing input"，
因为文件头（MP4 的 ftyp box）正好落在加密区内。

解密算法（逆向自 wx_channels_download / WechatSphDecrypt）：
    - 密钥是一个十进制整数，来自 joinLive/getFeed 响应的 metadata.key
    - 用 ISAAC64 伪随机生成器，以该密钥为种子
    - 密钥流按大端 8 字节一组，与密文逐字节 XOR，只处理前 encLen 字节

本模块只做两件事：
1. `Isaac64` + `decrypt_head()`：纯算法，无 IO
2. `open_decrypted(url)`：HTTP Range 拉流 + 流式解密，返回类文件对象给 ffmpeg
"""

from __future__ import annotations

import urllib.request
from typing import BinaryIO, Iterator

MASK64 = (1 << 64) - 1
GOLDEN = 0x9E3779B97F4A7C13

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36 NetType/WIFI "
    "MicroMessenger/7.0.20.1781(0x6700143B) WindowsWechat(0x63090a13) "
    "UnifiedPCWindowsWechat(0xf2541022) XWEB/16467 Flue"
)


def _mix(a, b, c, d, e, f, g, h):
    """ISAAC64 的 mix 轮，全部按 64 位无符号回绕。"""
    a = (a - e) & MASK64
    f ^= h >> 9
    h = (h + a) & MASK64
    b = (b - f) & MASK64
    g ^= (a << 9) & MASK64
    a = (a + b) & MASK64
    c = (c - g) & MASK64
    h ^= b >> 23
    b = (b + c) & MASK64
    d = (d - h) & MASK64
    a ^= (c << 15) & MASK64
    c = (c + d) & MASK64
    e = (e - a) & MASK64
    b ^= d >> 14
    d = (d + e) & MASK64
    f = (f - b) & MASK64
    c ^= (e << 20) & MASK64
    e = (e + f) & MASK64
    g = (g - c) & MASK64
    d ^= f >> 17
    f = (f + g) & MASK64
    h = (h - d) & MASK64
    e ^= (g << 14) & MASK64
    g = (g + h) & MASK64
    return a, b, c, d, e, f, g, h


class Isaac64:
    """ISAAC64 CSPRNG，用作密钥流生成器。"""

    def __init__(self, seed_key: int):
        self.seed = [0] * 256
        self.mm = [0] * 256
        self.aa = self.bb = self.cc = 0
        self.cnt = 255
        self._init(seed_key & MASK64)

    def _init(self, key: int) -> None:
        a = b = c = d = e = f = g = h = GOLDEN
        self.seed[0] = key

        for _ in range(4):
            a, b, c, d, e, f, g, h = _mix(a, b, c, d, e, f, g, h)

        for i in range(0, 256, 8):
            a = (a + self.seed[i]) & MASK64
            b = (b + self.seed[i + 1]) & MASK64
            c = (c + self.seed[i + 2]) & MASK64
            d = (d + self.seed[i + 3]) & MASK64
            e = (e + self.seed[i + 4]) & MASK64
            f = (f + self.seed[i + 5]) & MASK64
            g = (g + self.seed[i + 6]) & MASK64
            h = (h + self.seed[i + 7]) & MASK64
            a, b, c, d, e, f, g, h = _mix(a, b, c, d, e, f, g, h)
            self.mm[i:i + 8] = [a, b, c, d, e, f, g, h]

        for i in range(0, 256, 8):
            a = (a + self.mm[i]) & MASK64
            b = (b + self.mm[i + 1]) & MASK64
            c = (c + self.mm[i + 2]) & MASK64
            d = (d + self.mm[i + 3]) & MASK64
            e = (e + self.mm[i + 4]) & MASK64
            f = (f + self.mm[i + 5]) & MASK64
            g = (g + self.mm[i + 6]) & MASK64
            h = (h + self.mm[i + 7]) & MASK64
            a, b, c, d, e, f, g, h = _mix(a, b, c, d, e, f, g, h)
            self.mm[i:i + 8] = [a, b, c, d, e, f, g, h]

        self._isaac()

    def _isaac(self) -> None:
        self.cc = (self.cc + 1) & MASK64
        self.bb = (self.bb + self.cc) & MASK64
        for i in range(256):
            r = i % 4
            if r == 0:
                self.aa = (~(self.aa ^ ((self.aa << 21) & MASK64))) & MASK64
            elif r == 1:
                self.aa ^= self.aa >> 5
            elif r == 2:
                self.aa ^= (self.aa << 12) & MASK64
            else:
                self.aa ^= self.aa >> 33
            self.aa = (self.aa + self.mm[(i + 128) % 256]) & MASK64
            x = self.mm[i]
            y = (self.mm[(x >> 3) % 256] + self.aa + self.bb) & MASK64
            self.mm[i] = y
            self.bb = (self.mm[(y >> 11) % 256] + x) & MASK64
            self.seed[i] = self.bb

    def next64(self) -> int:
        result = self.seed[self.cnt]
        if self.cnt == 0:
            self._isaac()
            self.cnt = 255
        else:
            self.cnt -= 1
        return result


def keystream(seed_key: int, length: int) -> bytes:
    """生成 length 字节的密钥流。"""
    gen = Isaac64(seed_key)
    out = bytearray()
    while len(out) < length:
        out += gen.next64().to_bytes(8, "big")
    return bytes(out[:length])


def decrypt_head(data: bytes, enc_len: int, seed_key: int) -> bytes:
    """解密 data 的前 enc_len 字节，其余原样返回。

    enc_len <= 0 表示整个文件都加密（罕见，但接口有 full-enc 标记）。
    """
    if not data or enc_len <= 0:
        return data
    n = min(enc_len, len(data))
    ks = keystream(seed_key, n)
    out = bytearray(data)
    for i in range(n):
        out[i] ^= ks[i]
    return bytes(out)


class DecryptingStream:
    """对 HTTP 响应做流式解密的只读对象，供 ffmpeg stdin 使用。

    只有最开始 enc_len 字节需要解密，之后是明文。为了省内存，
    只对可能落在加密区内的块做 XOR。
    """

    def __init__(self, resp: BinaryIO, enc_len: int, seed_key: int, chunk: int = 1 << 16):
        self._resp = resp
        self._enc_len = max(0, enc_len)
        self._chunk = chunk
        self._consumed = 0
        self._ks = keystream(seed_key, self._enc_len) if self._enc_len else b""
        self._buf = b""

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            size = self._chunk
        data = self._resp.read(size)
        if not data:
            return b""
        start = self._consumed
        end = start + len(data)
        self._consumed = end
        if start >= self._enc_len:
            return data
        # 本块与加密区的重叠部分需要 XOR
        lo = start
        hi = min(end, self._enc_len)
        patched = bytearray(data)
        for i in range(lo, hi):
            patched[i - start] ^= self._ks[i]
        return bytes(patched)

    def close(self) -> None:
        self._resp.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def _headers(extra: dict | None = None) -> dict:
    h = {
        "User-Agent": DEFAULT_UA,
        "Referer": "https://channels.weixin.qq.com/",
        "Accept": "*/*",
        "Accept-Encoding": "identity",
    }
    if extra:
        h.update(extra)
    return h


def probe_encryption(url: str, timeout: int = 30) -> tuple[int, int]:
    """探测是否需要解密。

    返回 (enc_flag, enc_len)。enc_flag=0 表示明文，无需解密。

    坑：**必须发不带 Range 的请求**。带 `Range: bytes=0-15` 时，
    服务器返回的 `X-enclen` 会跟着缩成 16（只报这一段），
    而真实加密区通常是 131072（128KB）。用缩水的值去解密，
    只有前 16 字节被还原，`moov` 仍在密文里，ffmpeg 会报
    "moov atom not found"。
    """
    req = urllib.request.Request(url, headers=_headers(), method="HEAD")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            enc_flag = int(r.headers.get("X-encflag") or 0)
            enc_len = int(r.headers.get("X-enclen") or 0)
        return enc_flag, enc_len
    except Exception:
        # 有些 CDN 不支持 HEAD，退回 GET 并立刻关闭
        req = urllib.request.Request(url, headers=_headers())
        with urllib.request.urlopen(req, timeout=timeout) as r:
            enc_flag = int(r.headers.get("X-encflag") or 0)
            enc_len = int(r.headers.get("X-enclen") or 0)
        return enc_flag, enc_len


def open_decrypted(url: str, enc_len: int, seed_key: int,
                   timeout: int = 60) -> DecryptingStream:
    """打开解密流。enc_len=0 时不做解密，直接透传。"""
    req = urllib.request.Request(url, headers=_headers())
    resp = urllib.request.urlopen(req, timeout=timeout)
    if enc_len <= 0:
        return DecryptingStream(resp, 0, 0)
    return DecryptingStream(resp, enc_len, seed_key)


def iter_decrypted(url: str, enc_len: int, seed_key: int,
                   timeout: int = 60) -> Iterator[bytes]:
    """按块迭代解密后的内容，适合边拉边写。"""
    with open_decrypted(url, enc_len, seed_key, timeout) as stream:
        while True:
            chunk = stream.read(1 << 16)
            if not chunk:
                break
            yield chunk
