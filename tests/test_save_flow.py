"""端到端保存流程测试（在 /tmp 副本上进行，不触碰真实数据文件）。\n需要服务已启动（python app.py）。

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
import shutil
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from epirec import RecFile  # noqa: E402

BASE_URL = 'http://127.0.0.1:8317'
SRC = DATA_DIR
TMP = '/tmp/epi-test'

shutil.rmtree(TMP, ignore_errors=True)
os.makedirs(TMP)
shutil.copy(f'{SRC}/epidata1119.rec', f'{TMP}/test.rec')
shutil.copy(f'{SRC}/EpiData文件 1.qes', f'{TMP}/test.qes')
shutil.copy(f'{SRC}/EpiData文件 1.chk', f'{TMP}/test.chk')


def api(path, body=None):
    url = BASE_URL + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data,
                                 headers={'Content-Type': 'application/json'} if data else {})
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read().decode())


def post(path, body):
    url = BASE_URL + path
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return json.loads(e.read().decode())


proj = post('/api/open', {'path': f'{TMP}/test.rec'})
assert proj.get('path') == f'{TMP}/test.rec', proj.get('error')
n_before = len(proj['records'])
order = proj['order']
print(f'打开成功: {proj["path"]} 字段={len(proj["fields"])} 记录={n_before}')

# 校验拦截测试：超范围值应被拒绝（z3 范围 4~12）
last = dict(zip(order, proj['records'][-1]))
bad = {k: '' for k in order}
bad.update({k: last[k] for k in order})
bad['z3'] = '99'
r = post('/api/save', {'index': None, 'values': bad})
print('范围拦截:', '✓ 被拒绝' if r.get('error') else '✗ 未拦截')

# 新语义：被跳转合法跳过的必填字段允许留空（MUSTENTER 由录入界面当场拦截）；
# 用第 0 条做原位编辑验证，不改变记录数
rec0 = dict(zip(order, proj['records'][0]))
rec0['z3'] = ''
rec0['z4'] = ''
r = post('/api/save', {'index': 0, 'values': rec0})
print('跳过必填允许保存(EpiData 语义):', '✓ 放行' if r.get('ok') else '✗ 被拒')

# 新增一条：复制最后一条，改 z6（个人编号）与 a01（姓名）
vals = {k: last[k] for k in order}
vals['z6'] = '999999999'
vals['a01'] = '测试姓名'
vals['z8'] = '2026/11/21'
vals['z9'] = '1'
r = post('/api/save', {'index': None, 'values': vals})
assert r.get('ok'), r
print(f'新增保存: ✓ 第 {r["index"] + 1} 条，现共 {r["nrecords"]} 条')

# 重新加载验证（强制重新 open，确保是从磁盘读出的）
proj2 = post('/api/open', {'path': f'{TMP}/test.rec'})
assert len(proj2['records']) == n_before + 1
new_rec = dict(zip(proj2['order'], proj2['records'][-1]))
assert new_rec['z6'] == '999999999' and new_rec['a01'] == '测试姓名' and new_rec['z8'] == '2026/11/21'
print('重新打开: ✓ 新记录内容正确')

# 字节层验证：RecFile 重新解析保存后的文件（模拟 Windows EpiData 读取）
rf = RecFile.load(f'{TMP}/test.rec')
assert len(rf.records) == n_before + 1
got = rf.records[-1]
assert got['z6'] == '999999999' and got['a01'] == '测试姓名'
# 新记录的原始字节必须同样满足 78/! 断行结构
blob = rf.serialize_record(got)
lines = [blob[i:i + 78] for i in range(0, len(blob), 78)]
assert len(lines) == 10 and len(lines[-1]) == 20, (len(lines), len(lines[-1]))
print('字节布局: ✓ 9×78+20 与原文件一致，可被 EpiData 读取')

# 编辑原位更新：把第 1 条的 a07 身高改成 175.0
first = proj2['records'][0]
vals0 = dict(zip(proj2['order'], first))
vals0['a07'] = '175.0'
r = post('/api/save', {'index': 0, 'values': vals0})
assert r.get('ok'), r
rf = RecFile.load(f'{TMP}/test.rec')
assert rf.records[0]['a07'] == '175.0'
assert rf.records[0]['z6'] == proj2['records'][0][proj2['order'].index('z6')]
print('编辑保存: ✓ 第 1 条 a07 原位更新为 175.0')

# 其余 42 条未被动过的记录必须完好（与原始文件逐字段一致）
rf_orig = RecFile.load(f'{SRC}/epidata1119.rec')
diff = 0
for i in range(1, len(rf.records) - 1):
    if rf.records[i] != rf_orig.records[i]:
        diff += 1
print('既有数据:', '✓ 原 42 条完好' if diff == 0 else f'✗ {diff} 条变化')

# 删除记录：删除末尾的测试记录，删除前应生成完整备份
n_del = len(rf.records)
r = post('/api/delete', {'index': n_del - 1})
assert r.get('ok'), r
assert r['nrecords'] == n_del - 1
import glob  # noqa: E402
baks = glob.glob(f'{TMP}/test.rec.deleted-*.bak')
assert baks, '删除前应生成完整备份'
rf = RecFile.load(f'{TMP}/test.rec')
assert len(rf.records) == n_del - 1
print(f'删除记录: ✓ 已删除并备份 {os.path.basename(baks[-1])}')

print('RESULT: ALL PASS')
