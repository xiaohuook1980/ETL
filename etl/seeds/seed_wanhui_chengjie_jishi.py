"""种子数据：从 fish-prod 拉万汇+橙界—计时写入 fish-test
- enterprises (id=491 万汇)
- projects (id=2049376503824969730 橙界—计时)
- 业务周期未知 → 默认 '自然月'
- 出款比例 0.8 默认

待对账时确认 unit_prices / business_cycle 是否需要补
"""
import sys
sys.path.insert(0, 'D:/小鱼AI数据')
from scripts._db import connect

ENT_ID = 491
PROJ_ID = 2049376503824969730
PROJ_PRE_ID = 2046800554923962369

src = connect('fish-prod')
sc = src.cursor()
dst = connect('fish-test')
dc = dst.cursor()

# enterprises
sc.execute("SELECT id, title, realname FROM biz_enterprise WHERE id=%s", (ENT_ID,))
row = sc.fetchone()
ent_id, full_name, realname = row
short_name = '万汇'
print(f'企业: {ent_id} {full_name} (实控人={realname}) → short={short_name}')
dc.execute("""INSERT INTO enterprises (id, full_name, short_name, status, note)
              VALUES (%s, %s, %s, 'active', NULL)
              ON DUPLICATE KEY UPDATE full_name=VALUES(full_name), short_name=VALUES(short_name)""",
           (ent_id, full_name, short_name))

# projects
sc.execute("SELECT project_title FROM mini_project WHERE id=%s", (PROJ_ID,))
title = sc.fetchone()[0]
print(f'项目: id={PROJ_ID} pre_id={PROJ_PRE_ID} title={title}')
dc.execute("""INSERT INTO projects (id, pre_id, enterprise_id, title, short_name, jiafang_name,
                                    finance_mode, business_cycle, payroll_cycle, profit_ratio,
                                    jiafang_contract_period, baoli_contract_period, insurance_compliance,
                                    status, note)
              VALUES (%s, %s, %s, %s, %s, NULL, 'normal', '自然月', NULL, 0.800, NULL, NULL, NULL, 'active', NULL)
              ON DUPLICATE KEY UPDATE
                pre_id=VALUES(pre_id), enterprise_id=VALUES(enterprise_id), title=VALUES(title),
                short_name=VALUES(short_name)""",
           (PROJ_ID, PROJ_PRE_ID, ent_id, title, '橙界计时'))

dst.commit()
src.close(); dst.close()

# 验证
conn = connect('fish-test')
cur = conn.cursor()
cur.execute("SELECT id, full_name, short_name FROM enterprises WHERE id=%s", (ENT_ID,))
print('\n=== enterprise:', cur.fetchone())
cur.execute("SELECT id, enterprise_id, title, short_name, business_cycle, profit_ratio FROM projects WHERE id=%s", (PROJ_ID,))
print('=== project:', cur.fetchone())
conn.close()
print('\n✓ 种子数据写入完成')
