"""代码风格自检工具"""

from __future__ import annotations

import os
import sys
import unicodedata
from typing import List, Tuple

# 允许出现的非 ASCII 字符区间
ALLOWED_RANGES: Tuple[Tuple[int, int], ...] = (
    (0x00B7, 0x00B7),
    (0x2018, 0x2019),
    (0x201C, 0x201D),
    (0x2014, 0x2014),
    (0x2026, 0x2026),
    (0x3000, 0x303F),
    (0x4E00, 0x9FFF),
    (0xFF00, 0xFFEF),
)

# 需要检查的文件
TARGET_FILES = ("app.py",)
TARGET_DIRECTORIES = ("linxia",)


def is_allowed(character: str) -> bool:
    """判断单个字符是否白名单内。"""
    code = ord(character)
    if code < 0x80:
        return True
    for low, high in ALLOWED_RANGES:
        if low <= code <= high:
            return True
    return False


def collect_files(root: str) -> List[str]:
    """收集所有待检查的 Python 文件。"""
    files: List[str] = []
    for directory in TARGET_DIRECTORIES:
        target = os.path.join(root, directory)
        if not os.path.isdir(target):
            continue
        for current, _, names in os.walk(target):
            for name in names:
                if name.endswith(".py"):
                    files.append(os.path.join(current, name))
    for name in TARGET_FILES:
        target = os.path.join(root, name)
        if os.path.isfile(target):
            files.append(target)
    return sorted(files)


def check_file(path: str, root: str) -> int:
    """检查单个文件，返回发现的问题数量。"""
    problems = 0
    relative = os.path.relpath(path, root)
    with open(path, "r", encoding="utf-8") as handle:
        for number, line in enumerate(handle, start=1):
            for column, character in enumerate(line, start=1):
                if is_allowed(character):
                    continue
                problems += 1
                label = unicodedata.name(character, "未知字符")
                print(
                    f"{relative}:{number}:{column}  "
                    f"{character!r}  U+{ord(character):04X}  {label}"
                )
    return problems


def main() -> int:
    """入口函数。"""
    root = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))
    )

    files = collect_files(root)
    if not files:
        print(f"没有在 {root} 下找到可检查的 Python 文件")
        return 1

    total = 0
    for path in files:
        total += check_file(path, root)

    if total == 0:
        print(f"风格检查通过，共检查 {len(files)} 个文件，未发现违规符号")
        return 0

    print(f"风格检查未通过，共发现 {total} 处违规符号")
    return 1


if __name__ == "__main__":
    sys.exit(main())
