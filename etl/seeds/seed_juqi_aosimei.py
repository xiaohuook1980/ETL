"""种子数据：从 fish-prod 拉东莞聚起+澳思美写入 fish-test
- enterprises (id=699 东莞聚起，实控人 刘倍)
- projects: 澳思美
- 业务周期 = 上月26-本月25 (注意：跟自然月不同)
- 单价 17 元/小时（hours_x_price 模式）
- 控制人/授信：刘倍 / 1,300,000（按 mini_actual_ctr 实时读，不在 seed 写死）
"""
import sys
import json as _json
sys.path.insert(0, 'D:/小鱼AI数据')
from scripts._db import connect

ENT_ID = 699
ENT_SHORT = '聚起'
PROJ_ID = 1995430369780154369
PROJ_PRE_ID = 1988183439777923073
PROJ_SHORT = '澳思美'
BUSINESS_CYCLE = '上月26-本月25'
PROFIT_RATIO = 0.800
UNIT_PRICE = 17.0  # 元/小时

src = connect('fish-prod')
sc = src.cursor()
dst = connect('fish-test')
dc = dst.cursor()

# enterprises
sc.execute("SELECT id, title, realname FROM biz_enterprise WHERE id=%s", (ENT_ID,))
ent_id, full_name, realname = sc.fetchone()
print(f'企业: {ent_id} {full_name} (实控人={realname}) → short={ENT_SHORT}')
dc.execute("""INSERT INTO enterprises (id, full_name, short_name, status)
              VALUES (%s, %s, %s, 'active')
              ON DUPLICATE KEY UPDATE
                full_name=VALUES(full_name), short_name=VALUES(short_name)""",
           (ent_id, full_name, ENT_SHORT))

# projects
sc.execute("SELECT project_title FROM mini_project WHERE id=%s", (PROJ_ID,))
title = sc.fetchone()[0]
print(f'项目: id={PROJ_ID} pre_id={PROJ_PRE_ID} title={title} → short={PROJ_SHORT}')
print(f'  业务周期={BUSINESS_CYCLE}  比例={PROFIT_RATIO}  单价={UNIT_PRICE}元/小时')

dc.execute("""INSERT INTO projects (id, pre_id, enterprise_id, title, short_name,
                                    finance_mode, business_cycle, profit_ratio, status)
              VALUES (%s, %s, %s, %s, %s, 'normal', %s, %s, 'active')
              ON DUPLICATE KEY UPDATE
                pre_id=VALUES(pre_id), enterprise_id=VALUES(enterprise_id),
                title=VALUES(title), short_name=VALUES(short_name),
                business_cycle=VALUES(business_cycle), profit_ratio=VALUES(profit_ratio)""",
           (PROJ_ID, PROJ_PRE_ID, ent_id, title, PROJ_SHORT, BUSINESS_CYCLE, PROFIT_RATIO))

# unit_prices（17 元/小时，全通配匹配）
dc.execute("""DELETE FROM unit_prices WHERE project_id=%s""", (PROJ_ID,))
dc.execute("""INSERT INTO unit_prices
              (project_id, area, worker_type, shift_name, price, unit, note)
              VALUES (%s, '', '', '', %s, '元/小时', '默认单价（hours_x_price 模式）')""",
           (PROJ_ID, UNIT_PRICE))

# controllers + map（实控人刘倍）
sc.execute("SELECT id, idcard, mobile FROM mini_actual_ctr WHERE user_name=%s AND mark=1 LIMIT 1",
           (realname,))
ctrl = sc.fetchone()
if ctrl:
    ctrl_id_local = ent_id  # 简化：用 enterprise_id 作 controller 表 PK 占位（不冲突就行）
    dc.execute("""INSERT INTO controllers (id, name, id_card, mobile, note)
                  VALUES (%s, %s, %s, %s, NULL)
                  ON DUPLICATE KEY UPDATE id_card=VALUES(id_card), mobile=VALUES(mobile)""",
               (ctrl[0], realname, ctrl[1], ctrl[2]))
    dc.execute("""INSERT INTO controller_enterprise_map (controller_id, enterprise_id, role)
                  VALUES (%s, %s, '实控人')
                  ON DUPLICATE KEY UPDATE role=VALUES(role)""",
               (ctrl[0], ent_id))
    print(f'实控人: {realname} (idcard={ctrl[1]})')

dst.commit()
src.close(); dst.close()

# 验证
conn = connect('fish-test')
cur = conn.cursor()
cur.execute("SELECT id, full_name, short_name FROM enterprises WHERE id=%s", (ENT_ID,))
print('\n=== enterprise:', cur.fetchone())
cur.execute("""SELECT id, enterprise_id, title, short_name, business_cycle, profit_ratio
               FROM projects WHERE id=%s""", (PROJ_ID,))
print('=== project:', cur.fetchone())
cur.execute("SELECT * FROM unit_prices WHERE project_id=%s", (PROJ_ID,))
print('=== unit_prices:', cur.fetchone())
conn.close()
print('\n✓ 种子数据写入完成')
