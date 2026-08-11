# -*- coding: utf-8 -*-
"""前端 HTML 内联 JS 的轻量语法冒烟测试（无 node 环境下的大括号/引号平衡检查）。

运行: py -m unittest test.test_frontend_syntax -v
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HTML_FILES = ["web/index.html", "templates/index.html"]


def _check_balance(js):
    """检查 JS 文本中括号/花括号/方括号与引号是否平衡。

    只做词法级平衡检查（跳过字符串/注释内容），用于捕获粘贴错误级别的
    语法问题；完整语法校验仍需浏览器/node。
    """
    stack = []
    pairs = {")": "(", "]": "[", "}": "{"}
    line = 1
    i = 0
    n = len(js)
    while i < n:
        ch = js[i]
        if ch == "\n":
            line += 1
            i += 1
            continue
        # 注释：单行 // 与多行 /* */
        if ch == "/" and i + 1 < n and js[i + 1] == "/":
            while i < n and js[i] != "\n":
                i += 1
            continue
        if ch == "/" and i + 1 < n and js[i + 1] == "*":
            i += 2
            while i + 1 < n and not (js[i] == "*" and js[i + 1] == "/"):
                if js[i] == "\n":
                    line += 1
                i += 1
            i += 2
            continue
        # 字符串与模板串
        if ch in "\"'`":
            quote = ch
            i += 1
            while i < n:
                if js[i] == "\\":
                    i += 2
                    continue
                if js[i] == quote:
                    break
                if js[i] == "\n" and quote != "`":
                    raise AssertionError(f"字符串未闭合（行 {line}）")
                if js[i] == "\n":
                    line += 1
                i += 1
            if i >= n:
                raise AssertionError(f"字符串未闭合（行 {line}）")
            i += 1
            continue
        if ch in "([{":
            stack.append((ch, line))
        elif ch in ")]}":
            if not stack or stack[-1][0] != pairs[ch]:
                raise AssertionError(f"括号不匹配 {ch!r}（行 {line}）")
            stack.pop()
        i += 1
    if stack:
        raise AssertionError(f"存在未闭合括号 {stack[-1][0]!r}（行 {stack[-1][1]}）")


class FrontendSyntaxTest(unittest.TestCase):

    def test_inline_scripts_balanced(self):
        for name in HTML_FILES:
            text = (ROOT / name).read_text(encoding="utf-8")
            scripts = re.findall(r"<script>(.*?)</script>", text, re.S)
            self.assertTrue(scripts, f"{name} 应包含内联 <script>")
            for idx, js in enumerate(scripts):
                with self.subTest(file=name, script=idx):
                    _check_balance(js)


if __name__ == "__main__":
    unittest.main()
