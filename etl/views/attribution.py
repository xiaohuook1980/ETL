"""项目归属规则视图（读 fish-test.project_attribution_rules）

给项目设置页用：返回单项目的 6 种规则（3 类 × 2 种）。
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts._db import connect


CATEGORIES = ('kaoqin_bill', 'wage', 'payroll', 'payroll_bm')
SCOPES = ('enterprise', 'project')
RULE_TYPES = ('sheet', 'column')


def _empty_scope_block():
    return {'sheet': None, 'column': []}


def get_rules(project_id, format_id=None):
    """返回 dict[category][scope] = {sheet: cell_or_null, column: list_of_cells}。

    考账（kaoqin_bill）有 enterprise + project 两个 scope；
    工资/发薪（wage / payroll）只有 project scope（enterprise scope 永远为空）。

    format_id 给定 → 仅返回该 format 的规则；为 None → 返回全部
    """
    project_id = int(project_id)
    out = {c: {s: _empty_scope_block() for s in SCOPES} for c in CATEGORIES}

    conn = connect('fish-test')
    try:
        cur = conn.cursor()
        sql = """
            SELECT category, scope, rule_type, file_columns, column_names, mode,
                   keywords, enabled, match_count, last_matched_at
            FROM project_attribution_rules
            WHERE project_id=%s
        """
        args = [project_id]
        if format_id is not None:
            sql += ' AND format_id=%s'
            args.append(int(format_id))
        sql += ' ORDER BY category, scope, rule_type, file_columns, column_names, mode'
        cur.execute(sql, args)
        for cat, sc, rt, fcols, cols, mode, kws, en, mc, mt in cur.fetchall():
            if cat not in CATEGORIES or sc not in SCOPES or rt not in RULE_TYPES:
                continue
            default_mode = 'extract' if cat == 'payroll_bm' else 'include'
            cell = {
                'file_columns': fcols or '',
                'column_names': cols or '',
                'mode': mode or default_mode,
                'keywords': kws if isinstance(kws, list) else json.loads(kws or '[]'),
                'enabled': bool(en),
                'match_count': int(mc or 0),
                'last_matched_at': mt.isoformat(sep=' ') if mt else None,
            }
            if rt == 'sheet':
                # sheet 规则历史上单条；新模型可有 include+exclude 两条 → 优先用 include 显示，exclude 跟随
                out[cat][sc]['sheet'] = cell
            else:
                out[cat][sc]['column'].append(cell)

        # 项目元数据（页面顶部展示用）
        cur.execute("""
            SELECT p.id, p.title, p.short_name, e.short_name AS enterprise_short, p.enterprise_id
            FROM projects p JOIN enterprises e ON e.id = p.enterprise_id
            WHERE p.id=%s
        """, (project_id,))
        row = cur.fetchone()
        meta = None
        if row:
            meta = {
                'project_id': str(row[0]),
                'title': row[1],
                'short_name': row[2],
                'enterprise_short': row[3],
                'enterprise_id': str(row[4]),
            }
    finally:
        conn.close()

    return {'meta': meta, 'rules': out}


def get_enterprise_rules(enterprise_id):
    """返回该企业下所有已注册项目的归属规则（解析时用，跨项目扫）。"""
    enterprise_id = int(enterprise_id)
    conn = connect('fish-test')
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT p.id, p.title, r.category, r.rule_type, r.column_names, r.keywords
            FROM projects p
            LEFT JOIN project_attribution_rules r ON r.project_id = p.id
            WHERE p.enterprise_id=%s
        """, (enterprise_id,))
        rows = cur.fetchall()
    finally:
        conn.close()

    by_proj = {}
    for pid, title, cat, rt, cols, kws in rows:
        d = by_proj.setdefault(str(pid), {'project_id': str(pid), 'title': title, 'rules': []})
        if cat is None:
            continue
        d['rules'].append({
            'category': cat, 'rule_type': rt,
            'column_names': cols,
            'keywords': kws if isinstance(kws, list) else json.loads(kws or '[]'),
        })
    return list(by_proj.values())
