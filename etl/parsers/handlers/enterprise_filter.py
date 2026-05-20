"""企业归属过滤引擎（dispatcher 层 drop，跟 validity 不同）

跟 validity 区别：
- validity：行入 mart，标记 is_valid=0 + invalid_reason（保留供审计）
- enterprise_filter：行直接 drop 不入 mart（外企业数据跟本项目业务无关）

规则来源：project_enterprise_rules（按 project_id + kind + enabled + priority）
"""

# user-friendly 列名 → mart 字段名（跟 validity 共用一份语义）
USER_COL_TO_MART = {
    'attendance': {
        '姓名':'name_raw', '日期':'shift_date', '工时':'hours', '件数':'quantity',
        '班次':'shift_name', '部门':'floor_or_group', '岗位':'worker_type',
        '类型':'worker_class', '身份证':'id_card_raw',
    },
    'bill': {
        '姓名':'name_raw', '金额':'amount', '身份证':'id_card_raw',
    },
}


def _resolve_field(user_col, kind, column_mapping=None):
    """优先 USER_COL_TO_MART；其次反查 column_mapping；最后原样（落 extra_data）"""
    mart = USER_COL_TO_MART.get(kind, {}).get(user_col)
    if mart:
        return mart
    if column_mapping:
        for mart_field, col_pat in column_mapping.items():
            if mart_field == 'extra_data':
                continue
            if isinstance(col_pat, str):
                cands = [c.strip() for c in col_pat.split(',')]
                if user_col in cands:
                    return mart_field
    return user_col


def _get_value(row, field):
    if field in row:
        return row[field]
    extra = row.get('extra_data') or {}
    return extra.get(field)


def _load_rules(project_id, conn, kind, format_id=None):
    """加载企业归属规则。
    format_id 给定时：取 format_id 匹中的 + NULL 兜底（NULL = 对所有 format 生效）
    format_id 为 None：取全部（老逻辑/未启用 format 模式）
    """
    cur = conn.cursor()
    if format_id is not None:
        cur.execute("""
            SELECT id, priority, filter_column, filter_value, mode, enabled, note
            FROM project_enterprise_rules
            WHERE project_id=%s AND target_kind=%s AND enabled=1
              AND (format_id IS NULL OR format_id=%s)
            ORDER BY priority
        """, (project_id, kind, int(format_id)))
    else:
        cur.execute("""
            SELECT id, priority, filter_column, filter_value, mode, enabled, note
            FROM project_enterprise_rules
            WHERE project_id=%s AND target_kind=%s AND enabled=1
            ORDER BY priority
        """, (project_id, kind))
    return [{
        'id': r[0], 'priority': r[1],
        'filter_column': r[2], 'filter_value': r[3],
        'mode': r[4] or 'include',
        'note': r[6] or '',
    } for r in cur.fetchall()]


def _row_passes(row, rule, kind, column_mapping=None):
    """返回 True = 通过（保留），False = 不通过（drop）"""
    field = _resolve_field(rule['filter_column'], kind, column_mapping)
    val = _get_value(row, field)
    sval = '' if val is None else str(val).strip()
    kws = [k.strip() for k in str(rule['filter_value'] or '').split(',') if k.strip()]
    if not kws:
        return True  # 关键词为空 = 规则失效，保留
    hit = any(k in sval for k in kws)
    if rule['mode'] == 'include':
        return hit          # 必须命中才保留
    else:  # exclude
        return not hit      # 命中则 drop


def apply_enterprise_filter(rows, *, kind, project_id=None, conn=None,
                            column_mapping=None, format_id=None):
    """对 rows 应用企业归属过滤；返回 (kept_rows, dropped_count)。

    column_mapping: 用于 filter_column 反查（用户填文件原列名时能找到 mart 字段）。
    format_id: dispatcher 命中 rule 透传的 format，按 format_id 过滤规则。
    没规则 → 全部保留（默认放行）。
    多条规则 → 必须全部通过才保留（AND 语义）。
    """
    if not rows or not project_id or not conn:
        return rows, 0
    rules = _load_rules(project_id, conn, kind, format_id=format_id)
    if not rules:
        return rows, 0
    kept = []
    dropped = 0
    for row in rows:
        if all(_row_passes(row, r, kind, column_mapping) for r in rules):
            kept.append(row)
        else:
            dropped += 1
    return kept, dropped
