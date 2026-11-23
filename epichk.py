"""EpiData .chk 校验规则解析与执行（对照官方 EpiData 3.1 文档）。

参与数据校验/流程的命令：RANGE / LEGAL（含 USE、逗号与区间写法）/ COMMENT LEGAL /
MISSINGVALUE / MUSTENTER / REPEAT / JUMPS（含 RESET）/ AUTOJUMP /
BEFORE ENTRY / AFTER ENTRY 的 IF..THEN..ELSE..ENDIF、GOTO、LET、CLEAR。
纯展示或桌面集成类命令（HELP/TYPE/COLOR/BEEP/BACKUP/EXECUTE/RELATE 等）只识别，
不参与本网页版的数据校验。

文件结构：无缩进行 = 字段名开始一个块；缩进行 = 命令；字段块以无缩进 ``END``
结束（子命令的 ``END`` 有缩进）。以 ``*`` 开头的行是注释。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field as dc_field

IF_PAT = re.compile(r'^IF\s+(.+?)\s+THEN\s*$', re.IGNORECASE)
IF_INLINE_PAT = re.compile(r'^IF\s+(.+?)\s+THEN\s+(.+)$', re.IGNORECASE)
POS_COND_PAT = re.compile(
    r'^POS\(\s*(["\'])(.*?)\1\s*,\s*([A-Za-z_][A-Za-z0-9_]*)\s*\)\s*(<>|!=|=|>|<)\s*(\d+)$',
    re.IGNORECASE)
CMP_COND_PAT = re.compile(
    r'^([A-Za-z_][A-Za-z0-9_]*)\s*(<>|!=|>=|<=|=|>|<)\s*(.+)$', re.IGNORECASE)
GOTO_PAT = re.compile(
    r'^GOTO(?:\s+([A-Za-z_][A-Za-z0-9_]*|END|WRITE|WRITEREC|SKIPNEXTFIELD))?$', re.IGNORECASE)
CLEAR_PAT = re.compile(r'^CLEAR(?:\s+([A-Za-z_][A-Za-z0-9_]*))?$', re.IGNORECASE)
ASSIGN_PAT = re.compile(r'^(?:LET\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.+)$', re.IGNORECASE)
HELP_PAT = re.compile(r'^HELP\s+"(.*?)"', re.IGNORECASE)
TYPE_PAT = re.compile(r'^TYPE\s+"(.*?)"', re.IGNORECASE)
CHECK_PAT = re.compile(r'^CHECK\s+"(.*?)"\s+(.+)$', re.IGNORECASE)
MISSING_PAT = re.compile(r'^MISSINGVALUE\s*(.*)$', re.IGNORECASE)
LEGAL_ITEM_PAT = re.compile(r'^(-?\d+(?:\.\d+)?)\s*-\s*(-?\d+(?:\.\d+)?)$')
END_LINE = re.compile(r'^END\s*$', re.IGNORECASE)

# 识别但不执行的命令（展示、环境、桌面集成）
NOOP_PREFIXES = ('BEEP', 'COLOR', 'CONFIRM', 'HIDE', 'UNHIDE', 'NOENTER',
                 'COMMENT LEGAL', 'CLEAR COMMENT LEGAL',
                 'AUTOSAVE', 'SHOWLASTRECORD', 'TOPOFSCREEN', 'TYPE COMMENT',
                 'TYPE STATUSBAR', 'TYPE=', 'WRITENOTE', 'COPYTOCLIPBOARD', 'DEFINE',
                 'IGNOREMISSING', 'EXIT', 'QUIT', 'KEY', 'LOAD')


@dataclass
class Action:
    kind: str                    # goto / clear / let / help / type / noop
    target: str = ''
    value: str = ''
    text: str = ''


@dataclass
class Condition:
    kind: str                    # cmp / pos / noop
    field: str = ''
    op: str = '='
    value: str = ''
    value_raw: str = ''          # 比较值原文（含引号），供前端区分字面量与表达式
    threshold: str = '0'         # pos() 比较阈值（官方写法通常为 0）
    then_actions: list[Action] = dc_field(default_factory=list)
    else_actions: list[Action] = dc_field(default_factory=list)


@dataclass
class LetRule:
    """由 IF pos(...) THEN LET ... ELSE LET ... 推导出的条件赋值（兼容旧字段）。"""
    source: str
    needle: str
    target: str
    on_found: str
    on_missing: str


@dataclass
class Rule:
    name: str
    rng: tuple[str, str] | None = None
    legal: list[str] = dc_field(default_factory=list)
    legal_ref: str | None = None
    comment_legal: list[str] = dc_field(default_factory=list)
    comment_ref: str | None = None
    comment_file: str | None = None    # COMMENT LEGAL 引用的外部 .rec 查询表
    key_unique: bool = False
    key_no: int | None = None
    lookup_labels: dict[str, str] = dc_field(default_factory=dict)
    missingvalues: list[str] = dc_field(default_factory=list)
    mustenter: bool = False
    repeat: bool = False
    jumps: dict[str, str] = dc_field(default_factory=dict)   # 录入值 -> 跳转目标
    jumps_reset: str | None = None      # JUMPS RESET 的填充字符（None=不填）
    autojump: str | None = None         # AUTOJUMP 目标
    # 以下为兼容旧版前端的派生字段
    gotos: list[tuple[str, str, str]] = dc_field(default_factory=list)
    after_goto: str | None = None
    lets: list[LetRule] = dc_field(default_factory=list)
    # 新版通用动作（IF 条件可嵌套在 then_actions / else_actions 中）
    actions: list[Action | Condition] = dc_field(default_factory=list)
    before_actions: list[Action | Condition] = dc_field(default_factory=list)
    clears: list[str] = dc_field(default_factory=list)


def _clean_value(raw: str) -> str:
    s = raw.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ('"', "'"):
        return s[1:-1]
    return '' if s == '.' else s


def _expand_legal(tokens: list[str]) -> list[str]:
    """LEGAL 支持空格/逗号分隔与 `2-6` 区间写法（官方 Range/Legal 编辑器语义）。"""
    out: list[str] = []
    for tok in tokens:
        for piece in tok.split(','):
            s = piece.strip().strip('"\'')
            if not s:
                continue
            m = LEGAL_ITEM_PAT.match(s)
            if m:
                lo, hi = float(m.group(1)), float(m.group(2))
                if 0 <= hi - lo <= 1000:
                    out.extend(str(int(x)) if float(x).is_integer() else str(x)
                               for x in (lo + i for i in range(int(hi - lo) + 1)))
                    continue
            out.append(s)
    return list(dict.fromkeys(out))


def _parse_condition(expr: str) -> Condition | None:
    expr = expr.strip()
    m = POS_COND_PAT.match(expr)          # pos("x",field) <> 0
    if m:
        return Condition(kind='pos', field=m.group(3).lower(),
                         op='<>' if m.group(4) == '!=' else m.group(4),
                         value=m.group(2), threshold=m.group(5))
    m = CMP_COND_PAT.match(expr)          # field >= value
    if m:
        fld, op, val = m.groups()
        return Condition('cmp', fld.lower(), '<>' if op == '!=' else op,
                         _clean_value(val), value_raw=val.strip())
    # 复合条件（如 (V1>2) AND (V2=1)）交给前端表达式求值器
    return Condition('expr', value=expr)


def _parse_action(s: str) -> Action | None:
    up = s.upper()
    m = GOTO_PAT.match(s)
    if m:
        return Action('goto', target=(m.group(1) or '').lower())
    m = CLEAR_PAT.match(s)
    if m:
        if up.startswith('CLEAR COMMENT'):
            return Action('noop')
        return Action('clear', target=(m.group(1) or '').lower())
    m = HELP_PAT.match(s)
    if m:
        return Action('help', text=m.group(1))
    m = TYPE_PAT.match(s)
    if m:
        return Action('type', text=m.group(1))
    m = CHECK_PAT.match(s)
    if m:
        return Action('check', text=m.group(1), value=m.group(2))
    m = ASSIGN_PAT.match(s)
    if m:
        return Action('let', target=m.group(1).lower(), value=_clean_value(m.group(2)))
    if any(up.startswith(p) or up == p for p in NOOP_PREFIXES):
        return Action('noop')
    return None


def _parse_actions(lines: list[str], warnings: list[str]) -> list[Action | Condition]:
    acts: list[Action | Condition] = []
    i = 0
    while i < len(lines):
        s = lines[i].strip()
        if not s or s.startswith('*') or s.upper() in ('END', 'ENDIF', 'ELSE'):
            i += 1
            continue
        if IF_PAT.match(s):
            cond, i = _parse_if(lines, i + 1, IF_PAT.match(s).group(1), warnings)
            acts.append(cond)
            continue
        m = IF_INLINE_PAT.match(s)         # IF x=1 THEN GOTO y（同一行）
        if m and not IF_PAT.match(s):
            cond = _parse_condition(m.group(1))
            inline = m.group(2).strip()
            if inline.upper().endswith('ENDIF'):
                inline = inline[:-5].strip()
            act = _parse_action(inline)
            if cond and act:
                cond.then_actions = [act]
                acts.append(cond)
                i += 1
                continue
        act = _parse_action(s)
        if act:
            acts.append(act)
        else:
            warnings.append(f'命令未支持: {s[:50]}')
        i += 1
    return acts


def _parse_if(lines: list[str], i: int, expr: str,
              warnings: list[str]) -> tuple[Condition, int]:
    cond = _parse_condition(expr)
    if cond is None:
        warnings.append(f'IF 条件未支持: {expr[:50]}')
        cond = Condition('noop')
    then_lines: list[str] = []
    else_lines: list[str] = []
    depth = 0
    in_else = False
    while i < len(lines):
        t = lines[i].strip()
        if IF_PAT.match(t):
            depth += 1
        elif t.upper().startswith('ENDIF'):
            if depth == 0:
                i += 1
                break
            depth -= 1
        elif t.upper() == 'ELSE' and depth == 0:
            in_else = True
            i += 1
            continue
        (else_lines if in_else else then_lines).append(lines[i])
        i += 1
    cond.then_actions = _parse_actions(then_lines, warnings)
    cond.else_actions = _parse_actions(else_lines, warnings)
    return cond, i


def _parse_labelblocks(lines: list[str]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for idx, raw in enumerate(lines):
        s = raw.strip()
        m = re.match(r'^LABEL\s+([A-Za-z_][A-Za-z0-9_]*)', s, re.IGNORECASE)
        if not m:
            continue
        vals: list[str] = []
        for line in lines[idx + 1:]:
            if line.strip().upper() == 'END':
                break
            parts = line.split()
            if parts:
                vals.append(parts[0] if re.match(r'^-?\d', parts[0]) else ' '.join(parts))
        out[m.group(1).lower()] = vals
    return out


def _derive_compat_fields(r: Rule) -> None:
    """从通用动作派生旧版前端字段，保证旧调用方不变。"""
    for act in r.actions:
        if isinstance(act, Condition):
            if act.kind == 'pos' and len(act.then_actions) == 1 and len(act.else_actions) == 1:
                a, b = act.then_actions[0], act.else_actions[0]
                if a.kind == b.kind == 'let':
                    if act.op == '=':
                        a, b = b, a
                    r.lets.append(LetRule(act.field, act.value, a.target, a.value, b.value))
            elif act.kind == 'cmp' and act.op == '=' and len(act.then_actions) == 1:
                a = act.then_actions[0]
                if a.kind == 'goto':
                    r.gotos.append((act.field, act.value, a.target))
        elif act.kind == 'goto':
            r.after_goto = act.target or None
        elif act.kind == 'clear':
            r.clears.append(act.target)


class ChkFile:
    def __init__(self) -> None:
        self.rules: dict[str, Rule] = {}
        self.warnings: list[str] = []
        self.label_blocks: dict[str, list[str]] = {}
        self.global_missing: list[str] = []
        self.field_missing_specs: list[tuple[str, list[str]]] = []
        self.ignore_missing: bool = False
        self.before_record_actions: list[Action | Condition] = []
        self.after_record_actions: list[Action | Condition] = []

    @classmethod
    def load(cls, path: str) -> 'ChkFile':
        c = cls()
        raw = open(path, 'rb').read()
        text = raw.decode('gbk', errors='replace').replace('\r\n', '\n')
        c.ignore_missing = bool(re.search(r'(?im)^\s*IGNOREMISSING\b', text))
        lines = text.split('\n')
        i = 0
        while i < len(lines):
            line = lines[i]
            s = line.strip()
            if not s or s.startswith('*') or END_LINE.match(line):
                i += 1
                continue
            up = s.upper()
            if up in ('BEFORE ENTRY', 'AFTER ENTRY'):
                block, i = c._collect_block(lines, i + 1, stripped_end=True)
                _parse_actions(block, c.warnings)   # 不挂在字段上的 ENTRY 块：只解析避免误判为字段
                continue
            if ' ' in s and up not in ('LABELBLOCK', 'BEFORE FILE', 'AFTER FILE',
                                       'BEFORE RECORD', 'AFTER RECORD'):
                # 顶层字段名只能是单个词；多词行是缩进异常的普通命令
                _parse_actions([line], c.warnings)
                i += 1
                continue
            if up == 'LABELBLOCK':
                block, i = c._collect_block(lines, i + 1)
                c.label_blocks.update(_parse_labelblocks(block))
                continue
            if up in ('BEFORE FILE', 'AFTER FILE', 'BEFORE RECORD', 'AFTER RECORD'):
                block, i = c._collect_block(lines, i + 1)
                if up == 'BEFORE FILE':
                    c._parse_before_file(block)
                elif up == 'BEFORE RECORD':
                    c.before_record_actions = _parse_actions(block, c.warnings)
                elif up == 'AFTER RECORD':
                    c.after_record_actions = _parse_actions(block, c.warnings)
                continue
            name = s.split()[0].lower()
            body, i = c._collect_block(lines, i + 1)
            c.rules[name] = c._parse_body(name, body, c.warnings)
        c._resolve()
        return c

    @staticmethod
    def _collect_block(lines: list[str], i: int,
                       stripped_end: bool = False) -> tuple[list[str], int]:
        """收集到 END 为止。子块（AFTER ENTRY 等）的 END 有缩进，需 stripped_end=True；
        字段块/文件块的 END 无缩进，用默认。"""
        body: list[str] = []
        while i < len(lines):
            is_end = (END_LINE.match(lines[i].strip()) if stripped_end
                      else END_LINE.match(lines[i]))
            if is_end:
                break
            body.append(lines[i])
            i += 1
        return body, i + 1

    def _parse_before_file(self, block: list[str]) -> None:
        for raw in block:
            m = MISSING_PAT.match(raw.strip())
            if not m:
                continue
            parts = m.group(1).split()
            if not parts:
                continue
            if parts[0].upper() == 'ALL':
                self.global_missing.extend(parts[1:])
            else:
                # 字段清单或范围由 app.py 按 .rec 字段顺序展开
                vals = [p for p in parts[1:] if re.match(r'^-?\d+$', p)]
                if vals:
                    self.field_missing_specs.append((' '.join(parts[:len(parts) - len(vals)]), vals))

    def _parse_body(self, name: str, body: list[str],
                    warnings: list[str] | None = None) -> Rule:
        r = Rule(name=name)
        warn = warnings if warnings is not None else []
        i = 0
        while i < len(body):
            t = body[i].strip()
            up = t.upper()
            if not t or t.startswith('*'):
                i += 1
                continue
            if up.startswith('RANGE '):
                parts = t.split()
                if len(parts) >= 3:
                    r.rng = (parts[1], parts[2])
            elif up == 'MUSTENTER':
                r.mustenter = True
            elif up == 'REPEAT':
                r.repeat = True
            elif up == 'LEGAL USE' or up.startswith('LEGAL USE '):
                r.legal_ref = t.split()[-1].lower()
            elif up == 'LEGAL' or up.startswith('LEGAL '):
                vals = _expand_legal(t.split()[1:])
                i += 1
                more = []
                while i < len(body) and body[i].strip().upper() != 'END':
                    parts = body[i].split()
                    if parts:
                        more.append(parts[0])
                    i += 1
                r.legal.extend(vals + _expand_legal(more))
            elif up == 'COMMENT LEGAL USE' or up.startswith('COMMENT LEGAL USE '):
                parts = t.split()
                k = next((j for j, p in enumerate(parts) if p.upper() == 'USE'), 2)
                r.comment_ref = parts[k + 1].lower() if len(parts) > k + 1 else ''
            elif up == 'COMMENT LEGAL' or up.startswith('COMMENT LEGAL '):
                parts = t.split()
                inline = parts[2:]
                file_ref = re.match(r'^([\w\-.]+\.rec)$', inline[0], re.IGNORECASE) if inline else None
                if file_ref:
                    r.comment_file = file_ref.group(1)
                else:
                    i += 1
                    while i < len(body) and body[i].strip().upper() != 'END':
                        inline.append(body[i].strip())
                        i += 1
                    for line in inline:
                        p = line.split()
                        if p:
                            r.comment_legal.append(p[0] if re.match(r'^-?\d', p[0]) else line)
                    r.legal.extend(r.comment_legal)
            elif up == 'KEY' or up.startswith('KEY '):
                parts = t.split()
                r.key_unique = any(p.upper() == 'UNIQUE' for p in parts)
                nums = [p for p in parts if p.isdigit()]
                r.key_no = int(nums[0]) if nums else None
            elif up == 'JUMPS' or up.startswith('JUMPS '):
                tokens = t.split()[1:]
                if tokens and tokens[0].upper() == 'RESET':
                    tokens = tokens[1:]
                    if tokens and len(tokens[0]) == 1 and '>' not in tokens[0]:
                        r.jumps_reset = tokens.pop(0)
                i += 1
                while i < len(body) and body[i].strip().upper() != 'END':
                    tokens.extend(body[i].split())
                    i += 1
                # 官方语法允许 `1 V5 2 WRITE END` 同行，也允许逐行；兼容 GUI 的 `1>V5`
                k = 0
                while k < len(tokens):
                    tok = tokens[k].rstrip(',')
                    if tok.upper() == 'END':
                        break
                    if '>' in tok:
                        a, b = tok.split('>', 1)
                        r.jumps[a] = b.lower()
                        k += 1
                        continue
                    if k + 1 < len(tokens):
                        r.jumps[tok] = tokens[k + 1].rstrip(',').lower()
                        k += 2
                    else:
                        k += 1
            elif up.startswith('AUTOJUMP '):
                r.autojump = t.split()[1].lower()
                i += 1
            elif up in ('AFTER ENTRY', 'BEFORE ENTRY'):
                seg, i = self._collect_block(body, i + 1, stripped_end=True)
                # _collect_block 把块尾 END 也跳过，这里 i 已经指向下一条命令
                acts = _parse_actions(seg, warn)
                if up == 'AFTER ENTRY':
                    r.actions = acts
                else:
                    r.before_actions = acts
                continue
            elif up.startswith('MISSINGVALUE'):
                m = MISSING_PAT.match(t)
                if m and m.group(1).strip():
                    r.missingvalues.extend(m.group(1).split())
                elif m:
                    digits = re.search(r'(\d(?:\s+\d){0,2})$', up)
                    if digits:
                        r.missingvalues.extend(digits.group(1).split())
            elif not _parse_action(t):
                warn.append(f'字段 {name} 的命令未支持: {t[:40]}')
            i += 1
        _derive_compat_fields(r)
        return r

    def _resolve(self) -> None:
        for r in self.rules.values():
            if r.legal_ref:
                src = self.rules.get(r.legal_ref)
                if src:
                    r.legal = list(dict.fromkeys(r.legal + src.legal))
                else:
                    self.warnings.append(f'字段 {r.name} 的 LEGAL USE 找不到字段 {r.legal_ref}')
            if r.comment_ref:
                src = self.rules.get(r.comment_ref)
                vals = (src.comment_legal or src.legal) if src else self.label_blocks.get(r.comment_ref)
                if vals:
                    r.comment_legal = list(vals)
                    r.legal = list(dict.fromkeys(r.legal + [v for v in vals if v]))
                # 找不到时留给 app.py 在同目录其它 .chk 中查找标注块
            for v in self.global_missing + r.missingvalues:
                if v not in r.legal:
                    r.legal.append(v)


def _num(s: str) -> float | None:
    t = (s or '').strip().upper()
    if t in ('INF', 'INFINITY'):
        return float('inf')
    if t in ('-INF', '-INFINITY'):
        return float('-inf')
    try:
        return float(s)
    except ValueError:
        return None


def _date_num(s: str) -> tuple[int, int, int] | None:
    m = re.fullmatch(r'(\d{1,2})[/\-](\d{1,2})[/\-](\d{2,4})', (s or '').strip())
    if not m:
        return None
    d, mo, y = map(int, m.groups())
    if y < 100:
        y += 2000 if y < 50 else 1900
    try:
        return y, mo, d
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
    # RANGE 与 LEGAL / MISSINGVALUE 同时出现时，后者是范围外的附加合法码
    extras = list(dict.fromkeys(rule.legal + rule.missingvalues))
    legal_ok = bool(extras and v in extras)
    if rule.rng and not legal_ok:
        lo, hi = rule.rng
        dlo, dhi = _date_num(lo), _date_num(hi)
        if dlo and dhi:
            dv = _date_num(v)
            if dv is None:
                return f'日期格式应为 dd/mm/yyyy'
            bad = not (dlo <= dv <= dhi)
        else:
            nv, nlo, nhi = _num(v), _num(lo), _num(hi)
            if nv is None:
                return f'数值字段，{v!r} 不是有效数字'
            bad = nlo is not None and nhi is not None and not (nlo <= nv <= nhi)
        if bad:
            tail = f'（{", ".join(extras)} 为合法附加码）' if extras else ''
            return f'超出范围 {lo} ~ {hi}{tail}'
    elif not rule.rng and extras and not legal_ok:
        return f'合法值仅为: {", ".join(extras)}'
    elif kind == '#' and _num(v) is None and not legal_ok:
        return f'数值字段，{v!r} 不是有效数字'
    return None
