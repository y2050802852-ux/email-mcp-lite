"""IMAP 修改版 UTF-7 编解码（RFC 3501 §5.1.3）。

IMAP 文件夹名（如中文「已删除」）在线路上必须用修改版 UTF-7 编码，
标准库 imaplib 不做自动转换，这里提供 encode/decode。

与标准 UTF-7 的区别：用 ',' 代替 '/'，且不允许可选的直接字符。
"""

from __future__ import annotations

import base64

# 可直接表示的字符范围：0x20-0x7e，除 & 之外
_DIRECT = set(chr(c) for c in range(0x20, 0x7F)) - {"&"}


def encode(s: str) -> bytes:
    out = bytearray()
    buf: list[str] = []  # 连续的非直接字符段

    def flush() -> None:
        if not buf:
            return
        data = "".join(buf).encode("utf-16-be")
        b64 = base64.b64encode(data).decode("ascii").rstrip("=").replace("/", ",")
        out.extend(f"&{b64}-".encode("ascii"))
        buf.clear()

    for ch in s:
        if ch in _DIRECT:
            flush()
            out.extend(ch.encode("ascii"))
        elif ch == "&":
            flush()
            out.extend(b"&-")
        else:
            buf.append(ch)
    flush()
    return bytes(out)


def decode(b: bytes | str) -> str:
    if isinstance(b, str):
        b = b.encode("ascii", errors="replace")
    out: list[str] = []
    i = 0
    n = len(b)
    while i < n:
        c = b[i]
        if c == 0x26:  # '&'
            j = b.find(b"-", i + 1)
            if j == -1:
                # 未闭合的编码段：按 RFC 宽松处理，取到末尾
                j = n
            chunk = b[i + 1 : j]
            if not chunk:  # '&-' 是转义的 '&'
                out.append("&")
            else:
                b64 = chunk.replace(b",", b"/").decode("ascii")
                # 补齐 base64 padding
                b64 += "=" * (-len(b64) % 4)
                try:
                    out.append(base64.b64decode(b64).decode("utf-16-be"))
                except (ValueError, UnicodeDecodeError):
                    # 非法序列：原样保留，尽量不丢信息
                    out.append(b[i : j + 1].decode("ascii", errors="replace"))
            i = j + 1
        else:
            out.append(bytes([c]).decode("ascii"))
            i += 1
    return "".join(out)
