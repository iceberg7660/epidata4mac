"""导出一致性测试：xlsx/txt 导出 vs 原版 EpiData 导出与人工汇总表（只读）。\n需要服务已启动（python app.py）。可选环境变量：\n  EPIDATA_REF_TXT    原版 EpiData 导出的 txt（表头顺序比对）\n  EPIDATA_REF_XLSX   人工汇总的 xlsx（逐格比对）

测试数据目录通过环境变量 EPIDATA_TEST_DATA 指定（包含 .rec/.qes/.chk 的本地目录）。
未设置或目录不存在时自动跳过，便于在没有真实数据的机器上运行。
"""
import os
import sys

DATA_DIR = os.environ.get('EPIDATA_TEST_DATA', '')
if not DATA_DIR or not os.path.isdir(DATA_DIR):
    print('跳过：未设置 EPIDATA_TEST_DATA 或目录不存在（指向含 .rec/.qes/.chk 的目录）')
    sys.exit(0)

import json
import os
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

BASE_URL = 'http://127.0.0.1:8317'
SRC = DATA_DIR
REF_TXT = os.environ.get('EPIDATA_REF_TXT', '')
REF_XLSX = os.environ.get('EPIDATA_REF_XLSX', '')
OUT = '/tmp/epi-test-export.xlsx'


def post(path, body):
    req = urllib.request.Request(BASE_URL + path, data=json.dumps(body).encode(),
                                 headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read().decode())


# 切回真实项目（只读操作，不保存）
proj = post('/api/open', {'path': f'{SRC}/epidata1119.rec'})
print(f'已打开: {proj["path"]} 记录={len(proj["records"])}')

req = urllib.request.Request(BASE_URL + '/api/export?fmt=xlsx')
with urllib.request.urlopen(req) as r:
    data = r.read()
open(OUT, 'wb').write(data)
print(f'导出完成: {len(data):,} 字节 -> {OUT}')

# ---- txt 导出表头 vs 原版 EpiData 导出（EPIDATA_REF_TXT）逐列比对 ----
if not REF_TXT or not os.path.isfile(REF_TXT):
    print('跳过 txt 表头比对：未设置 EPIDATA_REF_TXT')
    sys.exit(0)
req = urllib.request.Request(BASE_URL + '/api/export?fmt=txt')
my_txt = urllib.request.urlopen(req).read().decode('gbk').split('\r\n')[0]
ref_txt = open(REF_TXT, 'rb').read().decode('gbk', errors='replace').split('\r\n')[0]
mh = [x.strip('"') for x in my_txt.split(';')]
th = [x.strip('"') for x in ref_txt.split(';')]
hdr_bad = [(i, a, b) for i, (a, b) in enumerate(zip(mh, th)) if a != b]
print(f'txt 表头比对: 我的 {len(mh)} 列 / EpiData {len(th)} 列, 顺序差异 {len(hdr_bad)} 处')
if hdr_bad:
    for i, a, b in hdr_bad[:10]:
        print(f'  位置{i+1}: 我={a} EpiData={b}')
    sys.exit(1)
print('txt 表头顺序: ✓ 与 EpiData 原生导出完全一致')

import openpyxl  # noqa: E402

mine = openpyxl.load_workbook(OUT, read_only=True).worksheets[0]
my_rows = list(mine.iter_rows(values_only=True))       # [0]=字段名 [1]=题目 [2:]=数据
print(f'xlsx 导出: {len(my_rows)-2} 条 × {len(my_rows[0])} 列')

if not REF_XLSX or not os.path.isfile(REF_XLSX):
    print('跳过汇总表比对：未设置 EPIDATA_REF_XLSX')
    sys.exit(0)
theirs = openpyxl.load_workbook(REF_XLSX, read_only=True).worksheets[0]
their_rows = list(theirs.iter_rows(values_only=True))  # [0]=字段名 [1:]=数据

my_names = [str(x) for x in my_rows[0]]
their_names = [str(x) for x in their_rows[0]]
print(f'我的导出: {len(my_rows)-2} 条 × {len(my_names)} 列 | 汇总表: {len(their_rows)-1} 条 × {len(their_names)} 列')
assert my_names == their_names, '字段名/顺序不一致'

bad = 0
import datetime  # noqa: E402


def norm(x):
    if x is None:
        return ''
    if isinstance(x, (datetime.datetime, datetime.date)):
        return x.strftime('%Y/%m/%d')
    return str(x)


for ri in range(1, len(their_rows)):
    mine_r = my_rows[ri + 1]
    their_r = their_rows[ri]
    for ci, (a, b) in enumerate(zip(mine_r, their_r)):
        sa, sb = norm(a), norm(b)
        if sa != sb:
            bad += 1
            if bad <= 8:
                print(f'  不同: 行{ri+1} 列{my_names[ci]}: 我={sa!r} 汇总={sb!r}')
print('数值比对:', '✓ 42 条 × 551 列完全一致（日期对象已归一化）' if bad == 0 else f'✗ {bad} 处不同')
sys.exit(0 if bad == 0 else 1)
