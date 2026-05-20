"""project_formats CRUD（fish-test.project_formats）"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts._db import connect


KINDS = ('attendance', 'bill', 'wage_sheet', 'payroll')


def upsert_format(project_id, data):
    """新建或更新 format。
    data: {id?, target_kind, name, handler?, is_default?, note?}
    返回 {id}
    """
    project_id = int(project_id)
    kind = data.get('target_kind')
    if kind not in KINDS:
        return {'error': f'invalid target_kind: {kind}'}
    name = (data.get('name') or '').strip()
    if not name:
        return {'error': 'name required'}
    handler = (data.get('handler') or 'standard').strip()
    is_default = 1 if data.get('is_default') else 0
    note = data.get('note') or None
    is_xiaoyu_payroll = 1 if data.get('is_xiaoyu_payroll') else 0
    if is_xiaoyu_payroll and kind != 'payroll':
        is_xiaoyu_payroll = 0  # 仅 payroll kind 允许
    fid = data.get('id')

    conn = connect('fish-test')
    try:
        cur = conn.cursor()
        if fid:
            cur.execute("""UPDATE project_formats
                           SET name=%s, handler=%s, is_default=%s, note=%s,
                               is_xiaoyu_payroll=%s
                           WHERE id=%s AND project_id=%s""",
                        (name, handler, is_default, note, is_xiaoyu_payroll,
                         int(fid), project_id))
            conn.commit()
            return {'id': str(fid), 'action': 'update'}
        # 新建：is_default 全局唯一（每 kind 内）
        if is_default:
            cur.execute("""UPDATE project_formats SET is_default=0
                           WHERE project_id=%s AND target_kind=%s""",
                        (project_id, kind))
        cur.execute("""INSERT INTO project_formats
                       (project_id, target_kind, name, handler, is_default, note,
                        is_xiaoyu_payroll)
                       VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                    (project_id, kind, name, handler, is_default, note,
                     is_xiaoyu_payroll))
        new_id = cur.lastrowid
        conn.commit()
        return {'id': str(new_id), 'action': 'insert'}
    finally:
        conn.close()


def toggle_format(project_id, format_id, enabled):
    """启用/禁用 format（仅切 status，不删任何规则）"""
    project_id = int(project_id)
    fid = int(format_id)
    new_status = 'active' if enabled else 'disabled'
    conn = connect('fish-test')
    try:
        cur = conn.cursor()
        cur.execute("""UPDATE project_formats SET status=%s
                       WHERE id=%s AND project_id=%s""",
                    (new_status, fid, project_id))
        conn.commit()
        return {'id': str(fid), 'enabled': bool(enabled), 'status': new_status}
    finally:
        conn.close()


def delete_format(project_id, format_id):
    """删 format 及挂在其下的所有规则"""
    project_id = int(project_id)
    fid = int(format_id)
    conn = connect('fish-test')
    try:
        cur = conn.cursor()
        # 1. 删本 format 关联的规则
        for tbl in ('project_classify_rules', 'project_enterprise_rules',
                    'project_validity_rules', 'project_attribution_rules',
                    'project_pivot_templates', 'project_payroll_biz_date_rules',
                    'project_aggregate_label_rules'):
            cur.execute(f'DELETE FROM {tbl} WHERE project_id=%s AND format_id=%s',
                        (project_id, fid))
        # 2. 删 format 本身
        cur.execute('DELETE FROM project_formats WHERE id=%s AND project_id=%s',
                    (fid, project_id))
        conn.commit()
        return {'deleted': True}
    finally:
        conn.close()
