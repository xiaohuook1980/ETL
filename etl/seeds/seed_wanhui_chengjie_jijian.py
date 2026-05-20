"""种子数据：从 fish-prod 拉万汇+橙界——计件写入 fish-test
- enterprises (id=491 万汇 — 已 seed 过，update only)
- projects (id=2049404228145049602 橙界——计件)
- 计件单价：重量 × 0.04 元（baseline xlsx '26-4月计件单量' sheet）
- payroll_filter_keywords/kaoqin_filter_keywords 暂留 NULL，待跑通后按 baseline 真相补
"""
import sys
sys.path.insert(0, 'D:/小鱼AI数据')
from scripts._db import connect

ENT_ID = 491
PROJ_ID = 2049404228145049602

src = connect('fish-prod')
sc = src.cursor()
dst = connect('fish-test')
dc = dst.cursor()

# enterprises (已 seed 过，幂等)
sc.execute("SELECT id, title, realname FROM biz_enterprise WHERE id=%s", (ENT_ID,))
ent_id, full_name, realname = sc.fetchone()
short_name = '万汇'
print(f'企业: {ent_id} {full_name} (实控人={realname}) → short={short_name}')
dc.execute("""INSERT INTO enterprises (id, full_name, short_name, status)
              VALUES (%s, %s, %s, 'active')
              ON DUPLICATE KEY UPDATE full_name=VALUES(full_name), short_name=VALUES(short_name)""",
           (ent_id, full_name, short_name))

# projects
sc.execute("""SELECT m.id, m.project_title, p.id AS pre_id
              FROM mini_project m LEFT JOIN mini_pre_project p ON p.project_id=m.id
              WHERE m.id=%s""", (PROJ_ID,))
row = sc.fetchone()
proj_id, title, pre_id = row
print(f'项目: id={proj_id} pre_id={pre_id} title={title}')
dc.execute("""INSERT INTO projects (id, pre_id, enterprise_id, title, short_name,
                                    finance_mode, business_cycle, profit_ratio, status)
              VALUES (%s, %s, %s, %s, %s, 'normal', '自然月', 0.800, 'active')
              ON DUPLICATE KEY UPDATE
                pre_id=VALUES(pre_id), enterprise_id=VALUES(enterprise_id),
                title=VALUES(title), short_name=VALUES(short_name)""",
           (proj_id, pre_id, ent_id, title, '橙界计件'))

dst.commit()
src.close(); dst.close()

# 验证
conn = connect('fish-test')
cur = conn.cursor()
cur.execute("SELECT id, enterprise_id, title, short_name, business_cycle, profit_ratio FROM projects WHERE id=%s", (PROJ_ID,))
print('=== project:', cur.fetchone())
conn.close()
print('\n✓ 种子数据写入完成')
