"""字节级 round-trip 测试：解析 → 重序列化 → 与原文件逐字节比对。

测试数据目录通过环境变量 EPIDATA_TEST_DATA 指定（包含 .rec/.qes/.chk 的本地目录）。
未设置或目录不存在时自动跳过，便于在没有真实数据的机器上运行。
"""
import os
import sys

DATA_DIR = os.environ.get('EPIDATA_TEST_DATA', '')
if not DATA_DIR or not os.path.isdir(DATA_DIR):
    print('跳过：未设置 EPIDATA_TEST_DATA 或目录不存在（指向含 .rec/.qes/.chk 的目录）')
    sys.exit(0)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from epirec import RecFile  # noqa: E402

FILES = ['epidata1119.rec', '1128.rec', 'EpiData文件 1.rec']
REF_TXT = os.environ.get('EPIDATA_REF_TXT', '')   # 可选：原版 EpiData 导出的 txt（同一 .rec 的导出）

ok = True
for name in FILES:
    p = os.path.join(DATA_DIR, name)
    original = open(p, 'rb').read()
    rf = RecFile.load(p)
    rendered = rf.render()
    same = rendered == original
    ok &= same
    n = len(rf.records)
    print(f'{name}: 字段={len(rf.fields)} 标题={len(rf.labels)} 记录={n} '
          f'记录宽={rf.record_width} round-trip={"✓ 字节一致" if same else "✗ 不一致"}')
    if not same:
        # 定位第一个差异
        a, b = original, rendered
        for i in range(min(len(a), len(b))):
            if a[i] != b[i]:
                print(f'  首个差异偏移 {i}: 原={a[max(0,i-30):i+30]!r} 新={b[max(0,i-30):i+30]!r}')
                break
        else:
            print(f'  长度不同: 原={len(a)} 新={len(b)}')

# 可选交叉核对：第 1 条记录与原版 EpiData 导出 txt（EPIDATA_REF_TXT，需同一 .rec 的导出）
if REF_TXT and os.path.isfile(REF_TXT) and ok:
    rf = RecFile.load(os.path.join(DATA_DIR, FILES[0]))
    txt_lines = open(REF_TXT, 'rb').read().decode('gbk', errors='replace').replace('\r\n', '\n').split('\n')
    header = next(x for x in txt_lines if x.startswith('"z1"'))
    names = [x.strip('"') for x in header.split(';')]
    row1 = next(x for x in txt_lines[txt_lines.index(header) + 1:] if x.strip())
    # 按 EpiData 导出规则拆分：分号分隔、字符串带引号
    vals, cur, inq = [], '', False
    for ch in row1:
        if ch == '"':
            inq = not inq
        elif ch == ';' and not inq:
            vals.append(cur); cur = ''
        else:
            cur += ch
    vals.append(cur)
    rec1 = rf.records[0]
    bad = []
    for nm, tv in zip(names, vals):
        rv = rec1.get(nm, '')
        tv2 = tv.strip().strip('"').strip()
        if rv != tv2:
            bad.append((nm, rv, tv2))
    print(f'txt 交叉核对 ({len(names)} 字段): ' + ('✓ 完全一致' if not bad else f'✗ {len(bad)} 处不同'))
    for nm, rv, tv in bad[:10]:
        print(f'   {nm}: rec={rv!r} txt={tv!r}')
    ok &= not bad

print('RESULT:', 'ALL PASS' if ok else 'FAILED')
sys.exit(0 if ok else 1)
