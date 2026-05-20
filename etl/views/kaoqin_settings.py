"""考勤设置视图：单价 + 排除规则 + 当前 mart distinct 值（提示用）"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts._db import connect


DIMENSIONS = ('worker_class', 'worker_type', 'floor_or_group', 'shift_name')


def get_settings(project_id):
    project_id = int(project_id)
    conn = connect('fish-test')
    try:
        cur = conn.cursor()

        # 项目元
        cur.execute("""SELECT p.id, p.title, e.short_name, p.daily_deduction_hours FROM projects p
                       JOIN enterprises e ON e.id=p.enterprise_id WHERE p.id=%s""", (project_id,))
        meta = cur.fetchone()
        meta_d = {'project_id': str(meta[0]), 'title': meta[1],
                  'enterprise_short': meta[2]} if meta else None
        daily_deduction = float(meta[3] or 0) if meta else 0

        # 单价
        cur.execute("""SELECT id, area, worker_type, shift_name, price, unit,
                              effective_start, effective_end, note
                       FROM unit_prices WHERE project_id=%s
                       ORDER BY effective_start IS NULL, effective_start DESC, id""",
                    (project_id,))
        unit_prices = []
        for r in cur.fetchall():
            unit_prices.append({
                'id': r[0], 'area': r[1] or '', 'worker_type': r[2] or '',
                'shift_name': r[3] or '', 'price': float(r[4]) if r[4] is not None else None,
                'unit': r[5] or '元/小时',
                'effective_start': r[6].isoformat() if r[6] else None,
                'effective_end': r[7].isoformat() if r[7] else None,
                'note': r[8] or '',
            })

        # 排除/包含规则
        cur.execute("""SELECT id, dimension, mode, keyword, enabled, note,
                              match_count, last_matched_at
                       FROM attendance_filters WHERE project_id=%s
                       ORDER BY dimension, mode, keyword""", (project_id,))
        filters_by_dim = {d: [] for d in DIMENSIONS}
        for r in cur.fetchall():
            d = r[1]
            if d not in filters_by_dim:
                filters_by_dim[d] = []
            filters_by_dim[d].append({
                'id': r[0], 'mode': r[2], 'keyword': r[3],
                'enabled': bool(r[4]), 'note': r[5] or '',
                'match_count': int(r[6] or 0),
                'last_matched_at': r[7].isoformat(sep=' ') if r[7] else None,
            })

        # mart 现有 distinct 值（提示用，让用户勾选）
        cur.execute("""SELECT DISTINCT worker_type FROM attendance
                       WHERE project_id=%s AND worker_type IS NOT NULL AND worker_type<>''
                       UNION
                       SELECT DISTINCT worker_type FROM attendance_summary
                       WHERE project_id=%s AND worker_type IS NOT NULL AND worker_type<>''""",
                    (project_id, project_id))
        d_worker_type = sorted(set(r[0] for r in cur.fetchall() if r[0]))

        cur.execute("""SELECT DISTINCT floor_or_group FROM attendance
                       WHERE project_id=%s AND floor_or_group IS NOT NULL AND floor_or_group<>''
                       UNION
                       SELECT DISTINCT floor_or_group FROM attendance_summary
                       WHERE project_id=%s AND floor_or_group IS NOT NULL AND floor_or_group<>''""",
                    (project_id, project_id))
        d_floor = sorted(set(r[0] for r in cur.fetchall() if r[0]))

        cur.execute("""SELECT DISTINCT shift_name FROM attendance
                       WHERE project_id=%s AND shift_name IS NOT NULL AND shift_name<>''""",
                    (project_id,))
        d_shift = sorted(set(r[0] for r in cur.fetchall() if r[0]))

        cur.execute("""SELECT DISTINCT worker_class FROM attendance
                       WHERE project_id=%s AND worker_class IS NOT NULL AND worker_class<>''
                       UNION
                       SELECT DISTINCT worker_class FROM attendance_summary
                       WHERE project_id=%s AND worker_class IS NOT NULL AND worker_class<>''""",
                    (project_id, project_id))
        d_class = sorted(set(r[0] for r in cur.fetchall() if r[0]))

    finally:
        conn.close()

    return {
        'meta': meta_d,
        'unit_prices': unit_prices,
        'filters': filters_by_dim,
        'daily_deduction_hours': daily_deduction,
        'distinct_values': {
            'worker_class': d_class,
            'worker_type': d_worker_type,
            'floor_or_group': d_floor,
            'shift_name': d_shift,
        },
    }
