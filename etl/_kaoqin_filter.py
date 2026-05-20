"""考勤过滤 helper：从 attendance_filters 表构建 SQL WHERE 片段

用法：
    from etl._kaoqin_filter import build_attendance_where
    where, args = build_attendance_where(cur, project_id, table_alias='a')
    sql = f'... FROM attendance a WHERE a.project_id=%s {where}'
    cur.execute(sql, (project_id,) + tuple(args))

模式语义：
    - exclude：列值含任一关键词 → 排除该行
    - include：列值必须含至少一条关键词 → 否则排除
    - 同维度多条 exclude 取并集（任一命中即排除）
    - 同维度多条 include 取并集（任一命中即保留）
    - 排除优先级 > 包含

支持的 dimension：worker_type / worker_class / floor_or_group / shift_name
"""

_VALID_DIMENSIONS = ('worker_type', 'worker_class', 'floor_or_group', 'shift_name')


def build_attendance_where(cur, project_id, table_alias='a'):
    """返回 (where_fragment_starts_with_AND, args_list)。
    table_alias 用于多表 JOIN 时的列前缀（如 'a' → 'a.worker_type'）。
    无 enabled 规则时返回 ('', [])。
    """
    cur.execute("""SELECT dimension, mode, keyword
                   FROM attendance_filters
                   WHERE project_id=%s AND enabled=1""", (project_id,))
    rules = cur.fetchall()
    if not rules:
        return '', []

    by_excl = {}  # dim → [keywords]
    by_incl = {}
    for dim, mode, kw in rules:
        if dim not in _VALID_DIMENSIONS or not kw:
            continue
        target = by_excl if mode == 'exclude' else by_incl
        target.setdefault(dim, []).append(kw)

    parts = []
    args = []
    prefix = (table_alias + '.') if table_alias else ''

    # exclude：col 含任一关键词 → 排除（取反）
    for dim, kws in by_excl.items():
        col_expr = f"COALESCE({prefix}{dim}, '')"
        like_parts = []
        for kw in kws:
            like_parts.append(f'{col_expr} LIKE %s')
            args.append(f'%{kw}%')
        if like_parts:
            parts.append('NOT (' + ' OR '.join(like_parts) + ')')

    # include：col 必须含某关键词
    for dim, kws in by_incl.items():
        col_expr = f"COALESCE({prefix}{dim}, '')"
        like_parts = []
        for kw in kws:
            like_parts.append(f'{col_expr} LIKE %s')
            args.append(f'%{kw}%')
        if like_parts:
            parts.append('(' + ' OR '.join(like_parts) + ')')

    if not parts:
        return '', []
    return ' AND ' + ' AND '.join(parts), args


def apply_daily_deduction(cur, project_id, business_month, base_amount):
    """对"无账单 fallback 用 hours×price 算出的 base_amount"应用项目级天扣除规则。
    返回 (after, info_dict)。base_amount<=0 或 ded<=0 时 after=base_amount。

    扣减语义：每天总工时减 N → 每天账单减 N × 代表单价
    扣减金额 = projects.daily_deduction_hours × COUNT(DISTINCT shift_date) × 代表单价
    代表单价 = unit_prices 中 unit 不含'天' 的第一条 price（shift_name='' 优先）
    按天结算项目（unit 全部含'天'）→ 不扣减
    """
    cur.execute("SELECT daily_deduction_hours FROM projects WHERE id=%s", (project_id,))
    r = cur.fetchone()
    ded_per_day = float(r[0] or 0) if r else 0
    info = {'daily_deduction_hours': ded_per_day, 'distinct_days': 0,
            'rep_price': 0, 'deduction_amount': 0}
    if ded_per_day <= 0 or base_amount <= 0:
        return base_amount, info

    cur.execute("""SELECT price FROM unit_prices
                   WHERE project_id=%s
                     AND COALESCE(unit,'') NOT LIKE %s
                   ORDER BY (shift_name<>''), id LIMIT 1""",
                (project_id, '%天%'))
    r = cur.fetchone()
    if not r:
        return base_amount, info
    rep_price = float(r[0] or 0)
    info['rep_price'] = rep_price

    att_where, att_args = build_attendance_where(cur, project_id, table_alias='')
    sql = (f"""SELECT COUNT(DISTINCT shift_date) FROM attendance
               WHERE project_id=%s AND business_month=%s
                 AND shift_date IS NOT NULL {att_where}""")
    cur.execute(sql, [project_id, business_month] + att_args)
    distinct_days = int((cur.fetchone() or [0])[0] or 0)
    info['distinct_days'] = distinct_days

    deduction = ded_per_day * distinct_days * rep_price
    info['deduction_amount'] = round(deduction, 2)
    return max(0, base_amount - deduction), info
