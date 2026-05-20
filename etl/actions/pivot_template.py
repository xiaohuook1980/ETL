"""横向 pivot 模板配置写动作（fish-test.project_pivot_templates）"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts._db import connect


def upsert_pivot_template(project_id, target_kind, data, format_id=None):
    """新建/更新项目的 pivot 模板（按 (project_id, target_kind, format_id) 唯一）"""
    project_id = int(project_id)
    if target_kind not in ('attendance', 'bill'):
        raise ValueError(f'invalid target_kind: {target_kind}')

    template_name = (data.get('template_name') or '月度横向')[:64]
    scan_start = int(data.get('scan_row_start', 1))
    scan_end = int(data.get('scan_row_end', 10))
    min_digits = int(data.get('min_consecutive_digits', 10))
    static_map = data.get('static_column_mapping') or {}
    enabled = 1 if data.get('enabled', True) else 0
    note = (data.get('note') or '')[:255]
    fid = int(format_id) if format_id is not None else None

    conn = connect('fish-test')
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO project_pivot_templates
                (project_id, target_kind, format_id, template_name, scan_row_start,
                 scan_row_end, min_consecutive_digits, static_column_mapping,
                 enabled, note)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                template_name=VALUES(template_name),
                scan_row_start=VALUES(scan_row_start),
                scan_row_end=VALUES(scan_row_end),
                min_consecutive_digits=VALUES(min_consecutive_digits),
                static_column_mapping=VALUES(static_column_mapping),
                enabled=VALUES(enabled),
                note=VALUES(note)
        """, (project_id, target_kind, fid, template_name, scan_start, scan_end,
              min_digits, json.dumps(static_map, ensure_ascii=False),
              enabled, note))
        conn.commit()
        return {'project_id': project_id, 'target_kind': target_kind,
                'format_id': fid, 'saved': True}
    finally:
        conn.close()


def delete_pivot_template(project_id, target_kind, format_id=None):
    project_id = int(project_id)
    conn = connect('fish-test')
    try:
        cur = conn.cursor()
        if format_id is not None:
            cur.execute("""DELETE FROM project_pivot_templates
                           WHERE project_id=%s AND target_kind=%s AND format_id=%s""",
                        (project_id, target_kind, int(format_id)))
        else:
            cur.execute("""DELETE FROM project_pivot_templates
                           WHERE project_id=%s AND target_kind=%s AND format_id IS NULL""",
                        (project_id, target_kind))
        n = cur.rowcount
        conn.commit()
    finally:
        conn.close()
    return {'deleted': n}
