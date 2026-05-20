"""种子数据：苏州似马韶华 + 康丽达电子 写入 fish-test
- enterprises (id=693 似马韶华，实控人 张瑞峰)
- projects: 康丽达电子 (id=1965709473330548737)
- 业务周期=自然月 (memory project_simashaohua_cycle.md)
- finance_mode 暂按 normal（baseline 含预估考勤但 normal 也算）
"""
import sys
sys.path.insert(0, 'D:/小鱼AI数据')
from scripts._db import connect

ENT_ID = 693
ENT_SHORT = '似马韶华'
PROJ_ID = 1965709473330548737
PROJ_PRE_ID = 1960957186678771714
PROJ_SHORT = '康丽达'
PROFIT_RATIO = 0.800
FINANCE_MODE = 'normal'

src = connect('fish-prod')
sc = src.cursor()
dst = connect('fish-test')
dc = dst.cursor()

sc.execute("SELECT id, title, realname FROM biz_enterprise WHERE id=%s", (ENT_ID,))
ent_id, full_name, realname = sc.fetchone()
print(f'企业: {ent_id} {full_name} (实控人={realname}) → short={ENT_SHORT}')
dc.execute("""INSERT INTO enterprises (id, full_name, short_name, status)
              VALUES (%s, %s, %s, 'active')
              ON DUPLICATE KEY UPDATE full_name=VALUES(full_name), short_name=VALUES(short_name)""",
           (ent_id, full_name, ENT_SHORT))

sc.execute("SELECT m.project_title FROM mini_project m WHERE m.id=%s", (PROJ_ID,))
title = sc.fetchone()[0]
print(f'项目: id={PROJ_ID} pre_id={PROJ_PRE_ID} title={title} → short={PROJ_SHORT}')

dc.execute("""INSERT INTO projects (id, pre_id, enterprise_id, title, short_name,
                                    finance_mode, business_cycle, profit_ratio, status)
              VALUES (%s, %s, %s, %s, %s, %s, '自然月', %s, 'active')
              ON DUPLICATE KEY UPDATE
                pre_id=VALUES(pre_id), enterprise_id=VALUES(enterprise_id),
                title=VALUES(title), short_name=VALUES(short_name),
                finance_mode=VALUES(finance_mode), profit_ratio=VALUES(profit_ratio)""",
           (PROJ_ID, PROJ_PRE_ID, ent_id, title, PROJ_SHORT, FINANCE_MODE, PROFIT_RATIO))

dc.execute("DELETE FROM business_cycles WHERE project_id=%s", (PROJ_ID,))
dc.execute("""INSERT INTO business_cycles (project_id, cycle_type, start_day, note)
              VALUES (%s, '自然月', 1, '康丽达自然月（memory simashaohua_cycle）')""", (PROJ_ID,))

sc.execute("SELECT id, idcard, mobile FROM mini_actual_ctr WHERE user_name=%s AND mark=1 LIMIT 1", (realname,))
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
    print(f'实控人: {realname}')

dst.commit()
src.close(); dst.close()
print('✓ 种子数据完成')
