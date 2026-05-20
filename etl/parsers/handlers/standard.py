"""standard handler：column_mapping 驱动的通用明细表解析器。

适用：横平竖直的明细表（每行一条数据），不处理双区域 / 横向 pivot 等特殊结构。
那些走各 specialized handler（step 6 落地）。

输入：
    ws            openpyxl worksheet
    kind          'attendance' / 'bill' / 'wage_sheet' / 'payroll'
    column_mapping {mart 字段: 文件列名}
                  - 文件列名支持 * 单字符通配
                  - extra_data 字段值是逗号分隔的列名列表（多列装 JSON）

输出：
    list[dict]    每行一条已解析数据，键名对齐 mart 字段

【目前实现】
    - attendance kind：完整支持
    - 其他 kind：step 6 扩展
"""
import re
from etl._utils import parse_excel_date, normalize_shift, safe_float
from etl.parsers.parse_payroll_xlsx import _parse_pay_time
from etl.parsers.handlers._amount_expr import compile_expr, looks_like_expr

# 支持表达式的金额字段（双引号包列名 + 加减乘除 + 括号）
_EXPR_ENABLED_FIELDS = {'amount', 'payable_amount'}


# 必填字段（按 kind 区分）
REQUIRED_FIELDS = {
    'attendance':  ['shift_date', 'name_raw'],         # hours/quantity 二选一在解析时校验
    'bill':        ['name_raw', 'amount'],
    'wage_sheet':  ['name_raw', 'payable_amount'],
    'payroll':     ['name_raw', 'pay_time', 'work_amount'],
}

# 跳过的"假姓名"（合计行/表头副本）
NAME_BLACKLIST = {'合计', '小计', '总计', '汇总', '累计', '姓名', '名字', '户名', '收款人'}


def _match_col(pattern, header_cell):
    """单元格值是否与 pattern 匹配。pattern 含 * → regex；否则子串匹配。"""
    if header_cell is None:
        return False
    cell = str(header_cell).strip()
    if not cell:
        return False
    if '*' not in pattern:
        return pattern in cell
    regex = re.escape(pattern).replace(r'\*', '.')
    return re.search(regex, cell) is not None


def _find_col_idx(headers, col_pat):
    """col_pat 支持逗号分隔多候选（如 '生产小时, 工时'）；返第一个找到的列索引。

    注意：extra_data 字段单独处理（多列名合并装 JSON），不走这里。
    """
    if not isinstance(col_pat, str):
        return None
    candidates = [c.strip() for c in col_pat.split(',') if c.strip()]
    for cand in candidates:
        for i, h in enumerate(headers):
            if _match_col(cand, h):
                return i
    return None


def _locate_header(ws, kind, column_mapping, max_scan=10, required_override=None):
    """在前 max_scan 行里找表头行：必填字段对应列名都能找到的第一行。

    required_override: 用于动态调整 required 字段（如 payroll 用 $bill_month 时跳过 pay_time）
    返回 (header_row_idx, headers_list, col_idx_map, expr_evaluators)
    col_idx_map: {mart 字段: int 列索引}（extra_data 是 list[int]）
    expr_evaluators: {mart 字段: (callable, {col_name: idx})} —— 仅 amount / payable_amount
        可能含表达式时；col_idx_map 中该字段值为 -1 仅作"已就位"占位符
    """
    required = required_override if required_override is not None else REQUIRED_FIELDS.get(kind, [])
    rows = list(ws.iter_rows(max_row=max_scan, values_only=True))
    for ridx, row in enumerate(rows, start=1):
        if not row:
            continue
        headers = list(row)
        col_idx_map = {}
        expr_evaluators = {}
        for mart_field, col_pat in column_mapping.items():
            if not col_pat:
                continue
            if mart_field == 'extra_data':
                # 与"特征列"一致：空格分隔；逗号兼容老配置；!!! 还原真实空格
                cols = [c.replace('!!!', ' ').strip()
                        for c in re.split(r'[\s,，]+', str(col_pat))
                        if c.strip()]
                idxs = []
                for c in cols:
                    i = _find_col_idx(headers, c)
                    if i is not None:
                        idxs.append(i)
                if idxs:
                    col_idx_map['extra_data'] = idxs
                continue
            # 表达式分支：仅 amount / payable_amount 且 col_pat 含双引号
            if mart_field in _EXPR_ENABLED_FIELDS and looks_like_expr(col_pat):
                try:
                    fn, ref_cols = compile_expr(col_pat)
                except ValueError:
                    continue  # 语法错误 → 本行视为该字段缺失
                col_idxs = {}
                missing = False
                for c in ref_cols:
                    i = _find_col_idx(headers, c)
                    if i is None:
                        missing = True
                        break
                    col_idxs[c] = i
                if missing or not col_idxs:
                    continue
                expr_evaluators[mart_field] = (fn, col_idxs)
                col_idx_map[mart_field] = -1  # 占位：required 检查能通过
                continue
            idx = _find_col_idx(headers, col_pat)
            if idx is not None:
                col_idx_map[mart_field] = idx
        if all(f in col_idx_map for f in required):
            return ridx, headers, col_idx_map, expr_evaluators
    return None, None, None, None


def _cell(row, idx):
    """安全取行内某列；越界或 None 返 None"""
    if idx is None or idx >= len(row):
        return None
    return row[idx]


def _str_or_none(v):
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def _eval_expr_amount(row, evaluators, field):
    """对带 expr 评估器的字段求值。返回 float 或 None。"""
    if field not in evaluators:
        return None
    fn, col_idxs = evaluators[field]
    env = {}
    for col_name, idx in col_idxs.items():
        v = _cell(row, idx)
        env[col_name] = safe_float(v) or 0
    try:
        return float(fn(env))
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def _parse_attendance(ws, column_mapping):
    """attendance kind 的明细解析。"""
    header_row_idx, headers, col_map, _evals = _locate_header(ws, 'attendance', column_mapping)
    if header_row_idx is None:
        return []

    hours_idx = col_map.get('hours')
    qty_idx = col_map.get('quantity')
    if hours_idx is None and qty_idx is None:
        return []

    di = col_map['shift_date']
    ni = col_map['name_raw']
    extra_idxs = col_map.get('extra_data', [])

    rows = []
    for ridx, row in enumerate(ws.iter_rows(min_row=header_row_idx + 1, values_only=True),
                                start=header_row_idx + 1):
        if not row or all(v is None for v in row):
            continue
        nm = _str_or_none(_cell(row, ni))
        if not nm or nm in NAME_BLACKLIST:
            continue
        d = parse_excel_date(_cell(row, di))
        if not d:
            continue
        h = safe_float(_cell(row, hours_idx)) if hours_idx is not None else None
        q = safe_float(_cell(row, qty_idx)) if qty_idx is not None else None
        if (h is None or h <= 0) and (q is None or q <= 0):
            continue

        extra = {}
        for ei in extra_idxs:
            v = _cell(row, ei)
            if v is not None:
                col_name = str(headers[ei]).strip() if ei < len(headers) and headers[ei] else f'col{ei}'
                extra[col_name] = v

        rows.append({
            'row_idx': ridx,
            'shift_date': d,
            'name_raw': nm,
            'hours': h,
            'quantity': q,
            'shift_name':     normalize_shift(_cell(row, col_map.get('shift_name'))) if col_map.get('shift_name') is not None else None,
            'floor_or_group': _str_or_none(_cell(row, col_map.get('floor_or_group'))),
            'worker_type':    _str_or_none(_cell(row, col_map.get('worker_type'))),
            'worker_class':   _str_or_none(_cell(row, col_map.get('worker_class'))),
            'id_card_raw':    _str_or_none(_cell(row, col_map.get('id_card_raw'))),
            'extra_data':     extra or None,
        })
    return rows


def _parse_wage(ws, column_mapping):
    """wage_sheet kind 的明细解析（'姓名 + 应发工资' 行）。

    is_substitute / substitute_name 默认 0/None（用户在 UI 上配 column_mapping 暂不支持替班标记）。
    希锐合并表头 R1=标题 R2/R3=表头 R4+=数据 → max_scan=5 能找到 R2/R3 表头行。
    payable_amount 支持表达式（双引号包列名 + 加减乘除 + 括号）。
    """
    header_row_idx, headers, col_map, evals = _locate_header(ws, 'wage_sheet', column_mapping, max_scan=5)
    if header_row_idx is None:
        return []

    ni = col_map['name_raw']
    pi = col_map.get('payable_amount')  # 表达式模式下值为 -1（占位）
    extra_idxs = col_map.get('extra_data', [])
    is_expr = 'payable_amount' in evals

    rows = []
    for ridx, row in enumerate(ws.iter_rows(min_row=header_row_idx + 1, values_only=True),
                                start=header_row_idx + 1):
        if not row or all(v is None for v in row):
            continue
        nm = _str_or_none(_cell(row, ni))
        if not nm or nm in NAME_BLACKLIST:
            continue
        if is_expr:
            amt = _eval_expr_amount(row, evals, 'payable_amount')
        else:
            amt = safe_float(_cell(row, pi))
        if amt is None or amt == 0:
            continue

        extra = {}
        for ei in extra_idxs:
            v = _cell(row, ei)
            if v is not None:
                col_name = str(headers[ei]).strip() if ei < len(headers) and headers[ei] else f'col{ei}'
                extra[col_name] = v

        rows.append({
            'row_idx': ridx,
            'name_raw': nm,
            'payable_amount': amt,
            'is_substitute': 0,
            'substitute_name': None,
            'extra_data': extra or None,
        })
    return rows


def _parse_bill(ws, column_mapping):
    """bill kind 的明细解析（'姓名 + 金额' 行）。

    不处理 dept_subtotal / project_summary 等特殊模式（→ specialized handler）。
    amount 支持表达式（双引号包列名 + 加减乘除 + 括号），
    如 `"总工资（元）" + "劳务费用等（元）"`。
    """
    header_row_idx, headers, col_map, evals = _locate_header(ws, 'bill', column_mapping, max_scan=8)
    if header_row_idx is None:
        return []

    ni = col_map['name_raw']
    ai = col_map.get('amount')  # 表达式模式下值为 -1（占位）
    extra_idxs = col_map.get('extra_data', [])
    is_expr = 'amount' in evals

    rows = []
    for ridx, row in enumerate(ws.iter_rows(min_row=header_row_idx + 1, values_only=True),
                                start=header_row_idx + 1):
        if not row or all(v is None for v in row):
            continue
        nm = _str_or_none(_cell(row, ni))
        if not nm or nm in NAME_BLACKLIST:
            continue
        if is_expr:
            amt = _eval_expr_amount(row, evals, 'amount')
        else:
            amt = safe_float(_cell(row, ai))
        if amt is None or amt == 0:
            continue

        extra = {}
        for ei in extra_idxs:
            v = _cell(row, ei)
            if v is not None:
                col_name = str(headers[ei]).strip() if ei < len(headers) and headers[ei] else f'col{ei}'
                extra[col_name] = v

        rows.append({
            'row_idx': ridx,
            'name_raw': nm,
            'amount': amt,
            'id_card_raw': _str_or_none(_cell(row, col_map.get('id_card_raw'))),
            'extra_data': extra or None,
        })
    return rows


def _parse_payroll(ws, column_mapping, *, bill_month=None):
    """payroll kind 的明细解析（'姓名 + 金额 + 时间' 行）。

    业务月推断 / 项目关键词过滤 / 冲突去重 → 上层 dispatcher 接管，本层不做。

    特殊：column_mapping['pay_time'] == '$bill_month' 时，跳过 pay_time 列读取，
          用 bill_month 月首日填 pay_time（达达类无时间字段项目）。
    """
    from datetime import datetime
    use_bill_month = (column_mapping.get('pay_time') == '$bill_month')

    if use_bill_month:
        required = ['name_raw', 'work_amount']
        cm_for_header = {k: v for k, v in column_mapping.items() if k != 'pay_time'}
    else:
        required = None
        cm_for_header = column_mapping

    header_row_idx, headers, col_map, _evals = _locate_header(ws, 'payroll', cm_for_header,
                                                      max_scan=10, required_override=required)
    if header_row_idx is None:
        return []

    # bill_month 模式下 pay_time 用月首日固定值
    fallback_pt = None
    if use_bill_month:
        if not bill_month:
            return []  # 没传 bill_month，无法填 pay_time
        try:
            y, m = bill_month.split('-')
            fallback_pt = datetime(int(y), int(m), 1)
        except (ValueError, AttributeError):
            return []

    ni = col_map['name_raw']
    ai = col_map['work_amount']
    ti = col_map.get('pay_time')  # 可能为空（bill_month 模式）
    ici = col_map.get('id_card_raw')
    extra_idxs = col_map.get('extra_data', [])

    rows = []
    for ridx, row in enumerate(ws.iter_rows(min_row=header_row_idx + 1, values_only=True),
                                start=header_row_idx + 1):
        if not row or all(v is None for v in row):
            continue
        nm = _str_or_none(_cell(row, ni))
        if not nm or nm in NAME_BLACKLIST:
            continue
        amt = safe_float(_cell(row, ai))
        if amt is None or amt == 0:
            continue
        if use_bill_month:
            pt = fallback_pt
        else:
            pt = _parse_pay_time(_cell(row, ti))
            if not pt:
                continue

        extra = {}
        for ei in extra_idxs:
            v = _cell(row, ei)
            if v is not None:
                col_name = str(headers[ei]).strip() if ei < len(headers) and headers[ei] else f'col{ei}'
                extra[col_name] = v

        rows.append({
            'row_idx': ridx,
            'name_raw': nm,
            'work_amount': amt,
            'pay_time': pt,
            'id_card_raw': _str_or_none(_cell(row, ici)),
            'extra_data': extra or None,
            'payroll_kind_hint': 'bill_month_only' if use_bill_month else None,
        })
    return rows


# ============================================================
# 主入口
# ============================================================
def parse(ws, *, kind, column_mapping, auto_extra_columns=None, **ctx):
    """auto_extra_columns: dispatcher 扫规则表收集到的"过滤/校验/归属"用到的列名集合;
       自动并入 column_mapping['extra_data'],下游引擎可直接 row['extra_data'][列名] 取值。
       避免用户配 enterprise/validity 规则后忘了同步 extra_data 导致静默 drop。
    """
    if not column_mapping:
        return []
    if auto_extra_columns:
        column_mapping = _merge_auto_extra(column_mapping, auto_extra_columns)
    if kind == 'attendance':
        return _parse_attendance(ws, column_mapping)
    if kind == 'bill':
        return _parse_bill(ws, column_mapping)
    if kind == 'wage_sheet':
        return _parse_wage(ws, column_mapping)
    if kind == 'payroll':
        return _parse_payroll(ws, column_mapping, bill_month=ctx.get('bill_month'))
    raise NotImplementedError(f'standard handler 暂未支持 kind={kind}')


def _merge_auto_extra(column_mapping, auto_cols):
    """把 auto_cols 并入 column_mapping['extra_data'](跳过其他 mart 字段已占用的列名)。
    保持原 extra_data 顺序在前,自动列追加在后;返回新 dict 不改原 mapping。
    """
    cm = dict(column_mapping)
    used = set()
    for mart_field, col_pat in cm.items():
        if mart_field == 'extra_data' or not col_pat:
            continue
        if mart_field in _EXPR_ENABLED_FIELDS and looks_like_expr(col_pat):
            continue
        for c in str(col_pat).split(','):
            c = c.strip()
            if c:
                used.add(c)
    existing = cm.get('extra_data') or ''
    existing_cols = [c.replace('!!!', ' ').strip()
                     for c in re.split(r'[\s,，]+', str(existing))
                     if c.strip()]
    seen = set(existing_cols) | used
    merged = list(existing_cols)
    for c in auto_cols:
        c = (c or '').strip() if isinstance(c, str) else ''
        if c and c not in seen:
            merged.append(c)
            seen.add(c)
    if merged:
        cm['extra_data'] = ' '.join(c.replace(' ', '!!!') for c in merged)
    return cm
