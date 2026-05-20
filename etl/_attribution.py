"""归属规则 helper（解析时按本项目规则过滤本项目数据）

模型：
  - fish-prod 系统按项目放文件，跨项目情况由员工各项目各上传一份
  - 解析当前文件挂载的项目 P → 按 P 的规则过滤出"属于 P 的数据"
  - 不属于 P 的 sheet/行直接丢弃（不会装到其他项目，因为其他项目自己有文件）

两种过滤层：
  - 考账 (kaoqin_bill)：scope='enterprise' 过滤外企业行 + scope='project' 过滤外项目行
  - 工资/发薪 (wage / payroll)：仅 scope='project'

enabled 字段：
  - 0 = 不启用此规则 → 视为通过（信任挂载位置/默认）
  - 1 = 启用 → 必须命中关键词才装入，否则丢弃

接口：
  sheet_passes(cur, project_id, category, scope, sheet_name) → bool
  build_row_filter(cur, project_id, category, scope, headers) → callable(row) → bool
  build_bm_extractor(cur, project_id, headers) → callable(row, pay_time) → 'YYYY-MM' | None
  ensure_default_rules(cur, project_id) → 给项目补齐默认规则（payroll_bm 等）
"""
import json
import re


# 默认规则模板（项目注册时插入；用户可在此基础上增减）
DEFAULT_RULES = [
    # payroll_bm：业务日期提取（系统按内置 date 解析器扫 column_names 列；keywords 留空，仅占位）
    {
        'category': 'payroll_bm',
        'scope': 'project',
        'rule_type': 'column',
        'column_names': '备注|注释|摘要|批次名称|班次名称',
        'mode': 'extract',
        'keywords': [],
        'enabled': 1,
    },
]


def _load_rules(cur, project_id, category, scope, rule_type, format_id=None):
    """读本项目 enabled=1 的规则。返回 [(column_names, mode, keywords[])]。
    format_id 给定 → format 模式：取 NULL 兜底 + 当前 format 的规则
    format_id 为 None → 老模式：取全部"""
    if format_id is not None:
        cur.execute("""
            SELECT column_names, mode, keywords
            FROM project_attribution_rules
            WHERE project_id=%s AND category=%s AND scope=%s
              AND rule_type=%s AND enabled=1
              AND (format_id IS NULL OR format_id=%s)
        """, (project_id, category, scope, rule_type, int(format_id)))
    else:
        cur.execute("""
            SELECT column_names, mode, keywords
            FROM project_attribution_rules
            WHERE project_id=%s AND category=%s AND scope=%s
              AND rule_type=%s AND enabled=1
        """, (project_id, category, scope, rule_type))
    rules = []
    for cols, mode, kws_j in cur.fetchall():
        kws = kws_j if isinstance(kws_j, list) else json.loads(kws_j or '[]')
        kws = [k for k in kws if k]
        if kws:
            rules.append((cols or '', mode or 'include', kws))
    return rules


def _bump_match(cur, project_id, category, scope, rule_type, column_names, mode='include'):
    cur.execute("""
        UPDATE project_attribution_rules
        SET match_count = match_count + 1, last_matched_at = NOW()
        WHERE project_id=%s AND category=%s AND scope=%s
          AND rule_type=%s AND column_names=%s AND mode=%s
    """, (project_id, category, scope, rule_type, column_names, mode))


def sheet_passes(cur, project_id, category, scope, sheet_name, format_id=None):
    """该 sheet 是否通过本项目 (category, scope) 的 sheet 规则。

    支持 include / exclude 模式：
      - 任一 exclude 命中 → 直接 False
      - 否则 include 命中 → True
      - 启用了 include 但都未命中 → False
      - 无任何启用规则 → True（信任挂载位置）

    format_id：dispatcher 命中 rule 透传的 format。
    """
    fmt = format_id
    rules = _load_rules(cur, project_id, category, scope, 'sheet', format_id=fmt)
    if not rules:
        return True

    sn = str(sheet_name or '').strip()

    def _hit(name, kws, m):
        if m in ('eq', 'neq'):
            return any(kw == name for kw in kws)
        return any(kw in name for kw in kws)

    excludes = [(c, kws, m) for c, m, kws in rules if m in ('exclude', 'neq')]
    includes = [(c, kws, m) for c, m, kws in rules if m not in ('exclude', 'neq')]

    for cols, kws, m in excludes:
        if _hit(sn, kws, m):
            _bump_match(cur, project_id, category, scope, 'sheet', cols, m)
            return False

    if not includes:
        return True

    for cols, kws, m in includes:
        if _hit(sn, kws, m):
            _bump_match(cur, project_id, category, scope, 'sheet', cols, m)
            return True
    return False


def _find_col_index(headers, column_names_str):
    """column_names 可能是 '备注' 或 '备注|附言|摘要'；找表头里第一个命中的 index"""
    if not column_names_str:
        return None
    candidates = [c.strip() for c in column_names_str.replace(',', '|').split('|') if c.strip()]
    for i, h in enumerate(headers):
        if h is None:
            continue
        h_str = str(h)
        for c in candidates:
            if c in h_str:
                return i
    return None


def build_row_filter(cur, project_id, category, scope, headers, format_id=None):
    """返回 row → bool 函数：行是否通过本项目 (category, scope) 的列规则。

    format_id：dispatcher 命中 rule 透传的 format。"""
    fmt = format_id
    rules = _load_rules(cur, project_id, category, scope, 'column', format_id=fmt)
    if not rules:
        return lambda row: True

    # 4 种模式：
    #   include: 子串含关键词 → 保留
    #   exclude: 子串含关键词 → 丢弃（exclude 优先）
    #   eq:      值精确等于关键词 → 保留
    #   neq:     值精确等于关键词 → 丢弃（neq 优先）
    excludes_parsed = []  # exclude + neq 都进这里
    includes_parsed = []  # include + eq 都进这里
    for cols, mode, kws in rules:
        idx = _find_col_index(headers, cols)
        if idx is None:
            continue
        m = mode or 'include'
        if m in ('exclude', 'neq'):
            excludes_parsed.append((cols, idx, kws, m))
        else:
            includes_parsed.append((cols, idx, kws, m))

    if not excludes_parsed and not includes_parsed:
        return lambda row: True

    def _hit(v_str, kws, m):
        if m in ('eq', 'neq'):
            return any(kw == v_str for kw in kws)
        return any(kw in v_str for kw in kws)

    def matcher(row_values):
        # 1) exclude/neq 优先
        for cols, idx, kws, m in excludes_parsed:
            if idx >= len(row_values):
                continue
            v = row_values[idx]
            if v is None:
                continue
            v_str = str(v).strip()
            if _hit(v_str, kws, m):
                _bump_match(cur, project_id, category, scope, 'column', cols, m)
                return False

        # 2) 没 include/eq 规则 → 默认保留
        if not includes_parsed:
            return True

        # 3) include/eq：必须命中
        for cols, idx, kws, m in includes_parsed:
            if idx >= len(row_values):
                continue
            v = row_values[idx]
            if v is None:
                continue
            v_str = str(v).strip()
            if _hit(v_str, kws, m):
                _bump_match(cur, project_id, category, scope, 'column', cols, m)
                return True
        return False

    return matcher


def apply_row_filter_to_dicts(rows, *, project_id, category, scope='project',
                              conn=None, format_id=None):
    """对 std_rows (list[dict]) 应用项目归属/企业归属 column 规则。

    row 是 standard handler 输出的 dict（mart 字段名 + extra_data）。
    column_names（用户配的列名）按以下顺序取值：
        1. row[列名]（顶层）
        2. row['extra_data'][列名]
    支持 4 模式：include(子串含) / exclude(子串含 → drop) / eq(精确等于) / neq(精确等于 → drop)

    返回 (kept_rows, dropped_count)
    """
    if not rows or not project_id or not conn:
        return rows, 0
    cur = conn.cursor()
    fmt = format_id
    rules = _load_rules(cur, project_id, category, scope, 'column', format_id=fmt)
    if not rules:
        return rows, 0

    excludes = []
    includes = []
    for cols, mode, kws in rules:
        m = mode or 'include'
        candidates = [c.strip() for c in (cols or '').split('|') if c.strip()]
        if not candidates or not kws:
            continue
        target = excludes if m in ('exclude', 'neq') else includes
        target.append((candidates, kws, m))

    if not excludes and not includes:
        return rows, 0

    def _get(row, cand):
        if cand in row:
            return row[cand]
        ed = row.get('extra_data') or {}
        return ed.get(cand)

    def _hit(v_str, kws, m):
        if m in ('eq', 'neq'):
            return any(kw == v_str for kw in kws)
        return any(kw in v_str for kw in kws)

    kept = []
    dropped = 0
    for row in rows:
        # 1) exclude/neq 优先
        ex_hit = False
        for cands, kws, m in excludes:
            for c in cands:
                v = _get(row, c)
                if v is None:
                    continue
                if _hit(str(v).strip(), kws, m):
                    ex_hit = True
                    break
            if ex_hit:
                break
        if ex_hit:
            dropped += 1
            continue
        # 2) include/eq 必须命中
        if includes:
            in_hit = False
            for cands, kws, m in includes:
                for c in cands:
                    v = _get(row, c)
                    if v is None:
                        continue
                    if _hit(str(v).strip(), kws, m):
                        in_hit = True
                        break
                if in_hit:
                    break
            if not in_hit:
                dropped += 1
                continue
        kept.append(row)
    return kept, dropped


def _file_columns_match_headers(file_columns_str, headers):
    """file_columns 在 headers 中按空白分隔列名,顺序连续相邻出现"""
    if not file_columns_str:
        return True
    parts = [s.replace('#$%', ' ') for s in file_columns_str.split() if s.strip()]
    if not parts:
        return True
    n = len(parts)
    for start in range(len(headers) - n + 1):
        ok = True
        for i, p in enumerate(parts):
            h = headers[start + i]
            if h is None or p not in str(h):
                ok = False
                break
        if ok:
            return True
    return False


def build_bm_extractor(cur, project_id, headers, format_id=None):
    """业务日期提取器：返回 (row_values, pay_time) → ('ymd'/'ym'/'date_offset'/None, value)。

    规则（project_attribution_rules / payroll_bm）分两种 mode：
      mode='extract' 指定规则：从列抽 'ymd' 或 'ym' (parse_business_date)
      mode='infer'   推断规则：读列里的 date - offset_n*offset_unit → 完整 date

    解析顺序：先 extract（命中即用），再 infer（命中即用）。

    format_id：dispatcher 命中 rule 透传的 format。
    """
    from etl._utils import parse_business_date as _pbd
    from datetime import date, datetime, timedelta

    fmt = format_id

    # extract 规则
    if fmt is not None:
        cur.execute("""SELECT file_columns, column_names
                       FROM project_attribution_rules
                       WHERE project_id=%s AND category='payroll_bm'
                         AND scope='project' AND rule_type='column' AND mode='extract'
                         AND enabled=1
                         AND (format_id IS NULL OR format_id=%s)
                       ORDER BY id""", (project_id, fmt))
    else:
        cur.execute("""SELECT file_columns, column_names
                       FROM project_attribution_rules
                       WHERE project_id=%s AND category='payroll_bm'
                         AND scope='project' AND rule_type='column' AND mode='extract'
                         AND enabled=1
                       ORDER BY id""", (project_id,))
    extract_rules = []  # list of [(col_name, header_idx), ...] per rule
    for fcols, cols_str in cur.fetchall():
        if not _file_columns_match_headers(fcols, headers):
            continue
        if not cols_str:
            continue
        idxs = []
        for cn in [c.strip() for c in cols_str.split('|') if c.strip()]:
            idx = _find_col_index(headers, cn)
            if idx is not None:
                idxs.append((cn, idx))
        if idxs:
            extract_rules.append(idxs)

    # infer 规则
    if fmt is not None:
        cur.execute("""SELECT file_columns, column_names, offset_n, offset_unit
                       FROM project_attribution_rules
                       WHERE project_id=%s AND category='payroll_bm'
                         AND scope='project' AND rule_type='column' AND mode='infer'
                         AND enabled=1
                         AND (format_id IS NULL OR format_id=%s)
                       ORDER BY id""", (project_id, fmt))
    else:
        cur.execute("""SELECT file_columns, column_names, offset_n, offset_unit
                       FROM project_attribution_rules
                       WHERE project_id=%s AND category='payroll_bm'
                         AND scope='project' AND rule_type='column' AND mode='infer'
                         AND enabled=1
                       ORDER BY id""", (project_id,))
    infer_rules = []  # list of (idxs, n, unit) per rule
    for fcols, cols_str, off_n, off_unit in cur.fetchall():
        if not _file_columns_match_headers(fcols, headers):
            continue
        if not cols_str:
            continue
        idxs = []
        for cn in [c.strip() for c in cols_str.split('|') if c.strip()]:
            idx = _find_col_index(headers, cn)
            if idx is not None:
                idxs.append((cn, idx))
        if idxs:
            infer_rules.append((idxs, int(off_n or 0), off_unit or 'day'))

    if not extract_rules and not infer_rules:
        return lambda row, pay_time: (None, None)

    def _to_date(v):
        if isinstance(v, datetime):
            return v.date()
        if isinstance(v, date):
            return v
        if v is None:
            return None
        # 字符串/datetime str 用 parse_business_date 抽
        kind, val = _pbd(v, None)
        if kind == 'ymd':
            return val
        return None

    def extractor(row_values, pay_time):
        # 1) extract 规则：抽 ymd/ym
        for idxs in extract_rules:
            for cn, idx in idxs:
                if idx >= len(row_values):
                    continue
                v = row_values[idx]
                if v is None or str(v).strip() == '':
                    continue
                kind, val = _pbd(v, pay_time)
                if kind is not None:
                    return kind, val
        # 2) infer 规则：读列里的 date - offset
        for idxs, n, unit in infer_rules:
            for cn, idx in idxs:
                if idx >= len(row_values):
                    continue
                v = row_values[idx]
                if v is None or str(v).strip() == '':
                    continue
                d = _to_date(v)
                if d is None:
                    continue
                if n > 0:
                    if unit == 'month':
                        from dateutil.relativedelta import relativedelta
                        d = d - relativedelta(months=n)
                    else:
                        d = d - timedelta(days=n)
                return 'ymd', d
        return None, None

    return extractor


def ensure_default_rules(cur, project_id):
    """给项目插入默认规则（已存在则 IGNORE）。返回新插条数。"""
    n = 0
    for r in DEFAULT_RULES:
        cur.execute("""INSERT IGNORE INTO project_attribution_rules
                       (project_id, category, scope, rule_type, column_names,
                        mode, keywords, enabled)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                    (project_id, r['category'], r['scope'], r['rule_type'],
                     r['column_names'], r['mode'],
                     json.dumps(r['keywords'], ensure_ascii=False),
                     r['enabled']))
        if cur.rowcount == 1:
            n += 1
    return n
