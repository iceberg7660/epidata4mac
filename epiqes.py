"""EpiData .qes 问卷定义解析 + 与 .rec 字段的位置绑定。

.qes 为 GBK 文本。字段 = 占位符（连续 ``#``（可含 ``.#`` 小数）、连续 ``_``、
日期 ``<yyyy/mm/dd>``）；``{名称}`` 为字段名标记（部分字段无标记，由 EpiData
按行首单词自动命名，如 ``Z8 录入日期：<yyyy/mm/dd>`` → 字段 ``z8``）。

绑定规则（已用真实文件验证，551 字段全部对齐、0 宽度冲突）：
.rec 定义行中的 row = .qes 行号；同一行内占位符从左到右与该行的 .rec 字段
依次绑定，且占位符长度 == 字段存储宽度（日期占位符除外，按类型绑定）。

渲染时 ``{...}`` 标记以纯文字显示（去花括号），与原版 EpiData 录入界面一致。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field as dc_field

PH_PAT = re.compile(r'#+(?:\.#+)?|_+|<[ydm/]{4,12}>')
TOK_PAT = re.compile(r'\{[A-Za-z_][A-Za-z0-9_]*\}')


@dataclass
class Placeholder:
    line_no: int          # 1 起
    col: int              # 行内字符列（1 起）
    kind: str             # 'num' | 'str' | 'date'
    text: str             # 原始占位符文本
    name: str = ''        # 绑定到的 .rec 字段名


@dataclass
class QesFile:
    path: str = ''
    lines: list[str] = dc_field(default_factory=list)
    placeholders: list[Placeholder] = dc_field(default_factory=list)

    @classmethod
    def load(cls, path: str) -> 'QesFile':
        q = cls(path=path)
        raw = open(path, 'rb').read()
        q.lines = raw.decode('gbk', errors='replace').replace('\r\n', '\n').split('\n')
        for i, line in enumerate(q.lines):
            for m in PH_PAT.finditer(line):
                t = m.group(0)
                kind = 'date' if t.startswith('<') else ('num' if t[0] == '#' else 'str')
                q.placeholders.append(Placeholder(line_no=i + 1, col=m.start() + 1,
                                                  kind=kind, text=t))
        return q

    def bind(self, fields) -> None:
        """按（行号、行内顺序、宽度）把 .rec 字段绑到占位符上。"""
        for p in self.placeholders:
            p.name = ''
        by_line: dict[int, list[Placeholder]] = {}
        for p in self.placeholders:
            by_line.setdefault(p.line_no, []).append(p)
        cursor: dict[int, int] = {}
        unbound = []
        for f in fields:
            lst = by_line.get(f.row, [])
            i = cursor.get(f.row, 0)
            got = None
            while i < len(lst):
                p = lst[i]
                i += 1
                if p.name:
                    continue
                if p.kind == 'date' or len(p.text) == f.width:
                    got = p
                    break
            cursor[f.row] = i
            if got:
                got.name = f.name
                # 数值占位符带小数点 → 记录小数位数（如 ###.# → 1），供录入自动补零
                if got.kind == 'num' and '.' in got.text:
                    f.decimals = len(got.text.split('.')[1])
            else:
                unbound.append(f.name)
        if unbound:
            raise ValueError(f'.qes 中找不到这些字段的占位符: {unbound[:8]}')
        left = [p.line_no for p in self.placeholders if not p.name]
        if left:
            raise ValueError(f'.qes 中这些行的占位符没有对应字段: {sorted(set(left))[:8]}')

    def segments(self) -> list[list[dict]]:
        """每行 → [ {t:'text',s:..} | {t:'fld',n:字段名} ]，供前端渲染。
        {字段标记} 按原版 EpiData 一样以纯文字显示（仅去掉花括号）。"""
        ph_by_line: dict[int, list[Placeholder]] = {}
        for p in self.placeholders:
            ph_by_line.setdefault(p.line_no, []).append(p)
        show = lambda m: m.group(0)[1:-1]                # {X} → X
        out: list[list[dict]] = []
        for i, line in enumerate(self.lines, 1):
            parts: list[dict] = []
            pos = 0
            for p in ph_by_line.get(i, []):
                before = line[pos:p.col - 1]
                before = TOK_PAT.sub(show, before)
                if before:
                    parts.append({'t': 'text', 's': before})
                parts.append({'t': 'fld', 'n': p.name})
                pos = p.col - 1 + len(p.text)
            tail = TOK_PAT.sub(show, line[pos:])
            if tail:
                parts.append({'t': 'text', 's': tail})
            if not parts:
                parts = [{'t': 'text', 's': ''}]
            out.append(parts)
        return out
