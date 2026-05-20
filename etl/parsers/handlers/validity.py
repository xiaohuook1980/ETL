"""数据有效性引擎：行级过滤。

输入：handler 输出的 rows (list[dict])
输出：每个 row 打标 is_valid + invalid_reason；不删行（保留供审计 + calc 用 WHERE is_valid=1）

规则来源：
    1. project_validity_rules（按 project_id+kind+enabled+priority）
    2. 缺规则 → 内置合计行剔除（保险）

用户友好列名（如 '姓名' / '工时'）→ mart 字段名映射：
    - filter_column 是 user-friendly 名 → 通过 USER_COL_TO_MART 反查 mart 字段
    - 没找到映射 → 当 mart 字段直用（兼容用户直接配 'name_raw'）
    - mart 也没有 → 试 extra_data key
"""
import json
import re


_COMMA_RE = re.compile(r'[,，]')


# 用户配置的中文列名 → mart 字段名
USER_COL_TO_MART = {
    'attendance': {
        '姓名':'name_raw', '日期':'shift_date', '工时':'hours', '件数':'quantity',
        '班次':'shift_name', '部门':'floor_or_group', '岗位':'worker_type',
        '类型':'worker_class', '身份证':'id_card_raw',
    },
    'bill': {
        '姓名':'name_raw', '金额':'amount', '身份证':'id_card_raw',
    },
    'wage_sheet': {
        '姓名':'name_raw', '应发工资':'payable_amount',
    },
    'payroll': {
        '姓名':'name_raw', '金额':'work_amount', '付款时间':'pay_time',
        '身份证':'id_card_raw',
    },
}


def _resolve_field(user_col, kind, column_mapping=None):
    """用户列名 → mart 字段名。优先级：
    1) USER_COL_TO_MART 友好中文名（如"姓名"→name_raw）
    2) 反查 column_mapping：用户填文件原列名（如"跑单人姓名"）→ 对应 mart 字段
    3) 原样返回（落 extra_data key）
    """
    mart = USER_COL_TO_MART.get(kind, {}).get(user_col)
    if mart:
        return mart
    if column_mapping:
        for mart_field, col_pat in column_mapping.items():
            if mart_field == 'extra_data':
                continue
            if isinstance(col_pat, str):
                cands = [c.strip() for c in _COMMA_RE.split(col_pat)]
                if user_col in cands:
                    return mart_field
    return user_col


def _get_value(row, field):
    """从 row 取值；不在 row 顶层就看 extra_data。"""
    if field in row:
        return row[field]
    extra = row.get('extra_data') or {}
    return extra.get(field)


def _eval_rule(row, rule, kind, column_mapping=None):
    """单条规则判定。返回 True = 命中 = 该行无效。"""
    if not rule.get('filter_enabled', 1):
        return False
    fc = rule.get('filter_column')
    if not fc:
        return False
    mode = (rule.get('mode') or 'include').lower()

    # 双列比较：col_neq / col_eq → filter_column='列A,列B'（兼容中文逗号）
    if mode in ('col_neq', 'col_eq'):
        cols = [c.strip() for c in _COMMA_RE.split(fc) if c.strip()]
        if len(cols) != 2:
            return False
        v1 = _get_value(row, _resolve_field(cols[0], kind, column_mapping))
        v2 = _get_value(row, _resolve_field(cols[1], kind, column_mapping))
        s1 = '' if v1 is None else str(v1).strip()
        s2 = '' if v2 is None else str(v2).strip()
        if mode == 'col_neq':
            return s1 != s2
        return s1 == s2

    field = _resolve_field(fc, kind, column_mapping)
    val = _get_value(row, field)
    fv = rule.get('filter_value') or ''

    if mode in ('include', 'exclude'):
        kws = [k.strip() for k in _COMMA_RE.split(str(fv)) if k.strip()]
        if not kws:
            return False
        if val is None:
            hit = False
        else:
            sval = str(val).strip()
            hit = any(k in sval for k in kws)
        # 黑名单语义（数据有效性规则）：UI 上"包含 X"=含 X 的行命中 → is_valid=0
        # include = "含关键词则命中 → 行无效"（用户最常用：过滤路费报销/宿舍租金）
        # exclude = "不含关键词则命中 → 行无效"（白名单：必须含 X 才算工资）
        return hit if mode == 'include' else (not hit)

    # 字符串精确等于 / 不等于（首尾去空格后比较）
    # 适用：列值精确等于关键词时无效（如 worker_type='离职' 完整匹中，避免误中"离职复职"）
    if mode in ('str_eq', 'str_neq'):
        kws = [k.strip() for k in _COMMA_RE.split(str(fv)) if k.strip()]
        if not kws:
            return False
        sval = '' if val is None else str(val).strip()
        hit = any(k == sval for k in kws)
        return hit if mode == 'str_eq' else (not hit)

    # 空/非空（不依赖关键词）
    is_blank = (val is None) or (str(val).strip() == '')
    if mode == 'not_empty':  # 该列非空 → 命中（=该行无效）
        return not is_blank
    if mode == 'empty':      # 该列为空 → 命中
        return is_blank

    # 数值模式
    try:
        v = float(val) if val is not None and val != '' else None
        t = float(fv)
    except (TypeError, ValueError):
        return False
    if v is None:
        return False
    if mode == 'gt': return v > t
    if mode == 'lt': return v < t
    if mode == 'eq': return v == t
    return False


def _builtin_rules():
    """无项目规则时不施加任何默认过滤——默认所有行 is_valid=1。
    合计/小计 等汇总行在 standard.py / 各 _parse 的 NAME_BLACKLIST 已在 parse 阶段剔除，
    不需要再在 validity 引擎里重复一遍。"""
    return []


def _load_rules(project_id, conn, kind, format_id=None):
    """读取项目级 validity 规则。
    format_id 给定 → format 模式：取 NULL 兜底 + 当前 format 的规则
    format_id 为 None → 老模式：取全部"""
    cur = conn.cursor()
    if format_id is not None:
        cur.execute("""
            SELECT id, priority, feature_columns, feature_enabled,
                   filter_column, filter_enabled, mode, filter_value,
                   is_builtin, enabled, note, count_as_faxin
            FROM project_validity_rules
            WHERE project_id=%s AND target_kind=%s AND enabled=1
              AND (format_id IS NULL OR format_id=%s)
            ORDER BY priority
        """, (project_id, kind, int(format_id)))
    else:
        cur.execute("""
            SELECT id, priority, feature_columns, feature_enabled,
                   filter_column, filter_enabled, mode, filter_value,
                   is_builtin, enabled, note, count_as_faxin
            FROM project_validity_rules
            WHERE project_id=%s AND target_kind=%s AND enabled=1
            ORDER BY priority
        """, (project_id, kind))
    rules = []
    for r in cur.fetchall():
        rules.append({
            'id': r[0], 'priority': r[1],
            'feature_columns': (r[2] if isinstance(r[2], list) else (json.loads(r[2]) if r[2] else None)),
            'feature_enabled': r[3],
            'filter_column': r[4], 'filter_enabled': r[5],
            'mode': r[6], 'filter_value': r[7],
            'is_builtin': r[8], 'enabled': r[9], 'note': r[10],
            'count_as_faxin': bool(r[11]) if r[11] is not None else True,
        })
    return rules or _builtin_rules()


def apply_validity(rows, *, kind, project_id=None, conn=None, column_mapping=None,
                   format_id=None):
    """对 rows 标记 is_valid + invalid_reason；不删行。
    column_mapping: 用于 filter_column 反查（用户填文件原列名 → mart 字段）。
    format_id: dispatcher 命中 rule 透传的 format。"""
    if not rows:
        return rows
    rules = (_load_rules(project_id, conn, kind, format_id=format_id)
             if (project_id and conn) else _builtin_rules())
    for row in rows:
        row.setdefault('is_valid', 1)
        row.setdefault('invalid_reason', None)
        row.setdefault('count_as_faxin', 1)
        for rule in rules:
            if not rule.get('enabled', 1):
                continue
            if _eval_rule(row, rule, kind, column_mapping=column_mapping):
                row['is_valid'] = 0
                row['invalid_reason'] = rule.get('note') or '过滤'
                row['count_as_faxin'] = 1 if rule.get('count_as_faxin', True) else 0
                break
    return rows
