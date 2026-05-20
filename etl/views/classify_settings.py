"""数据识别配置视图（读 fish-test.project_classify_rules + project_validity_rules）"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts._db import connect


KINDS = ('attendance', 'bill', 'wage_sheet', 'payroll')


def get_overview(project_id):
    """返回 4 类规则数 + 项目元数据，给概览页用。"""
    project_id = int(project_id)
    out = {k: {'classify': 0, 'validity': 0} for k in KINDS}

    conn = connect('fish-test')
    try:
        cur = conn.cursor()
        # classify 规则计数
        cur.execute("""SELECT target_kind, COUNT(*) FROM project_classify_rules
                       WHERE project_id=%s AND enabled=1
                       GROUP BY target_kind""", (project_id,))
        for k, c in cur.fetchall():
            if k in out:
                out[k]['classify'] = int(c)
        # validity 规则计数
        cur.execute("""SELECT target_kind, COUNT(*) FROM project_validity_rules
                       WHERE project_id=%s AND enabled=1
                       GROUP BY target_kind""", (project_id,))
        for k, c in cur.fetchall():
            if k in out:
                out[k]['validity'] = int(c)
        # pending 待办计数
        cur.execute("""SELECT COUNT(*) FROM pending_classify_sheets
                       WHERE project_id=%s AND status='pending'""", (project_id,))
        pending_count = int(cur.fetchone()[0])

        # 项目元数据
        cur.execute("""SELECT p.id, p.title, p.short_name,
                              e.short_name AS enterprise_short, p.enterprise_id
                       FROM projects p JOIN enterprises e ON e.id=p.enterprise_id
                       WHERE p.id=%s""", (project_id,))
        row = cur.fetchone()
        meta = None
        if row:
            meta = {
                'project_id': str(row[0]), 'title': row[1], 'short_name': row[2],
                'enterprise_short': row[3], 'enterprise_id': str(row[4]),
            }
    finally:
        conn.close()

    return {
        'meta': meta,
        'rule_counts': out,
        'pending_count': pending_count,
        'default_total_per_kind': _default_count_by_kind(),
    }


def _default_count_by_kind():
    """DEFAULT_RULES 各 kind 条数（用于 UI 显示"模板有 N 条可应用"）"""
    from etl.classify_default_rules import DEFAULT_RULES
    out = {k: 0 for k in KINDS}
    for r in DEFAULT_RULES:
        k = r.get('target_kind')
        if k in out:
            out[k] += 1
    return out


def list_classify_rules(project_id, kind, format_id=None):
    """列某 kind 的项目级 classify 规则（按 priority 升序）。
    format_id 给定 → 仅返回该 format 的规则；为 None → 返回全部
    """
    project_id = int(project_id)
    if kind not in KINDS:
        return []
    conn = connect('fish-test')
    try:
        cur = conn.cursor()
        sql = """
            SELECT id, priority, match_columns, match_columns_any, match_excludes,
                   scan_rows, handler, column_mapping, enabled,
                   match_count, last_matched_at, note, format_id
            FROM project_classify_rules
            WHERE project_id=%s AND target_kind=%s
        """
        args = [project_id, kind]
        if format_id is not None:
            sql += ' AND format_id=%s'
            args.append(int(format_id))
        sql += ' ORDER BY priority'
        cur.execute(sql, args)
        rows = []
        for r in cur.fetchall():
            def _j(v):
                if v is None: return None
                if isinstance(v, (list, dict)): return v
                return json.loads(v)
            rows.append({
                'id': r[0], 'priority': r[1],
                'match_columns': _j(r[2]) or [],
                'match_columns_any': _j(r[3]) or [],
                'match_excludes': _j(r[4]) or [],
                'scan_rows': r[5], 'handler': r[6],
                'column_mapping': _j(r[7]),
                'enabled': bool(r[8]),
                'match_count': int(r[9] or 0),
                'last_matched_at': r[10].isoformat(sep=' ') if r[10] else None,
                'note': r[11] or '',
                'format_id': str(r[12]) if r[12] else None,
            })
    finally:
        conn.close()
    return rows


def list_validity_rules(project_id, kind, format_id=None):
    """列某 kind 的数据有效性规则（按 priority 升序）。
    format_id 给定 → 仅返回该 format 的规则；为 None → 返回全部"""
    project_id = int(project_id)
    if kind not in KINDS:
        return []
    conn = connect('fish-test')
    try:
        cur = conn.cursor()
        sql = """
            SELECT id, priority, feature_columns, feature_enabled,
                   filter_column, filter_enabled, mode, filter_value,
                   is_builtin, enabled, match_count, last_matched_at, note,
                   count_as_faxin, format_id
            FROM project_validity_rules
            WHERE project_id=%s AND target_kind=%s
        """
        args = [project_id, kind]
        if format_id is not None:
            sql += ' AND format_id=%s'
            args.append(int(format_id))
        sql += ' ORDER BY priority'
        cur.execute(sql, args)
        rows = []
        for r in cur.fetchall():
            def _j(v):
                if v is None: return None
                if isinstance(v, (list, dict)): return v
                return json.loads(v)
            rows.append({
                'id': r[0], 'priority': r[1],
                'feature_columns': _j(r[2]) or [],
                'feature_enabled': bool(r[3]),
                'filter_column': r[4] or '',
                'filter_enabled': bool(r[5]),
                'mode': r[6] or 'exclude',
                'filter_value': r[7] or '',
                'is_builtin': bool(r[8]),
                'enabled': bool(r[9]),
                'match_count': int(r[10] or 0),
                'last_matched_at': r[11].isoformat(sep=' ') if r[11] else None,
                'note': r[12] or '',
                'count_as_faxin': bool(r[13]) if r[13] is not None else True,
                'format_id': str(r[14]) if r[14] else None,
            })
    finally:
        conn.close()
    return rows


def list_payroll_biz_date_rules(project_id, format_id=None):
    """列发薪业务日期判定规则（按 rule_kind 分组）。
    format_id 给定 → 仅返回该 format 的规则；为 None → 返回全部"""
    project_id = int(project_id)
    conn = connect('fish-test')
    try:
        cur = conn.cursor()
        sql = """
            SELECT id, rule_kind, priority, file_columns, target_columns,
                   offset_n, offset_unit, enabled, match_count, last_matched_at, note
            FROM project_payroll_biz_date_rules
            WHERE project_id=%s
        """
        args = [project_id]
        if format_id is not None:
            sql += ' AND format_id=%s'
            args.append(int(format_id))
        sql += ' ORDER BY rule_kind, priority'
        cur.execute(sql, args)
        out = {'extract': [], 'infer': [], 'bill_month': []}
        for r in cur.fetchall():
            row = {
                'id': r[0], 'rule_kind': r[1], 'priority': r[2],
                'file_columns': r[3] or '', 'target_columns': r[4] or '',
                'offset_n': int(r[5] or 0), 'offset_unit': r[6] or 'day',
                'enabled': bool(r[7]),
                'match_count': int(r[8] or 0),
                'last_matched_at': r[9].isoformat(sep=' ') if r[9] else None,
                'note': r[10] or '',
            }
            if r[1] in out:
                out[r[1]].append(row)
    finally:
        conn.close()
    return out


def list_enterprise_rules(project_id, kind, format_id=None):
    """列项目级企业归属规则（行级特征列过滤）"""
    project_id = int(project_id)
    conn = connect('fish-test')
    try:
        cur = conn.cursor()
        sql = """
            SELECT id, priority, filter_column, filter_value, mode,
                   enabled, match_count, last_matched_at, note, format_id
            FROM project_enterprise_rules
            WHERE project_id=%s AND target_kind=%s
        """
        args = [project_id, kind]
        if format_id is not None:
            sql += ' AND format_id=%s'
            args.append(int(format_id))
        sql += ' ORDER BY priority'
        cur.execute(sql, args)
        out = []
        for r in cur.fetchall():
            out.append({
                'id': r[0], 'priority': r[1],
                'filter_column': r[2] or '', 'filter_value': r[3] or '',
                'mode': r[4] or 'include', 'enabled': bool(r[5]),
                'match_count': int(r[6] or 0),
                'last_matched_at': r[7].isoformat(sep=' ') if r[7] else None,
                'note': r[8] or '',
                'format_id': str(r[9]) if r[9] else None,
            })
    finally:
        conn.close()
    return out


def list_pending_sheets(project_id):
    """unknown sheet 待办（dispatcher 解析时入这里）"""
    project_id = int(project_id)
    conn = connect('fish-test')
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT id, raw_file_id, sheet_name, headers_preview, status, first_seen_at
            FROM pending_classify_sheets
            WHERE project_id=%s AND status='pending'
            ORDER BY first_seen_at DESC LIMIT 100
        """, (project_id,))
        out = []
        for r in cur.fetchall():
            out.append({
                'id': r[0], 'raw_file_id': r[1], 'sheet_name': r[2],
                'headers_preview': r[3], 'status': r[4],
                'first_seen_at': r[5].isoformat(sep=' ') if r[5] else None,
            })
    finally:
        conn.close()
    return out
