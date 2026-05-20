"""project_aggregate_label_rules 视图层"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts._db import connect


def list_rules(project_id, format_id=None):
    """列项目下的聚合标签规则（UI 列表用，严格匹配 format_id）。

    format_id：
        None → 仅返回 format_id IS NULL 的"全局/老模式"规则
        给定 → 仅返回 format_id=该值 的规则（不包含 NULL 兜底）

    handler 运行时另走 load_active_rules，它会同时加载 NULL 兜底 + 本 format。
    """
    project_id = int(project_id)
    conn = connect('fish-test')
    try:
        cur = conn.cursor()
        if format_id is None:
            cur.execute("""SELECT id, format_id, sheet_pattern, label, col_name,
                                  cell_ref, priority, enabled, note
                           FROM project_aggregate_label_rules
                           WHERE project_id=%s AND format_id IS NULL
                           ORDER BY priority, id""",
                        (project_id,))
        else:
            cur.execute("""SELECT id, format_id, sheet_pattern, label, col_name,
                                  cell_ref, priority, enabled, note
                           FROM project_aggregate_label_rules
                           WHERE project_id=%s AND format_id=%s
                           ORDER BY priority, id""",
                        (project_id, int(format_id)))
        out = []
        for r in cur.fetchall():
            out.append({
                'id': str(r[0]),
                'format_id': str(r[1]) if r[1] else None,
                'sheet_pattern': r[2] or '',
                'label': r[3] or '',
                'col_name': r[4] or '',
                'cell_ref': r[5] or '',
                'priority': int(r[6] or 100),
                'enabled': bool(r[7]),
                'note': r[8] or '',
            })
    finally:
        conn.close()
    return out


def load_active_rules(cur, project_id, format_id=None):
    """供 handler 使用：取 enabled=1 的规则列表（已绑定 cursor，不开新连接）"""
    if format_id is None:
        cur.execute("""SELECT id, sheet_pattern, label, col_name, cell_ref, priority
                       FROM project_aggregate_label_rules
                       WHERE project_id=%s AND enabled=1
                       ORDER BY (format_id IS NULL), priority, id""",
                    (int(project_id),))
    else:
        cur.execute("""SELECT id, sheet_pattern, label, col_name, cell_ref, priority
                       FROM project_aggregate_label_rules
                       WHERE project_id=%s AND enabled=1
                         AND (format_id IS NULL OR format_id=%s)
                       ORDER BY (format_id IS NULL), priority, id""",
                    (int(project_id), int(format_id)))
    out = []
    for r in cur.fetchall():
        out.append({
            'id': r[0],
            'sheet_pattern': r[1] or '',
            'label': r[2] or '',
            'col_name': r[3] or '',
            'cell_ref': r[4] or '',
            'priority': int(r[5] or 100),
        })
    return out
