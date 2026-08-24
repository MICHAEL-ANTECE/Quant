"""终端对齐工具。

中文字符在终端占 2 列，但 Python 的 str.ljust / f-string 宽度按 1 个码位算，
所以中英混排的表头和数据列会错位。这里用 East Asian Width 重新实现补齐。
"""

from __future__ import annotations

import unicodedata

ANSI = dict(dim="\033[2m", bold="\033[1m", red="\033[31m", grn="\033[32m",
            yel="\033[33m", cyn="\033[36m", mag="\033[35m", off="\033[0m")


def c(s, key: str) -> str:
    return f"{ANSI[key]}{s}{ANSI['off']}"


def width(s: str) -> int:
    """字符串在终端占多少列（忽略 ANSI 转义序列）。"""
    out, i = 0, 0
    while i < len(s):
        if s[i] == "\033":                       # 跳过 ANSI 序列
            j = s.find("m", i)
            i = (j + 1) if j != -1 else len(s)
            continue
        out += 2 if unicodedata.east_asian_width(s[i]) in ("W", "F") else 1
        i += 1
    return out


def lj(s, n: int) -> str:
    """左对齐补齐到 n 列。"""
    s = str(s)
    return s + " " * max(0, n - width(s))


def rj(s, n: int) -> str:
    """右对齐补齐到 n 列。"""
    s = str(s)
    return " " * max(0, n - width(s)) + s


def trunc(s, n: int) -> str:
    """按显示宽度截断到 n 列。"""
    s = str(s)
    if width(s) <= n:
        return s
    out = ""
    for ch in s:
        if width(out + ch) > n:
            break
        out += ch
    return out
