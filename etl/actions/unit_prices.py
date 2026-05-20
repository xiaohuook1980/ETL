"""单价配置写动作：单条 CRUD（跨项目）"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts._db import connect


def upsert_unit_price(data):
    """新建/更新一条 unit_prices。data 含：id?/project_id/area/worker_type/shift_name/price/unit/effective_start/end/note"""
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

    area = (data.get('area') or '').strip()[:64]
    worker_type = (data.get('worker_type') or '').strip()[:64]
    shift_name = (data.get('shift_name') or '').strip()[:32]
    unit = (data.get('unit') or '元/小时').strip()[:16]
    eff_start = data.get('effective_start') or None
    eff_end = data.get('effective_end') or None
    note = (data.get('note') or '').strip()[:255]

    conn = connect('fish-test')
    try:
        cur = conn.cursor()
        if rid:
            cur.execute("""UPDATE unit_prices SET
                           project_id=%s, area=%s, worker_type=%s, shift_name=%s,
                           price=%s, unit=%s,
                           effective_start=%s, effective_end=%s, note=%s
                           WHERE id=%s""",
                        (project_id, area, worker_type, shift_name,
                         price, unit, eff_start, eff_end, note, int(rid)))
            action = 'updated' if cur.rowcount > 0 else 'noop'
        else:
            cur.execute("""INSERT INTO unit_prices
                           (project_id, area, worker_type, shift_name,
                            price, unit, effective_start, effective_end, note)
                           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                        (project_id, area, worker_type, shift_name,
                         price, unit, eff_start, eff_end, note))
            rid = cur.lastrowid
            action = 'inserted'
        conn.commit()
    finally:
        conn.close()
    return {'id': rid, 'action': action}


def delete_unit_price(rid):
    conn = connect('fish-test')
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM unit_prices WHERE id=%s", (int(rid),))
        n = cur.rowcount
        conn.commit()
    finally:
        conn.close()
    return {'deleted': n}
