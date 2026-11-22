"""EpiData Entry .rec 文件读写（Epi Info 6 风格文本格式，GBK 编码）。

格式（已用真实文件字节级 round-trip 验证，见 tests/test_roundtrip.py）：

  第 1 行       文件头，如 ``616 1 VLAB``（第一个数字 = 定义行数，原样保留）
  随后 N 行     字段定义行（原样保留字节），ASCII 结构前缀::

                  <#|_><name> n1 row 30 col row2 typecode width 112 <问题文本(GBK字节)>

                ``#`` = 数值字段、``_`` = 字符字段；``_labelN``（width=0）为标题行，
                不占记录空间。width 按 GBK 字节计（中文 1 字 = 2 字节）。
                注意：原文件个别行的问题文本被 EpiData 在 ~123 字节处截断在
                GBK 双字节中间（自带 bug），故定义行必须按原始字节保留。
  其余行        数据记录：记录体固定 record_width 字节（= 各字段宽度之和），
                按 78 字符断行，每个物理行末尾加 ``!``，CRLF 换行。

字段值存储：数值字段右对齐左侧补空格；字符串字段左对齐右侧补空格；空值全空格。
"""
from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass

DEF_PAT = re.compile(
    rb'^(?P<pre>[#_])(?P<name>[A-Za-z0-9_]+)\s+'
    rb'(?P<n1>\d+)\s+(?P<row>\d+)\s+(?P<n3>\d+)\s+(?P<col>\d+)\s+'
    rb'(?P<row2>\d+)\s+(?P<tc>\d+)\s+(?P<width>\d+)\s+112 ?')
HEADER_PAT = re.compile(rb'^\s*(\d+)\s+(\d+)\s+VLAB\s*$')
LINE_CHUNK = 78


class RecError(ValueError):
    """格式不符合预期时抛出，带可读信息。"""


@dataclass
class Field:
    name: str          # 字段名（不含前缀，与 .chk/.qes 一致，全小写）
    kind: str          # '#' 数值 / '_' 字符
    row: int           # 在 .qes 中的行号（1 起）
    col: int           # 在 .qes 中的列号（1 起，按字节）
    width: int         # 存储宽度（字节）
    typecode: int      # 0 整数 / 1 字符串 / 6 长编号 / 19 日期 / 101 带小数
    text: str          # 定义行内的问题文本（可能有截断/乱码，仅展示用）
    line_no: int       # 在 .rec 中的定义行号（1 起）
    decimals: int = 0  # 小数位数（由 .qes 占位符解析，如 ###.# → 1）


@dataclass
class Label:
    name: str          # 如 label1
    row: int           # 在 .qes 中的行号
    col: int           # 在 .qes 中的列号（用于区分章节标题与行内碎片文字）
    text: str


class RecFile:
    def __init__(self) -> None:
        self.path: str = ''
        self.header_line: bytes = b''
        self.n_defs: int = 0
        self.def_lines: list[bytes] = []      # 原始字节，原样保留
        self.fields: list[Field] = []
        self.labels: list[Label] = []
        self.by_name: dict[str, Field] = {}
        self.record_width: int = 0            # 各字段宽度之和
        self.records: list[dict[str, str]] = []

    # ---------- 读取 ----------

    @classmethod
    def load(cls, path: str) -> 'RecFile':
        rf = cls()
        rf.path = path
        with open(path, 'rb') as f:
            data = f.read()
        lines = data.split(b'\r\n')
        if lines and lines[-1] == b'':
            lines.pop()
        if not lines:
            raise RecError('文件为空')
        m = HEADER_PAT.match(lines[0])
        if not m:
            raise RecError(f'文件头不识别: {lines[0][:40]!r}')
        rf.header_line = lines[0]
        rf.n_defs = int(m.group(1))
        if 1 + rf.n_defs > len(lines):
            raise RecError(f'头部声明 {rf.n_defs} 行定义，但文件只有 {len(lines) - 1} 行')
        rf.def_lines = lines[1:1 + rf.n_defs]
        data_lines = lines[1 + rf.n_defs:]

        for i, raw in enumerate(rf.def_lines):
            dm = DEF_PAT.match(raw)
            if not dm:
                raise RecError(f'第 {i + 2} 行定义无法解析: {raw[:60]!r}')
            name = dm.group('name').decode('ascii')
            row = int(dm.group('row'))
            col = int(dm.group('col'))
            tc = int(dm.group('tc'))
            width = int(dm.group('width'))
            text_bytes = raw[dm.end():]
            # 字段行在 112 后会重复一次裸字段名，展示文本中去掉它
            if width > 0 and text_bytes.startswith(name.encode('ascii')):
                text_bytes = text_bytes[len(name):].lstrip()
            text = text_bytes.decode('gbk', errors='replace').strip()
            if width == 0:
                # 标题行：n1 = 列号（行首的是章节标题，行中的是单位/括注等碎片文字）
                rf.labels.append(Label(name=name, row=row, col=int(dm.group('n1')), text=text))
            else:
                fld = Field(name=name, kind=dm.group('pre').decode(), row=row, col=col,
                            width=width, typecode=tc, text=text, line_no=i + 2)
                if fld.name in rf.by_name:
                    raise RecError(f'字段重名: {fld.name}')
                rf.fields.append(fld)
                rf.by_name[fld.name] = fld
        rf.record_width = sum(f.width for f in rf.fields)
        if rf.record_width <= 0:
            raise RecError('没有可存储的数据字段')

        blob_parts = []
        for i, ln in enumerate(data_lines):
            if not ln.endswith(b'!'):
                raise RecError(f'数据区第 {i + 1} 行缺少 ! 终止符: {ln[:40]!r}')
            blob_parts.append(ln[:-1])
        blob = b''.join(blob_parts)
        if len(blob) % rf.record_width != 0:
            raise RecError(f'数据总字节数 {len(blob)} 不是记录宽度 {rf.record_width} 的整数倍')
        for k in range(len(blob) // rf.record_width):
            rf.records.append(rf._parse_record(blob[k * rf.record_width:(k + 1) * rf.record_width]))
        return rf

    def _parse_record(self, blob: bytes) -> dict[str, str]:
        vals: dict[str, str] = {}
        pos = 0
        for f in self.fields:
            raw = blob[pos:pos + f.width]
            pos += f.width
            # 原样读取（不做数值规范化）；规范化由用户在界面中确认后执行
            vals[f.name] = raw.decode('gbk', errors='replace').strip()
        return vals

    # ---------- 写入 ----------

    def encode_value(self, f: Field, value: str) -> bytes:
        v = '' if value is None else str(value).strip()
        try:
            b = v.encode('gbk')
        except UnicodeEncodeError as e:
            raise RecError(f'字段 {f.name} 的值 {v!r} 无法以 GBK 编码保存') from e
        while len(b) > f.width:
            v = v[:-1]          # 逐字收缩，避免切断 GBK 双字节
            b = v.encode('gbk')
        return b.rjust(f.width) if f.kind == '#' else b.ljust(f.width)

    def serialize_record(self, values: dict[str, str]) -> bytes:
        blob = b''.join(self.encode_value(f, values.get(f.name, '')) for f in self.fields)
        return blob

    def record_lines(self, values: dict[str, str]) -> list[bytes]:
        blob = self.serialize_record(values)
        return [blob[i:i + LINE_CHUNK] + b'!' for i in range(0, len(blob), LINE_CHUNK)]

    def render(self, records: list[dict[str, str]] | None = None) -> bytes:
        recs = self.records if records is None else records
        out = [self.header_line, *self.def_lines]
        for vals in recs:
            out.extend(self.record_lines(vals))
        return b'\r\n'.join(out) + b'\r\n'

    def save(self, path: str | None = None, records: list[dict[str, str]] | None = None,
             backup: bool = True) -> None:
        """整体重写 .rec。首次写某个文件前自动生成 .bak 备份（保留最早原状）。"""
        path = path or self.path
        if backup and os.path.exists(path):
            bak = path + '.bak'
            if not os.path.exists(bak):
                shutil.copy2(path, bak)
        data = self.render(records)
        tmp = path + '.tmp'
        with open(tmp, 'wb') as f:
            f.write(data)
        os.replace(tmp, path)
