"""EpiData .chk 校验规则解析与校验执行。

支持的命令（本调查 .chk 用到的全集）：RANGE / LEGAL / MUSTENTER / REPEAT /
JUMPS..END / AFTER ENTRY .. IF x = v THEN GOTO field .. ENDIF .. END。

文件结构：无缩进行 = 字段名开始一个块；缩进行 = 命令；字段块以无缩进 ``END``
结束（子命令的 ``END`` 有缩进）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field as dc_field

IF_GOTO_PAT = re.compile(
    r'IF\s+([A-Za-z_][A-Za-z0-9_]*)\s*=\s*("?[^"\s]+"?)\s+THEN\s+GOTO\s+([A-Za-z_][A-Za-z0-9_]*)',
    re.IGNORECASE)


END_LINE = re.compile(r'^END\s*$')   # 字段块结束标记：必须无缩进（子命令 END 有缩进）


@dataclass
class Rule:
    name: str
    rng: tuple[str, str] | None = None          # (min, max)
    legal: list[str] = dc_field(default_factory=list)
    mustenter: bool = False
    repeat: bool = False
    jumps: dict[str, str] = dc_field(default_factory=dict)      # 录入值 -> 跳转字段
    gotos: list[tuple[str, str, str]] = dc_field(default_factory=list)  # (变量, 值, 目标)


class ChkFile:
    def __init__(self) -> None:
        self.rules: dict[str, Rule] = {}
        self.warnings: list[str] = []    # 未能解析的命令（提示但不阻断）

    @classmethod
    def load(cls, path: str) -> 'ChkFile':
        c = cls()
        raw = open(path, 'rb').read()
        text = raw.decode('gbk', errors='replace')
        lines = text.replace('\r\n', '\n').split('\n')
        i = 0
        while i < len(lines):
            s = lines[i].strip()
            if not s or END_LINE.match(lines[i]):
                i += 1
                continue
            name = s.split()[0].lower()
            body = []
            i += 1
            while i < len(lines) and not END_LINE.match(lines[i]):
                body.append(lines[i])
                i += 1
            i += 1  # 跳过块结束 END
            c.rules[name] = c._parse_body(name, body, c.warnings)
        return c

    @staticmethod
    def _parse_body(name: str, body: list[str],
                    warnings: list[str] | None = None) -> Rule:
        r = Rule(name=name)
        i = 0
        while i < len(body):
            t = body[i].strip()
            up = t.upper()
            if up.startswith('RANGE '):
                parts = t.split()
                if len(parts) >= 3:
                    r.rng = (parts[1], parts[2])
            elif up == 'MUSTENTER':
                r.mustenter = True
            elif up == 'REPEAT':
                r.repeat = True
            elif up == 'LEGAL':
                i += 1
                while i < len(body) and body[i].strip().upper() != 'END':
                    v = body[i].strip()
                    if v:
                        r.legal.append(v)
                    i += 1
            elif up == 'JUMPS':
                i += 1
                while i < len(body) and body[i].strip().upper() != 'END':
                    parts = body[i].split()
                    if len(parts) >= 2:
                        r.jumps[parts[0]] = parts[1].lower()
                    i += 1
            elif up == 'AFTER ENTRY':
                seg = []
                i += 1
                while i < len(body) and body[i].strip().upper() != 'END':
                    seg.append(body[i])
                    i += 1
                joined = ' '.join(x.strip() for x in seg)
                for m in IF_GOTO_PAT.finditer(joined):
                    r.gotos.append((m.group(1).lower(), m.group(2).strip('"'), m.group(3).lower()))
                # AFTER ENTRY 里有没被识别的逻辑则提示
                leftover = IF_GOTO_PAT.sub('', joined)
                leftover = re.sub(r'ENDIF|END', '', leftover, flags=re.IGNORECASE)
                if leftover.strip(' ;\t'):
                    if warnings is not None:
                        warnings.append(f'字段 {name} 的 AFTER ENTRY 含未支持逻辑: '
                                        f'{leftover.strip()[:60]}')
            elif up and up != 'END':
                if warnings is not None:
                    warnings.append(f'字段 {name} 的命令未支持: {t[:40]}')
            i += 1
        return r


def _num(s: str) -> float | None:
    try:
        return float(s)
    except ValueError:
        return None


def validate_value(rule: Rule | None, value: str, kind: str) -> str | None:
    """返回错误信息；None = 通过。kind: '#'|'_' （用于数值格式检查）。"""
    v = (value or '').strip()
    if not v:
        if rule and rule.mustenter:
            return '必填字段（MUSTENTER），不能为空'
        return None
    if rule is None:
        if kind == '#' and _num(v) is None:
            return f'数值字段，{v!r} 不是有效数字'
        return None
    if rule.legal and v not in rule.legal:
        return f'合法值仅为: {", ".join(rule.legal)}'
    if rule.rng:
        lo, hi = rule.rng
        nv, nlo, nhi = _num(v), _num(lo), _num(hi)
        if nv is None:
            return f'数值字段，{v!r} 不是有效数字'
        if nlo is not None and nhi is not None and not (nlo <= nv <= nhi):
            return f'超出范围 {lo} ~ {hi}'
    elif kind == '#' and _num(v) is None:
        return f'数值字段，{v!r} 不是有效数字'
    return None
