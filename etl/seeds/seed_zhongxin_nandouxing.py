"""种子数据：从 fish-prod 拉众鑫+南斗星写入 fish-test
- enterprises (id=588 众鑫)
- projects (id=1879848562958331906 南斗星)
- unit_prices: 南斗星 #1 模式=sum_amount_col（账单走 xlsx 金额列直接求和），不需要单价
  暂时空 seed，后续若需要再补
"""
import sys
sys.path.insert(0, 'D:/小鱼AI数据')
from scripts._db import connect

ENT_ID = 588
PROJ_ID = 1879848562958331906
PROJ_PRE_ID = 1874766270603419649

src = connect('fish-prod')
sc = src.cursor()
dst = connect('fish-test')
dc = dst.cursor()

# enterprises
sc.execute("SELECT id, title, realname FROM biz_enterprise WHERE id=%s", (ENT_ID,))
row = sc.fetchone()
ent_id, full_name, realname = row
short_name = '众鑫'
print(f'企业: {ent_id} {full_name} (实控人={realname}) → short={short_name}')
dc.execute("""INSERT INTO enterprises (id, full_name, short_name, status, note)
              VALUES (%s, %s, %s, 'active', NULL)
              ON DUPLICATE KEY UPDATE full_name=VALUES(full_name), short_name=VALUES(short_name)""",
           (ent_id, full_name, short_name))

# projects（南斗星）—— business_cycle='自然月', profit_ratio=0.8
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
           (PROJ_ID, PROJ_PRE_ID, ent_id, title, '南斗星'))

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
