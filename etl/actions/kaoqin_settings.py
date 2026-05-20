"""考勤设置写动作：单价 + 排除规则"""
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts._db import connect


_VALID_DIMENSIONS = {'worker_class', 'worker_type', 'floor_or_group', 'shift_name'}
_VALID_MODES = {'include', 'exclude'}


def save_unit_prices(project_id, prices):
    """全量替换本项目 unit_prices。

    prices: [{area, worker_type, shift_name, price, unit, effective_start, effective_end, note}]
    """
    project_id = int(project_id)
    conn = connect('fish-test')
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM unit_prices WHERE project_id=%s", (project_id,))
        n = 0
        for p in prices:
            try:
                price = float(p.get('price'))
            except (TypeError, ValueError):
                continue
            if price <= 0:
                continue
            cur.execute("""INSERT INTO unit_prices
                           (project_id, area, worker_type, shift_name, price, unit,
                            effective_start, effective_end, note)
                           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                        (project_id,
                         (p.get('area') or '').strip(),
                         (p.get('worker_type') or '').strip(),
                         (p.get('shift_name') or '').strip(),
                         price,
                         (p.get('unit') or '元/小时').strip(),
                         p.get('effective_start') or None,
                         p.get('effective_end') or None,
                         (p.get('note') or '').strip()))
            n += 1
        conn.commit()
    finally:
        conn.close()
    return {'inserted': n}


def save_filters(project_id, filters_by_dim):
    """全量替换本项目 attendance_filters。

    filters_by_dim: {worker_type: [{mode, keyword, enabled, note}], ...}
    """
    project_id = int(project_id)
    conn = connect('fish-test')
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM attendance_filters WHERE project_id=%s", (project_id,))
        n = 0
        for dim, rules in (filters_by_dim or {}).items():
            if dim not in _VALID_DIMENSIONS or not rules:
                continue
            for r in rules:
                kw = (r.get('keyword') or '').strip()
                if not kw:
                    continue
                mode = r.get('mode', 'exclude')
                if mode not in _VALID_MODES:
                    mode = 'exclude'
                cur.execute("""INSERT INTO attendance_filters
                               (project_id, dimension, mode, keyword, enabled, note)
                               VALUES (%s, %s, %s, %s, %s, %s)
                               ON DUPLICATE KEY UPDATE
                                 enabled=VALUES(enabled), note=VALUES(note)""",
                            (project_id, dim, mode, kw,
                             1 if r.get('enabled') else 0,
                             (r.get('note') or '').strip()))
                n += 1
        conn.commit()
    finally:
        conn.close()
    return {'inserted': n}


def save_daily_deduction_hours(project_id, hours):
    """更新项目级"一天固定扣除工时"。"""
    project_id = int(project_id)
    try:
        h = float(hours or 0)
    except (TypeError, ValueError):
        h = 0
    if h < 0:
        h = 0
    conn = connect('fish-test')
    try:
        cur = conn.cursor()
        cur.execute("UPDATE projects SET daily_deduction_hours=%s WHERE id=%s",
                    (h, project_id))
        conn.commit()
    finally:
        conn.close()
    return {'daily_deduction_hours': h}


def save_all(project_id, unit_prices=None, filters=None, daily_deduction_hours=None):
    out = {}
    if unit_prices is not None:
        out['unit_prices'] = save_unit_prices(project_id, unit_prices)
    if filters is not None:
        out['filters'] = save_filters(project_id, filters)
    if daily_deduction_hours is not None:
        out['daily_deduction_hours'] = save_daily_deduction_hours(project_id, daily_deduction_hours)
    return out
