"""种子数据：从 fish-prod 拉希锐+丽盈普工写入 fish-test
- enterprises (id=712)
- projects (id=1986627402054696961)
- unit_prices (白班20 / 夜班21，沿用之前讨论的值)

不依赖任何本地 xlsx 文件。
"""
import sys
sys.path.insert(0, 'D:/小鱼AI数据')
from scripts._db import connect

src = connect('fish-prod')
sc = src.cursor()
dst = connect('fish-test')
dc = dst.cursor()

# ---- enterprises（希锐 712）
sc.execute("SELECT id, title, realname FROM biz_enterprise WHERE id=700")
row = sc.fetchone()
ent_id, full_name, _ = row
short_name = '希锐'
print(f'企业: {ent_id} {full_name} → short={short_name}')
dc.execute("""INSERT INTO enterprises (id, full_name, short_name, status, note)
              VALUES (%s, %s, %s, 'active', NULL)
              ON DUPLICATE KEY UPDATE full_name=VALUES(full_name), short_name=VALUES(short_name)""",
           (ent_id, full_name, short_name))

# ---- projects（丽盈普工）
sc.execute("""SELECT id, project_id, title FROM mini_pre_project
              WHERE id=1968249922528268290""")
pre = sc.fetchone()
pre_id, project_id, title = pre
print(f'项目: pre_id={pre_id} project_id={project_id} title={title}')

dc.execute("""INSERT INTO projects (id, pre_id, enterprise_id, title, short_name, jiafang_name,
                                    finance_mode, business_cycle, payroll_cycle, profit_ratio,
                                    jiafang_contract_period, baoli_contract_period, insurance_compliance,
                                    status, note)
              VALUES (%s, %s, %s, %s, %s, %s, 'normal', '自然月', NULL, 0.800, NULL, NULL, NULL, 'active', NULL)
              ON DUPLICATE KEY UPDATE
                pre_id=VALUES(pre_id), enterprise_id=VALUES(enterprise_id), title=VALUES(title)""",
           (project_id, pre_id, ent_id, title, '丽盈', '广州丽盈塑料有限公司'))

# ---- unit_prices（白班 20、夜班 21，沿用之前已确认值）
# 4 维匹配：area + worker_type + shift_name + effective_*
dc.execute("DELETE FROM unit_prices WHERE project_id=%s", (project_id,))
dc.execute("""INSERT INTO unit_prices (project_id, area, worker_type, shift_name, price, unit, effective_start, note)
              VALUES (%s, '', '', '白班', 20.00, '元/小时', '2026-04-01', '日结工 白班/早班 主流单价'),
                     (%s, '', '', '夜班', 21.00, '元/小时', '2026-04-01', '日结工 夜班 主流单价')""",
           (project_id, project_id))

dst.commit()
src.close(); dst.close()

# 验证
conn = connect('fish-test')
cur = conn.cursor()
cur.execute("SELECT id, full_name, short_name FROM enterprises")
print('\n=== enterprises ==='); [print(' ', r) for r in cur.fetchall()]
cur.execute("SELECT id, pre_id, enterprise_id, title, short_name, jiafang_name, profit_ratio FROM projects")
print('=== projects ==='); [print(' ', r) for r in cur.fetchall()]
cur.execute("SELECT id, project_id, area, worker_type, shift_name, price, unit, effective_start FROM unit_prices ORDER BY shift_name")
print('=== unit_prices ==='); [print(' ', r) for r in cur.fetchall()]
conn.close()
print('\n✓ 种子数据写入完成')
