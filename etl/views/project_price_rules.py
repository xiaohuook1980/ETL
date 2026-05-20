"""单价配置 v2 读视图：项目级 + 规则列表 + 入口页统计"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts._db import connect


def list_projects_with_price_summary():
    """入口页：列出所有已注册项目 + 单价配置摘要
    返回：list[{project_id, project_title, enterprise_short, dim1/2/3_col_name, n_rules,
                price_min, price_max, unit_main, updated_at, status}]
    """
    conn = connect('fish-test')
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT p.id, p.title, p.short_name, e.short_name AS ent_short,
                   COALESCE(c.dim1_col_name, '') AS d1,
                   COALESCE(c.dim2_col_name, '') AS d2,
                   COALESCE(c.dim3_col_name, '') AS d3,
                   c.updated_at AS cfg_updated,
                   r.n_rules, r.price_min, r.price_max, r.unit_main, r.rules_updated
            FROM projects p
            JOIN enterprises e ON p.enterprise_id = e.id
            LEFT JOIN project_price_config c ON c.project_id = p.id
            LEFT JOIN (
                SELECT project_id,
                       COUNT(*) AS n_rules,
                       MIN(price) AS price_min,
                       MAX(price) AS price_max,
                       MAX(updated_at) AS rules_updated,
                       SUBSTRING_INDEX(GROUP_CONCAT(unit ORDER BY id), ',', 1) AS unit_main
                FROM project_price_rules
                GROUP BY project_id
            ) r ON r.project_id = p.id
            ORDER BY (r.n_rules IS NULL OR r.n_rules = 0) ASC,
                     r.rules_updated DESC,
                     p.id DESC
        """)
        rows = cur.fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        n_rules = r[8] or 0
        price_min = float(r[9]) if r[9] is not None else None
        price_max = float(r[10]) if r[10] is not None else None
        unit_main = r[11] or '元/小时'
        out.append({
            'project_id': str(r[0]),
            'project_title': r[1],
            'project_short': r[2],
            'enterprise_short': r[3],
            'dim1_col_name': r[4],
            'dim2_col_name': r[5],
            'dim3_col_name': r[6],
            'n_rules': n_rules,
            'price_min': price_min,
            'price_max': price_max,
            'unit_main': unit_main,
            'updated_at': (r[12] or r[7]).isoformat() if (r[12] or r[7]) else None,
            'status': 'configured' if n_rules > 0 else 'empty',
        })
    return out


def get_project_price_config(project_id):
    """读单个项目的维度配置 + 规则列表"""
    project_id = int(project_id)
    conn = connect('fish-test')
    try:
        cur = conn.cursor()
        # 项目基本信息
        cur.execute("""SELECT p.id, p.title, p.short_name, e.short_name
                       FROM projects p JOIN enterprises e ON p.enterprise_id=e.id
                       WHERE p.id=%s""", (project_id,))
        proj = cur.fetchone()
        if not proj:
            return None

        # 维度配置
        cur.execute("""SELECT dim1_col_name, dim2_col_name, dim3_col_name, note
                       FROM project_price_config WHERE project_id=%s""", (project_id,))
        cfg = cur.fetchone()

        # 规则列表
        cur.execute("""SELECT id, dim1_keywords, dim2_keywords, dim3_keywords,
                              price, unit, effective_start, effective_end,
                              priority, note, updated_at
                       FROM project_price_rules
                       WHERE project_id=%s
                       ORDER BY priority ASC, id ASC""", (project_id,))
        rules = []
        for r in cur.fetchall():
            rules.append({
                'id': r[0],
                'dim1_keywords': r[1] or '',
                'dim2_keywords': r[2] or '',
                'dim3_keywords': r[3] or '',
                'price': float(r[4]),
                'unit': r[5],
                'effective_start': r[6].isoformat() if r[6] else None,
                'effective_end': r[7].isoformat() if r[7] else None,
                'priority': r[8],
                'note': r[9] or '',
                'updated_at': r[10].isoformat() if r[10] else None,
            })
    finally:
        conn.close()

    return {
        'project': {
            'project_id': str(proj[0]),
            'project_title': proj[1],
            'project_short': proj[2],
            'enterprise_short': proj[3],
        },
        'config': {
            'dim1_col_name': cfg[0] if cfg else '',
            'dim2_col_name': cfg[1] if cfg else '',
            'dim3_col_name': cfg[2] if cfg else '',
            'note': (cfg[3] if cfg else '') or '',
        },
        'rules': rules,
    }
