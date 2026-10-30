#!/usr/bin/env python3
"""设计令牌对比度检查（WCAG 2.1 AA）。

只读工具：解析 web/src/shared/styles/tokens.css 里的颜色令牌，按 CHECKS 表
逐对计算对比度并报告不达标项。正文文字要求 4.5:1，图形与边框要求 3:1。

用法：
    .venv/bin/python scripts/check_contrast.py          # 报告
    .venv/bin/python scripts/check_contrast.py --strict # 有不达标项时退出码 1

改动 tokens.css 后跑一次，避免"深一点的灰"这类调整把某个组合掉到线下。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

TOKENS = Path("web/src/shared/styles/tokens.css")

# (前景令牌, 背景令牌, 最低比值, 用途说明)
CHECKS: list[tuple[str, str, float, str]] = [
    ("--c-ink", "--c-paper", 4.5, "主文字 / 纸底"),
    ("--c-text-2", "--c-paper", 4.5, "次级文字 / 纸底"),
    ("--c-text-3", "--c-paper", 4.5, "元信息 / 纸底"),
    ("--c-text-3", "--c-white", 4.5, "元信息 / 白底"),
    ("--c-info", "--c-paper", 4.5, "链接 / 纸底"),
    ("--c-ok", "--c-paper", 4.5, "达标文字 / 纸底"),
    ("--c-ok", "--c-ok-bg", 4.5, "达标徽标文字 / 浅绿底"),
    ("--c-warn", "--c-paper", 4.5, "警示文字 / 纸底"),
    ("--c-warn", "--c-warn-bg", 4.5, "警示徽标文字 / 浅橙底"),
    ("--c-danger", "--c-paper", 4.5, "危险文字 / 纸底"),
    ("--c-danger-2", "--c-danger-bg", 4.5, "危险徽标文字 / 浅红底"),
    ("--c-white", "--c-danger-2", 4.5, "白字 / 危险按钮底"),
    ("--c-ink", "--c-yellow", 4.5, "黑字 / 强调黄底"),
    ("--c-ink", "--c-paper", 3.0, "边框与图形 / 纸底"),
]

# 只提示、不算失败的组合：装饰级浅灰只用于非信息性描边，
# 输入框还另有 focus 轮廓，不承载语义（tokens.css 已把它标为装饰）。
ADVISORY: list[tuple[str, str, float, str]] = [
    ("--c-muted", "--c-white", 3.0, "输入框底线 / 白底（装饰）"),
    ("--c-text-4", "--c-paper", 4.5, "占位与禁用文字 / 纸底（可豁免）"),
]


def parse_tokens(path: Path) -> dict[str, str]:
    """从 tokens.css 里抽出 `--name: #RRGGBB;` 形式的颜色令牌。"""
    text = path.read_text(encoding="utf-8")
    found: dict[str, str] = {}
    for name, value in re.findall(r"(--[\w-]+)\s*:\s*(#[0-9A-Fa-f]{6})\b", text):
        found.setdefault(name, value.upper())
    return found


def luminance(hex_color: str) -> float:
    """相对亮度（WCAG 定义）：先做 sRGB 反伽马，再按 0.2126/0.7152/0.0722 加权。"""
    h = hex_color.lstrip("#")
    channels = []
    for i in (0, 2, 4):
        c = int(h[i:i + 2], 16) / 255.0
        channels.append(c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4)
    return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2]


def ratio(fg: str, bg: str) -> float:
    a, b = luminance(fg), luminance(bg)
    hi, lo = max(a, b), min(a, b)
    return (hi + 0.05) / (lo + 0.05)


def main(argv: list[str]) -> int:
    strict = "--strict" in argv
    tokens = parse_tokens(TOKENS)
    missing = sorted({t for pair in CHECKS for t in pair[:2] if t not in tokens})
    for name in missing:
        print(f"未定义或不是 6 位十六进制颜色：{name}")

    bad = 0
    print(f"{'前景':<14}{'背景':<14}{'实测':>7}{'要求':>7}  用途")
    for fg, bg, need, note in CHECKS:
        if fg not in tokens or bg not in tokens:
            continue
        got = ratio(tokens[fg], tokens[bg])
        ok = got + 1e-9 >= need
        bad += 0 if ok else 1
        print(f"{fg:<14}{bg:<14}{got:>7.2f}{need:>7.1f}  {'✓' if ok else '✗'} {note}")

    for fg, bg, need, note in ADVISORY:
        if fg not in tokens or bg not in tokens:
            continue
        got = ratio(tokens[fg], tokens[bg])
        mark = "✓" if got + 1e-9 >= need else "·"
        print(f"{fg:<14}{bg:<14}{got:>7.2f}{need:>7.1f}  {mark} {note}")

    if bad:
        print(f"\n共 {bad} 组不达 AA，先改 tokens.css 再提交。")
        return 1 if strict else 0
    print("\n全部达标。")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
