"""project_aggregate_label_rules CRUD"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts._db import connect


_CELL_REF_RE = re.compile(r'^[A-Z]+\d+$')


def upsert_rule(project_id, data):
    """新建或更新规则。
    data: {id?, format_id?, sheet_pattern, label, col_name, cell_ref, priority?, enabled?, note?}
    填法二选一：label+col_name (标签定位) 或 cell_ref (坐标定位)
    """
    project_id = int(project_id)
    label = (data.get('label') or '').strip()
    col_name = (data.get('col_name') or '').strip()
    cell_ref = (data.get('cell_ref') or '').strip().upper()
    if cell_ref:
        if not _CELL_REF_RE.match(cell_ref):
            return {'error': f'cell_ref 格式非法（如 N3）: {cell_ref!r}'}
    else:
        if not label or not col_name:
            return {'error': 'label+col_name 或 cell_ref 至少填一种'}
    sheet_pattern = (data.get('sheet_pattern') or '').strip()
    priority = int(data.get('priority') or 100)
    enabled = 1 if data.get('enabled', True) else 0
    note = (data.get('note') or '').strip() or None
    fid = data.get('format_id')
    format_id = int(fid) if fid not in (None, '', 'null') else None
    rid = data.get('id')

    conn = connect('fish-test')
    try:
        cur = conn.cursor()
        if rid:
            cur.execute("""UPDATE project_aggregate_label_rules
                           SET format_id=%s, sheet_pattern=%s, label=%s, col_name=%s,
                               cell_ref=%s, priority=%s, enabled=%s, note=%s
                           WHERE id=%s AND project_id=%s""",
                        (format_id, sheet_pattern, label, col_name, cell_ref or None,
                         priority, enabled, note, int(rid), project_id))
            conn.commit()
            return {'id': str(rid), 'action': 'update'}
        cur.execute("""INSERT INTO project_aggregate_label_rules
                       (project_id, format_id, sheet_pattern, label, col_name,
                        cell_ref, priority, enabled, note)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                    (project_id, format_id, sheet_pattern, label, col_name,
                     cell_ref or None, priority, enabled, note))
        new_id = cur.lastrowid
        conn.commit()
        return {'id': str(new_id), 'action': 'insert'}
    finally:
        conn.close()


def delete_rule(project_id, rule_id):
    """删除规则"""
    conn = connect('fish-test')
    try:
        cur = conn.cursor()
        cur.execute('DELETE FROM project_aggregate_label_rules WHERE id=%s AND project_id=%s',
                    (int(rule_id), int(project_id)))
        conn.commit()
        return {'deleted': True}
    finally:
        conn.close()


def save_all_rules(project_id, format_id, rules):
    """批量保存：覆盖该 (project_id, format_id) 下的所有规则。
    rules: list[{sheet_pattern, label, col_name, priority?, enabled?, note?}]
    先删后插（事务），避免逐条 upsert 时遗留旧规则
    """
    project_id = int(project_id)
    format_id = int(format_id) if format_id not in (None, '', 'null') else None
    conn = connect('fish-test')
    try:
        cur = conn.cursor()
        if format_id is None:
            cur.execute("""DELETE FROM project_aggregate_label_rules
                           WHERE project_id=%s AND format_id IS NULL""",
                        (project_id,))
        else:
            cur.execute("""DELETE FROM project_aggregate_label_rules
                           WHERE project_id=%s AND format_id=%s""",
                        (project_id, format_id))
        n_ins = 0
        for r in rules:
            label = (r.get('label') or '').strip()
            col_name = (r.get('col_name') or '').strip()
            cell_ref = (r.get('cell_ref') or '').strip().upper()
            # 二选一：cell_ref 或 (label+col_name)
            if cell_ref:
                if not _CELL_REF_RE.match(cell_ref):
                    continue
            else:
                if not label or not col_name:
                    continue
            sheet_pattern = (r.get('sheet_pattern') or '').strip()
            priority = int(r.get('priority') or 100)
            enabled = 1 if r.get('enabled', True) else 0
            note = (r.get('note') or '').strip() or None
            cur.execute("""INSERT INTO project_aggregate_label_rules
                           (project_id, format_id, sheet_pattern, label, col_name,
                            cell_ref, priority, enabled, note)
                           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                        (project_id, format_id, sheet_pattern, label, col_name,
                         cell_ref or None, priority, enabled, note))
            n_ins += 1
        conn.commit()
        return {'inserted': n_ins, 'format_id': format_id}
    finally:
        conn.close()
