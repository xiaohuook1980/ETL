"""种子数据：从 fish-prod 拉梦寺达 3 酒店项目写入 fish-test
- enterprises (id=716 梦寺达)
- projects: 飞船酒店 / 横琴湾酒店 / 企鹅酒店
- 业务月 2026-03 / 申请日 2026-04-28
- 实控人 陈森伟 / 出款比例 0.8 / 业务周期 自然月
- 不接小鱼系统（流水=0），所有数据走 xlsx 路径
"""
import sys
sys.path.insert(0, 'D:/小鱼AI数据')
from scripts._db import connect

ENT_ID = 716
ENT_SHORT = '梦寺达'
# (project_id, short_name, sheet_route_keywords)
# sheet_route_keywords 用于多 sheet 一文件混合多项目时按 sheet name 路由（如长隆融资.xlsx）
PROJECTS = [
    (1993150016369655809, '飞船酒店', ['飞船']),
    (2003405066444791810, '横琴湾酒店', ['横琴']),
    (2003405324088303617, '企鹅酒店', ['企鹅']),
]

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
              ON DUPLICATE KEY UPDATE full_name=VALUES(full_name), short_name=VALUES(short_name)""",
           (ent_id, full_name, ENT_SHORT))

# projects
import json as _json
for pid, short, route_kws in PROJECTS:
    sc.execute("SELECT m.id, m.project_title, p.id AS pre_id FROM mini_project m LEFT JOIN mini_pre_project p ON p.project_id=m.id WHERE m.id=%s", (pid,))
    row = sc.fetchone()
    proj_id, title, pre_id = row
    route_j = _json.dumps(route_kws, ensure_ascii=False)
    print(f'  项目: id={proj_id} pre_id={pre_id} title={title} → short={short} route={route_j}')
    dc.execute("""INSERT INTO projects (id, pre_id, enterprise_id, title, short_name,
                                        finance_mode, business_cycle, profit_ratio, status,
                                        sheet_route_keywords)
                  VALUES (%s, %s, %s, %s, %s, 'normal', '自然月', 0.800, 'active', %s)
                  ON DUPLICATE KEY UPDATE
                    pre_id=VALUES(pre_id), enterprise_id=VALUES(enterprise_id),
                    title=VALUES(title), short_name=VALUES(short_name),
                    sheet_route_keywords=VALUES(sheet_route_keywords)""",
               (proj_id, pre_id, ent_id, title, short, route_j))

dst.commit()
src.close(); dst.close()

# 验证
conn = connect('fish-test')
cur = conn.cursor()
cur.execute("SELECT id, enterprise_id, title, short_name FROM projects WHERE enterprise_id=%s ORDER BY id", (ENT_ID,))
print('\n=== 验证 ===')
for r in cur.fetchall():
    print(f'  {r}')
conn.close()
print('\n✓ 种子数据写入完成')
