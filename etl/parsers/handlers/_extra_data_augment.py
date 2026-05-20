"""按 project_id+kind+format_id 扫描规则表，收集 enterprise/validity/attribution
/payroll_bm 引擎用到的列名，由 standard handler 自动并入 column_mapping['extra_data']。

避免"配了 filter 但 extra_data 没列上，引擎在 row dict 里取不到值，静默 drop"的隐式失败。
"""
import json
import re

_SPLIT_RE = re.compile(r'[\s,，|]+')


def _split_cols(s):
    if s is None:
        return []
    if isinstance(s, list):
        return [str(c).strip() for c in s if c is not None and str(c).strip()]
    return [c.strip() for c in _SPLIT_RE.split(str(s)) if c.strip()]


_CAT_BY_KIND = {
    'attendance': 'kaoqin_bill',
    'bill':       'kaoqin_bill',
    'wage_sheet': 'wage',
    'payroll':    'payroll',
}


def _format_clause(format_id):
    """format_id 给定:取该 format + NULL 兜底;为 None:取全部(老逻辑)"""
    if format_id is None:
        return '', ()
    return ' AND (format_id IS NULL OR format_id=%s)', (int(format_id),)


def collect_extra_columns(cur, project_id, kind, format_id=None):
    """返回该 project+kind+format 涉及的所有列名 set。"""
    cols = set()
    fc_sql, fc_params = _format_clause(format_id)

    # 1. enterprise rules
    cur.execute(
        f"""SELECT filter_column FROM project_enterprise_rules
            WHERE project_id=%s AND target_kind=%s AND enabled=1{fc_sql}""",
        (project_id, kind) + fc_params)
    for (fc,) in cur.fetchall():
        cols.update(_split_cols(fc))

    # 2. validity rules
    cur.execute(
        f"""SELECT feature_columns, filter_column FROM project_validity_rules
            WHERE project_id=%s AND target_kind=%s AND enabled=1{fc_sql}""",
        (project_id, kind) + fc_params)
    for (feat, fc) in cur.fetchall():
        if feat:
            try:
                lst = feat if isinstance(feat, list) else json.loads(feat)
                cols.update(_split_cols(lst))
            except (json.JSONDecodeError, TypeError):
                pass
        cols.update(_split_cols(fc))

    # 3. attribution rules(rule_type='column')
    cat = _CAT_BY_KIND.get(kind)
    if cat:
        cur.execute(
            f"""SELECT column_names, file_columns FROM project_attribution_rules
                WHERE project_id=%s AND category=%s AND rule_type='column'
                  AND enabled=1{fc_sql}""",
            (project_id, cat) + fc_params)
        for (cn, fc) in cur.fetchall():
            cols.update(_split_cols(cn))
            cols.update(_split_cols(fc))

    # 4. payroll biz date rules(仅 payroll)
    if kind == 'payroll':
        cur.execute(
            f"""SELECT file_columns, target_columns FROM project_payroll_biz_date_rules
                WHERE project_id=%s AND enabled=1{fc_sql}""",
            (project_id,) + fc_params)
        for (fc, tc) in cur.fetchall():
            cols.update(_split_cols(fc))
            cols.update(_split_cols(tc))

    return cols
