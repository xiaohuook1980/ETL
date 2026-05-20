"""横向 pivot 模板配置读视图"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts._db import connect


# 横向 pivot 模板默认值（项目首次访问时使用）
DEFAULT_TEMPLATE = {
    'template_name': '月度横向',
    'scan_row_start': 1,
    'scan_row_end': 10,
    'min_consecutive_digits': 10,
    'static_column_mapping': {
        'name_raw': '姓名',
        'floor_or_group': '所在部门',
        'shift_name': '班别',
        'extra_data': '合计工时,夜班天数',
    },
    'enabled': True,
    'note': '',
}


def get_pivot_template(project_id, target_kind='attendance', format_id=None):
    """取项目的 pivot 模板配置；不存在 → 返回默认值

    format_id 给定时优先按 format_id 查；找不到再按 (project_id, target_kind) 兜底（老配置）。
    """
    project_id = int(project_id)
    conn = connect('fish-test')
    try:
        cur = conn.cursor()
        row = None
        if format_id is not None:
            cur.execute("""
                SELECT template_name, scan_row_start, scan_row_end,
                       min_consecutive_digits, static_column_mapping, enabled, note
                FROM project_pivot_templates
                WHERE project_id=%s AND target_kind=%s AND format_id=%s
            """, (project_id, target_kind, int(format_id)))
            row = cur.fetchone()
        if not row:
            cur.execute("""
                SELECT template_name, scan_row_start, scan_row_end,
                       min_consecutive_digits, static_column_mapping, enabled, note
                FROM project_pivot_templates
                WHERE project_id=%s AND target_kind=%s
                  AND format_id IS NULL
            """, (project_id, target_kind))
            row = cur.fetchone()
    finally:
        conn.close()
    if not row:
        return dict(DEFAULT_TEMPLATE, target_kind=target_kind, is_new=True)
    static_map = row[4]
    if isinstance(static_map, str):
        try: static_map = json.loads(static_map)
        except (ValueError, TypeError): static_map = {}
    return {
        'template_name': row[0],
        'scan_row_start': row[1],
        'scan_row_end': row[2],
        'min_consecutive_digits': row[3],
        'static_column_mapping': static_map or {},
        'enabled': bool(row[5]),
        'note': row[6] or '',
        'target_kind': target_kind,
        'is_new': False,
    }
