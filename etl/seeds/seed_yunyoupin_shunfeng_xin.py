"""种子数据：陕西云优聘 + 顺丰项目【新】写入 fish-test
- enterprises (id=440 陕西云优聘，实控人 周晓博)
- projects: 顺丰项目【新】(id=1894979691389972481)
- finance_mode='prepay'  业务周期=自然月
- 单价：暂无（顺丰项目按出勤 xlsx 装数据）
"""
import sys
import json as _json
sys.path.insert(0, 'D:/小鱼AI数据')
from scripts._db import connect

ENT_ID = 440
ENT_SHORT = '云优聘'
PROJ_ID = 1894979691389972481
PROJ_SHORT = '顺丰项目新'
BUSINESS_CYCLE_TYPE = '自然月'
BUSINESS_CYCLE_START_DAY = 1
PROFIT_RATIO = 0.800
FINANCE_MODE = 'prepay'  # 预付模式

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
sc.execute("SELECT m.project_title, p.id AS pre_id FROM mini_project m LEFT JOIN mini_pre_project p ON p.project_id=m.id WHERE m.id=%s", (PROJ_ID,))
title, pre_id = sc.fetchone()
print(f'项目: id={PROJ_ID} pre_id={pre_id} title={title} → short={PROJ_SHORT}')
print(f'  finance_mode={FINANCE_MODE}  业务周期=自然月  比例={PROFIT_RATIO}')

dc.execute("""INSERT INTO projects (id, pre_id, enterprise_id, title, short_name,
                                    finance_mode, business_cycle, profit_ratio, status)
              VALUES (%s, %s, %s, %s, %s, %s, '自然月', %s, 'active')
              ON DUPLICATE KEY UPDATE
                pre_id=VALUES(pre_id), enterprise_id=VALUES(enterprise_id),
                title=VALUES(title), short_name=VALUES(short_name),
                finance_mode=VALUES(finance_mode), profit_ratio=VALUES(profit_ratio)""",
           (PROJ_ID, pre_id, ent_id, title, PROJ_SHORT, FINANCE_MODE, PROFIT_RATIO))

# business_cycles
dc.execute("""DELETE FROM business_cycles WHERE project_id=%s""", (PROJ_ID,))
dc.execute("""INSERT INTO business_cycles (project_id, cycle_type, start_day, note)
              VALUES (%s, %s, %s, '自然月')""",
           (PROJ_ID, BUSINESS_CYCLE_TYPE, BUSINESS_CYCLE_START_DAY))

# controllers
sc.execute("SELECT id, idcard, mobile FROM mini_actual_ctr WHERE user_name=%s AND mark=1 LIMIT 1",
           (realname,))
ctrl = sc.fetchone()
if ctrl:
    dc.execute("""INSERT INTO controllers (id, name, id_card, mobile)
                  VALUES (%s, %s, %s, %s)
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
cur.execute("""SELECT id, enterprise_id, title, short_name, finance_mode, business_cycle, profit_ratio
               FROM projects WHERE id=%s""", (PROJ_ID,))
print('=== project:', cur.fetchone())
conn.close()
print('\n✓ 种子数据写入完成')
