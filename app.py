"""EpiData Mac 平替 — 本地网页版（通用解析 .qes/.chk/.rec：录入 / 浏览修改 / 导出）。

适用于任意 EpiData Entry (Epi Info 6 风格文本格式) 项目：
  - .rec 必需（字段结构以 .rec 定义行为准，0 条记录也可）
  - .qes 可选：同目录同名优先，其次目录内唯一文件；也可手动指定。
    缺失或绑定失败时自动退化为通用平铺录入表单
  - .chk 可选：RANGE/LEGAL/MUSTENTER/REPEAT/JUMPS/AFTER ENTRY..GOTO；
    遇到未支持的命令会收集为警告并展示，不影响打开

启动：python app.py   （Windows/macOS 均可；本机可用 启动.command 双击启动）
仅监听 127.0.0.1；标准库实现，xlsx 导出用 openpyxl。
"""
from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, quote

BASE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(BASE, 'static')
SETTINGS_FILE = os.path.join(BASE, '.last.json')

sys.path.insert(0, BASE)
from epirec import RecFile, RecError            # noqa: E402
from epiqes import QesFile                      # noqa: E402
from epichk import ChkFile, validate_value      # noqa: E402

STATE: dict = {'rec': None, 'qes': None, 'chk': None, 'path': ''}


def load_settings() -> dict:
    try:
        with open(SETTINGS_FILE, encoding='utf-8') as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return {}


# ---------- macOS 原生选择对话框（服务端调起，能拿到真实路径） ----------

def _osascript_pick(kind: str) -> dict:
    """kind: 'folder' 或 'file'。用户取消返回 cancelled=True。"""
    if kind == 'folder':
        script = 'POSIX path of (choose folder with prompt "选择 .rec 所在的文件夹")'
    else:
        script = 'POSIX path of (choose file with prompt "选择 .rec 数据文件")'
    try:
        p = subprocess.run(['osascript', '-e', script],
                           capture_output=True, text=True, timeout=600)
    except subprocess.TimeoutExpired:
        return {'dir': '', 'path': '', 'cancelled': False, 'error': '选择超时（10 分钟未操作）'}
    out = (p.stdout or '').strip().rstrip('/')
    if p.returncode == 0 and out:
        return {'dir': out, 'path': out, 'cancelled': False}
    err = (p.stderr or '')
    if '-128' in err or '用户已取消' in err or 'User canceled' in err:
        return {'dir': '', 'path': '', 'cancelled': True}
    return {'dir': '', 'path': '', 'cancelled': False,
            'error': f'无法打开选择对话框: {err.strip()[:80]}'}


def save_settings(**kw) -> None:
    s = load_settings()
    s.update(kw)
    with open(SETTINGS_FILE, 'w', encoding='utf-8') as f:
        json.dump(s, f, ensure_ascii=False)


def find_companion(rec_path: str, ext: str) -> str | None:
    """同目录找同名 .qes/.chk；找不到则取目录中唯一的一个。"""
    cand = os.path.splitext(rec_path)[0] + ext
    if os.path.exists(cand):
        return cand
    d = os.path.dirname(rec_path)
    others = sorted(os.path.join(d, f) for f in os.listdir(d) if f.lower().endswith(ext))
    return others[0] if len(others) == 1 else None


def load_project(rec_path: str, qes_path: str | None = None,
                 chk_path: str | None = None) -> dict:
    rec = RecFile.load(rec_path)
    qes_path = qes_path or find_companion(rec_path, '.qes')
    if chk_path == '':
        chk_path = None
    chk_path = chk_path or find_companion(rec_path, '.chk')

    qes = None
    warns: list[str] = []
    if qes_path:
        try:
            qes = QesFile.load(qes_path)
        except Exception as e:  # noqa: BLE001
            warns.append(f'.qes 解析失败（{e}），已退化为平铺表单')
            qes = None
    if qes is not None:
        try:
            qes.bind(rec.fields)
        except ValueError as e:
            warns.append(f'.qes 与 .rec 字段对不上，已退化为平铺表单：{e}')
            qes = None
    chk = ChkFile()
    if chk_path:
        try:
            chk = ChkFile.load(chk_path)
        except Exception as e:  # noqa: BLE001
            warns.append(f'.chk 解析失败（{e}），校验规则不可用')
    warns.extend(chk.warnings)

    STATE.update(rec=rec, qes=qes, chk=chk, path=rec_path)
    save_settings(last_dir=os.path.dirname(rec_path), last_file=rec_path)
    payload = project_payload()
    payload['warn'] = warns
    return payload


def _sec_level(text: str) -> str:
    """章节层级：单字母开头（A/B/D…）= 大标题；字母+数字（A09/B21/D1…）= 小标题；其余 = 文档标题。"""
    t = (text or '').strip()
    if re.match(r'^[A-Z]\s', t):
        return 'big'
    if re.match(r'^[A-Z][0-9]', t):
        return 'sub'
    return 'title'


def project_payload() -> dict:
    rec, qes, chk = STATE['rec'], STATE['qes'], STATE['chk']
    # 标题文本去掉花括号标记；只有位于行首的标题才算章节（行中的「分/小时」等是碎片文字）
    labels = sorted(rec.labels, key=lambda x: x.row)
    for lb in labels:
        lb.text = lb.text.replace('{', '').replace('}', '').strip()
    headings = [lb for lb in labels if lb.col <= 2]
    fields = []
    for f in rec.fields:
        sec = ''
        for lb in headings:
            if lb.row <= f.row:
                sec = lb.text
            else:
                break
        rule = chk.rules.get(f.name)
        fields.append({
            'name': f.name, 'kind': f.kind, 'width': f.width, 'tc': f.typecode,
            'text': f.text, 'row': f.row, 'section': sec,
            'decimals': f.decimals or (1 if f.typecode == 101 else 0),
            'repeat': bool(rule and rule.repeat),
            'mustenter': bool(rule and rule.mustenter),
            'rng': list(rule.rng) if rule and rule.rng else None,
            'legal': rule.legal if rule and rule.legal else [],
            'jumps': rule.jumps if rule and rule.jumps else {},
            'gotos': [list(g) for g in rule.gotos] if rule and rule.gotos else [],
        })
    order = [f['name'] for f in fields]
    return {
        'path': STATE['path'],
        'qes_path': qes.path if qes else '',
        'fields': fields,
        'order': order,
        'sections': [{'row': lb.row, 'text': lb.text, 'level': _sec_level(lb.text)}
                     for lb in headings],
        'lines': qes.segments() if qes else None,
        'records': [[rec.records[i][n] for n in order] for i in range(len(rec.records))],
        'draft': read_draft(),
    }


def validate_record(values: dict[str, str]) -> list[dict]:
    rec, chk = STATE['rec'], STATE['chk']
    errs = []
    for f in rec.fields:
        rule = chk.rules.get(f.name)
        # MUSTENTER 由录入界面当场拦截；被跳转合法跳过的必填允许留空（与 EpiData 一致），
        # 故保存时不再对「空的必填字段」报错，其余校验照常。
        if not (values.get(f.name, '') or '').strip() and rule and rule.mustenter:
            continue
        msg = validate_value(rule, values.get(f.name, ''), f.kind)
        if msg:
            errs.append({'name': f.name, 'text': f.text, 'msg': msg})
    return errs


# ---------- 录入草稿（伴生文件 <rec>.draft.json，半条记录不写入 .rec） ----------

def draft_path() -> str:
    return STATE['path'] + '.draft.json'


def read_draft() -> dict | None:
    try:
        d = json.load(open(draft_path(), encoding='utf-8'))
        if isinstance(d, dict) and isinstance(d.get('values'), dict):
            return d
    except Exception:  # noqa: BLE001
        pass
    return None


def write_draft(values: dict, cursor: str) -> None:
    rec: RecFile = STATE['rec']
    clean = {}
    for f in rec.fields:
        if f.name in values:
            clean[f.name] = rec.encode_value(f, str(values[f.name])).decode('gbk').strip()
    json.dump({'version': 1, 'cursor': cursor, 'values': clean,
               'rec_path': STATE['path'],
               'saved_at': time.strftime('%Y-%m-%d %H:%M:%S')},
              open(draft_path(), 'w', encoding='utf-8'), ensure_ascii=False)


def clear_draft() -> None:
    try:
        os.unlink(draft_path())
    except OSError:
        pass


# ---------- EpiData 式 text 导出（记录范围 / 字段选择 / 过滤器 / 选项） ----------

COND_PAT = re.compile(r'^\s*([A-Za-z_][A-Za-z0-9_]*)\s*(<=|>=|<>|!=|=|<|>)\s*("?[^"\s]+"?)\s*$')


def parse_filter(expr: str, names: set[str]) -> tuple[list, list]:
    """把 "a03=2 and z1=1" 解析为 [(var, op, val)...] 与连接词列表。"""
    parts = re.split(r'\s+(?:and|or)\s+', expr.strip(), flags=re.IGNORECASE)
    conns = re.findall(r'\s+(and|or)\s+', expr.strip(), flags=re.IGNORECASE)
    conds = []
    for p in parts:
        m = COND_PAT.match(p)
        if not m:
            raise ValueError(f'过滤器条件无法识别: {p!r}（示例: a03=2 and z1=1）')
        var, op, val = m.group(1).lower(), m.group(2), m.group(3).strip('"')
        if var not in names:
            raise ValueError(f'过滤器字段不存在: {var}')
        conds.append((var, op, val))
    return conds, conns


def apply_filter(row: dict, conds: list, conns: list) -> bool:
    results = []
    for var, op, val in conds:
        fv = (row.get(var) or '').strip()
        try:
            a, b = float(fv), float(val)
            c = (a > b) - (a < b)
        except ValueError:
            c = -1 if fv < val else (0 if fv == val else 1)
        results.append({'=': c == 0, '<>': c != 0, '!=': c != 0, '<': c < 0,
                        '>': c > 0, '<=': c <= 0, '>=': c >= 0}[op])
    if not results:
        return True
    out = results[0]
    for conn, r in zip(conns, results[1:]):
        out = (out and r) if conn.lower() == 'and' else (out or r)
    return out


def build_text_export(fields: list, rows: list[dict], sel: list[str],
                      quote: bool, sep: str, header: bool) -> str:
    tc = {f.name: f.typecode for f in fields}
    out = []
    if header:
        out.append(sep.join(('"%s"' % n) if quote else n for n in sel))
    for row in rows:
        cells = []
        for n in sel:
            v = row.get(n, '')
            cells.append('"%s"' % v if (quote and tc[n] == 1) else v)
        out.append(sep.join(cells))
    return '\r\n'.join(out) + '\r\n'


def _norm_num_checked(v: str, dec: int) -> tuple[str, str | None]:
    """数值规范化（返回 (新值, 问题)；有问题时原值保留、不破坏数据）：
    整数字段去前导零（0103120237 → 103120237、00 → 0）；
    浮点字段按定义小数位补齐（170 → 170.0）。
    问题 = 无法解析为数值 / 小数位超限 / 整数字段含小数。"""
    v = (v or '').strip()
    if not v:
        return v, None
    try:
        num = float(v)
        if not math.isfinite(num):
            return v, '无法解析为数值'
    except ValueError:
        return v, '无法解析为数值'
    frac = v.split('.')[1] if '.' in v else ''
    if len(frac) > dec:
        return v, (f'小数位超过定义的 {dec} 位' if dec else '整数字段不能含小数点')
    if dec == 0:
        if num != int(num):
            return v, '整数字段含小数'
        return str(int(num)), None
    ip = str(int(abs(num)))
    sign = '-' if num < 0 else ''
    return sign + ip + '.' + frac.ljust(dec, '0'), None


def normalize_scan(records: list[dict], fields: list) -> tuple[list, list]:
    """扫描全部记录：返回可规范化清单 changes 与问题清单 problems。"""
    changes, problems = [], []
    for i, row in enumerate(records):
        for f in fields:
            if f.kind != '#' or f.typecode == 19:
                continue
            dec = f.typecode - 100 if f.typecode >= 101 else 0
            v = (row.get(f.name) or '').strip()
            if not v:
                continue
            new, prob = _norm_num_checked(v, dec)
            if prob:
                problems.append({'index': i + 1, 'name': f.name,
                                 'text': f.text, 'value': v, 'msg': prob})
            elif new != v:
                changes.append({'index': i + 1, 'name': f.name,
                                'old': v, 'new': new})
    return changes, problems


def _xlsx_cell(v: str):
    """xlsx 数值列转换：仅做无损转换。带前导零的编号（如 0103120237）保持文本，
    与原版 EpiData 导出行为一致（原版 txt 中前导 0 同样保留）。"""
    v = (v or '').strip()
    if not v:
        return ''
    if re.fullmatch(r'-?\d+', v):
        return int(v) if str(int(v)) == v else v
    if re.fullmatch(r'-?\d+\.\d+', v):
        f = float(v)
        return f if str(f) == v else v
    return v


def export_bytes(fmt: str) -> tuple[bytes, str, str]:
    rec = STATE['rec']
    order = [f.name for f in rec.fields]
    texts = [f.text for f in rec.fields]
    rows = [[r[n] for n in order] for r in rec.records]
    stem = os.path.splitext(os.path.basename(STATE['path']))[0]
    if fmt == 'csv':
        import csv as _csv
        import io
        buf = io.StringIO()
        w = _csv.writer(buf)
        w.writerow(order)
        w.writerow(texts)
        w.writerows(rows)
        return buf.getvalue().encode('utf-8-sig'), stem + '.csv', 'text/csv'
    if fmt == 'xlsx':
        from openpyxl import Workbook
        wb = Workbook()
        ws = wb.active
        ws.title = 'data'
        ws.append(order)
        ws.append(texts)
        for row in rows:
            ws.append([_xlsx_cell(v) for v in row])
        ws.freeze_panes = 'A3'
        with tempfile.NamedTemporaryFile(suffix='.xlsx', delete=False) as t:
            wb.save(t.name)
            data = open(t.name, 'rb').read()
            os.unlink(t.name)
        return data, stem + '.xlsx', 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    if fmt == 'txt':
        # EpiData「导出数据」格式：分号分隔，字符串字段（typecode=1）加引号
        tcs = [f.typecode for f in rec.fields]
        lines = [';'.join('"%s"' % n for n in order)]
        for row in rows:
            cells = []
            for v, tc in zip(row, tcs):
                cells.append('"%s"' % v if tc == 1 else v)
            lines.append(';'.join(cells))
        return ('\r\n'.join(lines) + '\r\n').encode('gbk'), stem + '.txt', 'text/plain'
    raise ValueError(fmt)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # 安静模式
        pass

    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None):
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode('utf-8'),
                   'application/json; charset=utf-8')

    def _body(self) -> dict:
        n = int(self.headers.get('Content-Length') or 0)
        return json.loads(self.rfile.read(n).decode('utf-8')) if n else {}

    def do_GET(self):
        u = urlparse(self.path)
        if u.path in ('/', '/index.html'):
            return self._static('index.html')
        if u.path.startswith('/static/'):
            return self._static(u.path[len('/static/'):])
        if u.path == '/api/state':
            return self._json(load_settings())
        if u.path == '/api/project':
            if not STATE['rec']:
                return self._json({'error': '尚未打开数据文件'}, 400)
            return self._json(project_payload())
        if u.path == '/api/export':
            if not STATE['rec']:
                return self._json({'error': '尚未打开数据文件'}, 400)
            fmt = (parse_qs(u.query).get('fmt') or ['csv'])[0]
            try:
                data, fname, ctype = export_bytes(fmt)
            except Exception as e:  # noqa: BLE001
                return self._json({'error': str(e)}, 500)
            return self._send(200, data, ctype, {
                'Content-Disposition': f"attachment; filename*=UTF-8''{quote(fname)}"})
        return self._json({'error': 'not found'}, 404)

    def _static(self, name: str):
        path = os.path.normpath(os.path.join(STATIC, name))
        if not path.startswith(STATIC + os.sep) or not os.path.isfile(path):
            return self._json({'error': 'not found'}, 404)
        ctype = {'html': 'text/html; charset=utf-8', 'js': 'text/javascript; charset=utf-8',
                 'css': 'text/css; charset=utf-8'}.get(name.rsplit('.', 1)[-1],
                                                       'application/octet-stream')
        self._send(200, open(path, 'rb').read(), ctype)

    def do_POST(self):
        u = urlparse(self.path)
        try:
            body = self._body()
            if not isinstance(body, dict):
                body = {}
            # 写操作的项目一致性守卫：防止浏览器页面与服务端打开的文件不一致时写错文件
            if u.path in ('/api/save', '/api/draft', '/api/delete', '/api/export-text',
                          '/api/normalize') and STATE['rec']:
                want = body.get('path')
                if want and want != STATE['path']:
                    return self._json({
                        'error': f'页面与服务器打开的文件不一致（服务器当前是 {STATE["path"]}）。'
                                 '请刷新页面并重新打开你要用的 .rec 文件。',
                    }, 409)

            if u.path == '/api/pickdir':
                r = _osascript_pick('folder')
                if r['dir'] and not os.path.isdir(r['dir']):
                    r = {'dir': '', 'path': '', 'cancelled': False, 'error': '所选路径无效'}
                return self._json(r)

            if u.path == '/api/pickrec':
                r = _osascript_pick('file')
                if r['path']:
                    if not r['path'].lower().endswith('.rec'):
                        return self._json({'dir': '', 'path': '', 'cancelled': False,
                                           'error': '请选择 .rec 数据文件'}, 400)
                    r['dir'] = os.path.dirname(r['path'])
                return self._json(r)

            if u.path == '/api/scan':
                s = load_settings()
                d = body.get('dir') or s.get('last_dir') or os.path.expanduser('~')
                recs, qes_list, chk_list = [], [], []
                if os.path.isdir(d):
                    for f in sorted(os.listdir(d)):
                        p = os.path.join(d, f)
                        if f.lower().endswith('.rec'):
                            recs.append({'name': f, 'path': p, 'size': os.path.getsize(p),
                                         'mtime': time.strftime('%Y-%m-%d %H:%M',
                                                                time.localtime(os.path.getmtime(p)))})
                        elif f.lower().endswith('.qes'):
                            qes_list.append(p)
                        elif f.lower().endswith('.chk'):
                            chk_list.append(p)
                return self._json({'dir': d, 'recs': recs, 'qes_list': qes_list,
                                   'chk_list': chk_list})

            if u.path == '/api/open':
                b = body
                p = b.get('path', '')
                if not os.path.isfile(p):
                    return self._json({'error': f'文件不存在: {p}'}, 400)
                return self._json(load_project(p, b.get('qes'), b.get('chk')))

            if u.path == '/api/save':
                if not STATE['rec']:
                    return self._json({'error': '尚未打开数据文件'}, 400)
                idx, values = body.get('index'), body.get('values', {})
                rec: RecFile = STATE['rec']
                errs = validate_record(values)
                if errs:
                    return self._json({'error': '校验未通过', 'errs': errs}, 400)
                try:
                    rec.serialize_record(values)
                except RecError as e:
                    return self._json({'error': str(e)}, 400)
                records = [dict(r) for r in rec.records]
                clean = {k: str(v).strip() for k, v in values.items()}
                if idx is None:
                    records.append(clean)
                    idx = len(records) - 1
                else:
                    idx = int(idx)
                    if not (0 <= idx < len(records)):
                        return self._json({'error': f'记录号越界: {idx}'}, 400)
                    records[idx] = clean
                rec.save(records=records)
                rec.records = records
                clear_draft()          # 整份问卷已写入 .rec，草稿完成使命
                return self._json({'ok': True, 'index': idx, 'nrecords': len(records)})

            if u.path == '/api/draft':
                if not STATE['rec']:
                    return self._json({'error': '尚未打开数据文件'}, 400)
                write_draft(body.get('values', {}), body.get('cursor', ''))
                return self._json({'ok': True})

            if u.path == '/api/draft/clear':
                clear_draft()
                return self._json({'ok': True})

            if u.path == '/api/picksave':
                default_name = (body.get('name') or 'export.txt').replace('"', '')
                script = (f'POSIX path of (choose file name with prompt "导出至…" '
                          f'default name "{default_name}")')
                try:
                    p = subprocess.run(['osascript', '-e', script],
                                       capture_output=True, text=True, timeout=600)
                except subprocess.TimeoutExpired:
                    return self._json({'error': '选择超时（10 分钟未操作）'}, 400)
                out = (p.stdout or '').strip()
                if p.returncode == 0 and out:
                    if not out.lower().endswith('.txt'):
                        out += '.txt'
                    return self._json({'path': out})
                err = (p.stderr or '')
                if '-128' in err or '用户已取消' in err or 'User canceled' in err:
                    return self._json({'cancelled': True})
                return self._json({'error': f'无法打开保存对话框: {err.strip()[:80]}'}, 400)

            if u.path == '/api/export-text':
                if not STATE['rec']:
                    return self._json({'error': '尚未打开数据文件'}, 400)
                rec: RecFile = STATE['rec']
                order = [f.name for f in rec.fields]
                sel = [n for n in order if n in set(body.get('fields') or order)]
                if not sel:
                    return self._json({'error': '未选择任何字段'}, 400)
                total = len(rec.records)
                from_i = int(body.get('from') or 1)
                to_i = int(body.get('to') or total)
                from_i, to_i = max(1, from_i), min(total, to_i)
                if from_i > to_i or total == 0:
                    return self._json({'error': f'记录号范围无效（现有 {total} 条记录）'}, 400)
                rows = rec.records[from_i - 1:to_i]
                conds = conns = None
                if body.get('usefilter') and (body.get('filter') or '').strip():
                    try:
                        conds, conns = parse_filter(body['filter'], set(order))
                    except ValueError as e:
                        return self._json({'error': str(e)}, 400)
                if conds is not None:
                    rows = [r for r in rows if apply_filter(r, conds, conns)]
                fmt = body.get('format', 'txt')
                if fmt not in ('txt', 'xlsx', 'csv'):
                    return self._json({'error': f'不支持的导出格式: {fmt}'}, 400)
                encmap = {'gbk': 'gbk', 'utf8': 'utf-8', 'utf8bom': 'utf-8-sig'}
                texts = {f.name: f.text for f in rec.fields}
                tcs = {f.name: f.typecode for f in rec.fields}
                ext = {'txt': '.txt', 'xlsx': '.xlsx', 'csv': '.csv'}[fmt]
                ctype = {'txt': 'text/plain', 'xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                         'csv': 'text/csv'}[fmt]
                try:
                    if fmt == 'txt':
                        quote, header = bool(body.get('quote', True)), bool(body.get('header', True))
                        sep = {'TAB': '\t', 'SP': ' '}.get(body.get('sep'), body.get('sep', ';'))
                        if sep not in (';', ',', '\t', ' '):
                            return self._json({'error': '分割符仅支持 ; , Tab 空格'}, 400)
                        enc = body.get('encoding', 'gbk')
                        if enc not in encmap:
                            return self._json({'error': f'不支持的编码: {enc}'}, 400)
                        text = build_text_export(rec.fields, rows, sel, quote, sep, header)
                        data = text.encode(encmap[enc])
                    elif fmt == 'csv':
                        enc = body.get('encoding', 'utf8bom')
                        if enc not in encmap:
                            return self._json({'error': f'不支持的编码: {enc}'}, 400)
                        import csv as _csv
                        import io
                        buf = io.StringIO()
                        w = _csv.writer(buf)
                        w.writerow(sel)
                        w.writerow([texts.get(n, '') for n in sel])
                        for row in rows:
                            w.writerow([row.get(n, '') for n in sel])
                        data = buf.getvalue().encode(encmap[enc])
                    else:  # xlsx
                        from openpyxl import Workbook
                        wb = Workbook()
                        ws = wb.active
                        ws.title = 'data'
                        ws.append(sel)
                        ws.append([texts.get(n, '') for n in sel])
                        for row in rows:
                            ws.append([_xlsx_cell(row.get(n, '')) for n in sel])
                        ws.freeze_panes = 'A3'
                        with tempfile.NamedTemporaryFile(suffix='.xlsx', delete=False) as t:
                            wb.save(t.name)
                            data = open(t.name, 'rb').read()
                            os.unlink(t.name)
                except UnicodeEncodeError as e:
                    return self._json({'error': f'所选编码不支持某些字符（可改用 UTF-8）: {e}'}, 400)
                out_path = (body.get('out_path') or '').strip()
                if out_path:
                    try:
                        with open(out_path, 'wb') as f:
                            f.write(data)
                    except OSError as e:
                        return self._json({'error': f'无法写入 {out_path}: {e}'}, 400)
                    return self._json({'ok': True, 'path': out_path, 'format': fmt,
                                       'records': len(rows), 'fields': len(sel),
                                       'from': from_i, 'to': to_i})
                from urllib.parse import quote as _q
                fname = os.path.splitext(os.path.basename(STATE['path']))[0] + ext
                return self._send(200, data, ctype,
                                  {'Content-Disposition': f"attachment; filename*=UTF-8''{_q(fname)}"})

            if u.path == '/api/normalize':
                if not STATE['rec']:
                    return self._json({'error': '尚未打开数据文件'}, 400)
                rec: RecFile = STATE['rec']
                changes, problems = normalize_scan(rec.records, rec.fields)
                if not body.get('apply'):
                    return self._json({'ok': True, 'nchanges': len(changes),
                                       'nproblems': len(problems),
                                       'changes': changes[:200], 'problems': problems[:200]})
                # 执行：只应用可安全规范化的值；有问题（无法解析/超格式）的原值保留并报告
                for c in changes:
                    rec.records[c['index'] - 1][c['name']] = c['new']
                rec.save(records=rec.records)
                return self._json({'ok': True, 'applied': len(changes),
                                   'problems': len(problems), 'nrecords': len(rec.records)})

            if u.path == '/api/delete':
                if not STATE['rec']:
                    return self._json({'error': '尚未打开数据文件'}, 400)
                idx = int(body.get('index', -1))
                rec: RecFile = STATE['rec']
                if not (0 <= idx < len(rec.records)):
                    return self._json({'error': f'记录号越界: {idx}'}, 400)
                stamp = time.strftime('%Y%m%d-%H%M%S')
                bak = STATE['path'] + f'.deleted-{stamp}.bak'
                shutil.copy2(STATE['path'], bak)      # 删除前的完整备份
                rec.records.pop(idx)
                rec.save(records=rec.records, backup=False)
                return self._json({'ok': True, 'nrecords': len(rec.records),
                                   'backup': bak})

            return self._json({'error': 'not found'}, 404)
        except RecError as e:
            return self._json({'error': str(e)}, 400)
        except Exception as e:  # noqa: BLE001
            return self._json({'error': f'{type(e).__name__}: {e}'}, 500)


def main() -> None:
    port = 8317
    server = None
    for p in range(port, port + 20):
        try:
            server = ThreadingHTTPServer(('127.0.0.1', p), Handler)
            port = p
            break
        except OSError:
            continue
    if server is None:
        print('端口占用，启动失败')
        sys.exit(1)
    url = f'http://127.0.0.1:{port}'
    print(f'EpiData Mac 平替已启动: {url}  （Ctrl+C 退出）')
    threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\n已退出')


if __name__ == '__main__':
    main()
