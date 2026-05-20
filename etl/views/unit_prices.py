"""单价配置读视图：跨项目列所有 unit_prices 行"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts._db import connect


def list_all_unit_prices():
    """列所有项目的单价配置。
    LEFT JOIN projects 取项目名（手工新增的 project_id 不在 projects 表时显示 NULL）。
    """
    conn = connect('fish-test')
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT u.id, u.project_id, p.title AS project_title,
                   e.short_name AS enterprise_short,
                   u.area, u.worker_type, u.shift_name,
                   u.price, u.unit,
                   u.effective_start, u.effective_end, u.note,
                   u.created_at, u.updated_at
            FROM unit_prices u
            LEFT JOIN projects p ON p.id = u.project_id
            LEFT JOIN enterprises e ON e.id = p.enterprise_id
            ORDER BY u.project_id, u.id
        """)
        out = []
        for r in cur.fetchall():
            out.append({
                'id': r[0],
                'project_id': str(r[1]),
                'project_title': r[2] or f'(手工项目 {r[1]})',
                'enterprise_short': r[3] or '',
                'area': r[4] or '',
                'worker_type': r[5] or '',
                'shift_name': r[6] or '',
                'price': float(r[7]) if r[7] is not None else 0,
                'unit': r[8] or '元/小时',
                'effective_start': r[9].isoformat() if r[9] else None,
                'effective_end': r[10].isoformat() if r[10] else None,
                'note': r[11] or '',
                'created_at': r[12].isoformat(sep=' ') if r[12] else None,
                'updated_at': r[13].isoformat(sep=' ') if r[13] else None,
            })
    finally:
        conn.close()
    return out
