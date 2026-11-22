"""qes 绑定 + chk 规则解析的快速自检。

测试数据目录通过环境变量 EPIDATA_TEST_DATA 指定（包含 .rec/.qes/.chk 的本地目录）。
未设置或目录不存在时自动跳过，便于在没有真实数据的机器上运行。
"""
import os
import sys

DATA_DIR = os.environ.get('EPIDATA_TEST_DATA', '')
if not DATA_DIR or not os.path.isdir(DATA_DIR):
    print('跳过：未设置 EPIDATA_TEST_DATA 或目录不存在（指向含 .rec/.qes/.chk 的目录）')
    sys.exit(0)
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from epirec import RecFile  # noqa: E402
from epiqes import QesFile  # noqa: E402
from epichk import ChkFile, validate_value  # noqa: E402

DIR = DATA_DIR
rf = RecFile.load(os.path.join(DIR, 'epidata1119.rec'))
qes = QesFile.load(os.path.join(DIR, 'EpiData文件 1.qes'))
print('qes 行数:', len(qes.lines), '占位符:', len(qes.placeholders))
qes.bind(rf.fields)
print('绑定完成 ✓')
segs = qes.segments()
fmap = {p.name: p for p in qes.placeholders}
print('a04 占位符:', fmap['a04'].text, '行', fmap['a04'].line_no)
print('z8 占位符:', fmap['z8'].text, '行', fmap['z8'].line_no)
print('a01 占位符:', fmap['a01'].text, '行', fmap['a01'].line_no)
# 渲染样例：z8 所在行 + A04 行
for ln in (15, 16, 606, 607, 608):
    print(f'  行{ln}:', ' '.join((p['s'] if p['t'] == 'text' else f'[{p["n"]}]') for p in segs[ln - 1]))

chk = ChkFile.load(os.path.join(DIR, 'EpiData文件 1.chk'))
print('chk 规则数:', len(chk.rules))
r = chk.rules['a0902']
print('a0902:', r.rng, 'mustenter=', r.mustenter, 'gotos=', r.gotos)
r2 = chk.rules['a0201']
print('a0201:', r2.rng, 'jumps=', r2.jumps)
print('z1:', chk.rules['z1'].legal, 'repeat=', chk.rules['z1'].repeat)
print('z3:', chk.rules['z3'].rng, 'mustenter=', chk.rules['z3'].mustenter)
# 校验函数
print('校验: a05=5 →', validate_value(chk.rules.get('a05'), '5', '#'))
print('校验: a05=99 →', validate_value(chk.rules.get('a05'), '99', '#'))
print('校验: a05=空 →', validate_value(chk.rules.get('a05'), '', '#'))
print('校验: z1=2 →', validate_value(chk.rules.get('z1'), '2', '#'))
print('校验: a07=170.5 →', validate_value(chk.rules.get('a07'), '170.5', '#'))
print('校验: a07=abc →', validate_value(chk.rules.get('a07'), 'abc', '#'))
# repeat 字段清单
rep = [n for n, r3 in chk.rules.items() if r3.repeat]
print('REPEAT 字段:', rep)
must = [n for n, r3 in chk.rules.items() if r3.mustenter]
print('MUSTENTER 字段:', must)
goto_n = sum(len(r3.gotos) for r3 in chk.rules.values())
jump_n = sum(len(r3.jumps) for r3 in chk.rules.values())
print('条件跳转:', goto_n, 'JUMPS:', jump_n)
