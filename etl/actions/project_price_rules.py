"""单价配置 v2 写动作：维度 config + 规则 CRUD"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts._db import connect


def upsert_project_config(project_id, data):
    """配置项目维度列名"""
    project_id = int(project_id)
    if project_id <= 0:
        raise ValueError('project_id 必填')
    d1 = (data.get('dim1_col_name') or '').strip()[:64]
    d2 = (data.get('dim2_col_name') or '').strip()[:64]
    d3 = (data.get('dim3_col_name') or '').strip()[:64]
    note = (data.get('note') or '').strip()[:255]

    conn = connect('fish-test')
    try:
        cur = conn.cursor()
        cur.execute("""INSERT INTO project_price_config
                       (project_id, dim1_col_name, dim2_col_name, dim3_col_name, note)
                       VALUES (%s, %s, %s, %s, %s)
                       ON DUPLICATE KEY UPDATE
                         dim1_col_name=VALUES(dim1_col_name),
                         dim2_col_name=VALUES(dim2_col_name),
                         dim3_col_name=VALUES(dim3_col_name),
                         note=VALUES(note)""",
                    (project_id, d1, d2, d3, note))
        conn.commit()
    finally:
        conn.close()
    return {'project_id': project_id, 'dim1': d1, 'dim2': d2, 'dim3': d3}


def upsert_price_rule(data):
    """新建/更新一条单价规则"""
    rid = data.get('id')
    project_id = int(data.get('project_id') or 0)
    if project_id <= 0:
        raise ValueError('project_id 必填')
    try:
        price = float(data.get('price'))
    except (TypeError, ValueError):
        raise ValueError('price 必填且必须是数字')
    if price <= 0:
        raise ValueError('price 必须 > 0')

    d1 = (data.get('dim1_keywords') or '').strip()[:255]
    d2 = (data.get('dim2_keywords') or '').strip()[:255]
    d3 = (data.get('dim3_keywords') or '').strip()[:255]
    unit = (data.get('unit') or '元/小时').strip()[:16]
    eff_start = data.get('effective_start') or None
    eff_end = data.get('effective_end') or None
    priority = int(data.get('priority') or 100)
    note = (data.get('note') or '').strip()[:255]

    conn = connect('fish-test')
    try:
        cur = conn.cursor()
        if rid:
            cur.execute("""UPDATE project_price_rules SET
                           project_id=%s, dim1_keywords=%s, dim2_keywords=%s, dim3_keywords=%s,
                           price=%s, unit=%s, effective_start=%s, effective_end=%s,
                           priority=%s, note=%s
                           WHERE id=%s""",
                        (project_id, d1, d2, d3, price, unit, eff_start, eff_end,
                         priority, note, int(rid)))
            action = 'updated' if cur.rowcount > 0 else 'noop'
        else:
            cur.execute("""INSERT INTO project_price_rules
                           (project_id, dim1_keywords, dim2_keywords, dim3_keywords,
                            price, unit, effective_start, effective_end, priority, note)
                           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                        (project_id, d1, d2, d3, price, unit, eff_start, eff_end,
                         priority, note))
            rid = cur.lastrowid
            action = 'inserted'
        conn.commit()
    finally:
        conn.close()
    return {'id': rid, 'action': action}


def delete_price_rule(rid):
    conn = connect('fish-test')
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM project_price_rules WHERE id=%s", (int(rid),))
        n = cur.rowcount
        conn.commit()
    finally:
        conn.close()
    return {'deleted': n}
