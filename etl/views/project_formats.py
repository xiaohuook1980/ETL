"""project_formats 视图层（fish-test.project_formats）"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts._db import connect


KINDS = ('attendance', 'bill', 'wage_sheet', 'payroll')


def list_formats(project_id, kind=None):
    """列项目下的 format（按 kind 分组）"""
    project_id = int(project_id)
    conn = connect('fish-test')
    try:
        cur = conn.cursor()
        params = [project_id]
        # status='active' = 启用；'disabled' = 禁用（保留配置但不参与 classify/标准化）
        # 这里返回全部（含 disabled），由 UI 展示勾选状态
        sql = """SELECT id, target_kind, name, handler, is_default, status, note,
                        created_at, updated_at, is_xiaoyu_payroll
                 FROM project_formats WHERE project_id=%s AND status<>'deleted'"""
        if kind:
            sql += ' AND target_kind=%s'
            params.append(kind)
        sql += ' ORDER BY target_kind, is_default DESC, id'
        cur.execute(sql, params)
        out = []
        for r in cur.fetchall():
            out.append({
                'id': str(r[0]),
                'target_kind': r[1],
                'name': r[2],
                'handler': r[3],
                'is_default': bool(r[4]),
                'status': r[5],
                'enabled': r[5] != 'disabled',
                'note': r[6] or '',
                'created_at': r[7].isoformat(sep=' ') if r[7] else None,
                'updated_at': r[8].isoformat(sep=' ') if r[8] else None,
                'is_xiaoyu_payroll': bool(r[9]),
            })
    finally:
        conn.close()
    return out


def get_format(format_id):
    """取单个 format 详情"""
    conn = connect('fish-test')
    try:
        cur = conn.cursor()
        cur.execute("""SELECT id, project_id, target_kind, name, handler, is_default,
                              status, note, is_xiaoyu_payroll
                       FROM project_formats WHERE id=%s""", (int(format_id),))
        row = cur.fetchone()
        if not row:
            return None
        return {
            'id': str(row[0]),
            'project_id': str(row[1]),
            'target_kind': row[2],
            'name': row[3],
            'handler': row[4],
            'is_default': bool(row[5]),
            'status': row[6],
            'enabled': row[6] != 'disabled',
            'note': row[7] or '',
            'is_xiaoyu_payroll': bool(row[8]),
        }
    finally:
        conn.close()


def list_formats_summary(project_id):
    """按 kind 分组的 format 列表 + 每个 format 各类规则计数"""
    project_id = int(project_id)
    out = {k: [] for k in KINDS}
    formats = list_formats(project_id)
    if not formats:
        return out

    fmt_ids = [int(f['id']) for f in formats]
    conn = connect('fish-test')
    try:
        cur = conn.cursor()

        def _count(tbl, group_col='format_id'):
            placeholders = ','.join(['%s'] * len(fmt_ids))
            cur.execute(f"""SELECT {group_col}, COUNT(*) FROM {tbl}
                           WHERE project_id=%s AND format_id IN ({placeholders})
                             AND enabled=1
                           GROUP BY {group_col}""", [project_id] + fmt_ids)
            return {r[0]: int(r[1]) for r in cur.fetchall()}

        cls_cnt = _count('project_classify_rules')
        ent_cnt = _count('project_enterprise_rules')
        val_cnt = _count('project_validity_rules')
        attr_cnt = _count('project_attribution_rules')
        pivot_cnt = _count('project_pivot_templates')
    finally:
        conn.close()

    for f in formats:
        fid = int(f['id'])
        f['rule_counts'] = {
            'classify':   cls_cnt.get(fid, 0),
            'enterprise': ent_cnt.get(fid, 0),
            'validity':   val_cnt.get(fid, 0),
            'attribution':attr_cnt.get(fid, 0),
            'pivot':      pivot_cnt.get(fid, 0),
        }
        out[f['target_kind']].append(f)
    return out
