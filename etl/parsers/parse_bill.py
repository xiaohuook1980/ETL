"""账单 sheet 解析 → mart bill_totals + bill_persons

支持模式（按表头特征 dispatch）：
  1. sum_amount_col：表头含"姓名"+"金额" → 按金额列累加
                     bill_totals = SUM(amount)，bill_persons 按行
                     适用：南斗星汇总账单 / 大多数账单
  2. dept_subtotal：表头含"部门"+"小计"列 → 按部门累加 (TODO)
  3. 综合表：单一汇总数字（手填总额，无明细）→ 仅 bill_totals (TODO)

按 feedback_mart_bills_scope：只存"明确账单"。本 parser 不做 hours×price 派生。
按 feedback_no_unit_price_hack：单价/金额源严格限定，不能拿 DB work_amount 当账单。

业务月推断：
  - 优先 sheet 标题（如"3月账单"）
  - fallback 文件挂载的 mini_a_bill.bill_month（由 dispatcher 传入）
  - 都失败则跳过
"""
import re
import sys
sys.path.insert(0, 'D:/小鱼AI数据')
from etl._utils import find_col, safe_float, get_or_create_worker
from etl.mart.bills import upsert_bill_total, upsert_bill_person
# 延迟导入 scan_aggregate_labels：handlers/__init__.py 会加载 specialized.py，
# 后者反向导入 parse_bill 形成循环；放在函数内首次调用时再导入


def _parse_business_month_from_sheet_name(sheet_name):
    """从 sheet 名解析业务月（如 '26-3月账单' / '2026年3月账单' / '3月账单'）"""
    if not sheet_name:
        return None
    s = str(sheet_name)
    # YY-M / YYYY-M
    m = re.search(r'(\d{2,4})[-年](\d{1,2})', s)
    if m:
        y = int(m.group(1))
        if y < 100:
            y += 2000
        return f'{y:04d}-{int(m.group(2)):02d}'
    return None


def parse_sum_amount_col(ws):
    """sum_amount_col 模式：表头含'姓名'+金额列（金额/应发/账单金额/实发工资/总工时工资）

    返回 (rows, total_amount, header_row_idx)
    """
    header_row_idx = None
    headers = []
    for ridx, row in enumerate(ws.iter_rows(max_row=8, values_only=True), start=1):
        if not row: continue
        if any(v and '姓名' in str(v) for v in row):
            header_row_idx = ridx
            headers = list(row)
            break
    if header_row_idx is None:
        return [], 0.0, None

    ni = find_col(headers, '姓名', '名字', '工人姓名')
    # 优先级：实发工资 > 总工时工资 > 账单金额 > 应发 > 金额
    # （甲方账单首选实发工资，因为含管理费的最终账单金额）
    ai = (find_col(headers, '实发工资')
          or find_col(headers, '总工时工资')
          or find_col(headers, '账单金额', '合计金额')
          or find_col(headers, '应发')
          or find_col(headers, '金额'))
    if ni is None or ai is None:
        return [], 0.0, header_row_idx

    rows = []
    total = 0.0
    for ridx, row in enumerate(ws.iter_rows(min_row=header_row_idx + 1, values_only=True),
                                start=header_row_idx + 1):
        if not row or ni >= len(row) or row[ni] is None: continue
        name = str(row[ni]).strip()
        if not name or name in ('合计', '小计', '总计'): continue
        try:
            amt = float(row[ai] or 0) if ai < len(row) else 0
        except (ValueError, TypeError):
            amt = 0
        if amt == 0: continue
        rows.append({'row_idx': ridx, 'name_raw': name, 'amount': amt})
        total += amt
    return rows, total, header_row_idx


def parse_project_summary(ws):
    """项目综合表模式（梦寺达酒店类）：
    每行 = 部门+岗位组合，col=实发服务费合计
    无人员名单（bill_persons 不入；calc 走"上月发薪"等 fallback）

    自动找表头行（含 '序号'+'用工部门'+'实发服务费'）— 企鹅 R2 / 飞船 R3
    返回 (person_rows=[], total)
    """
    header_row_idx = None
    headers = []
    for ridx, r in enumerate(ws.iter_rows(max_row=8, values_only=True), 1):
        if not r: continue
        text = '|'.join(str(v) for v in r if v)
        if '序号' in text and '用工部门' in text and '实发服务费' in text:
            header_row_idx = ridx
            headers = list(r)
            break
    if header_row_idx is None:
        return [], 0.0

    sn_i = find_col(headers, '序号')
    amt_i = find_col(headers, '实发服务费', '实发服务')
    if sn_i is None or amt_i is None:
        return [], 0.0

    total = 0.0
    for ridx, r in enumerate(ws.iter_rows(min_row=header_row_idx + 1, values_only=True),
                              start=header_row_idx + 1):
        if not r or sn_i >= len(r): continue
        sn = str(r[sn_i] or '').strip()
        if not sn.replace('.', '').isdigit(): continue
        amt = safe_float(r[amt_i]) if amt_i < len(r) else None
        if amt is None or amt <= 0: continue
        total += amt
    return [], total  # 没有 bill_persons


def parse_dept_subtotal(ws, dept_col=2, amt_col=6):
    """识别部门小计行：col 0/1=None，col2=部门名（字符串），col6=金额（数值），其他列基本 None

    部门小计行通常在 sheet 末尾（每个部门一行），是甲方账单的真实金额（含管理费）。
    与 parse_sum_amount_col（按个人实发累加）不同，这里给的是含管理费的总额。

    返回 (rows, total) where rows = [{'row_idx', 'dept_name', 'amount'}]
    """
    rows = []
    total = 0.0
    for ridx, row in enumerate(ws.iter_rows(min_row=3, values_only=True), start=3):
        if not row or len(row) <= amt_col:
            continue
        c0_empty = (row[0] is None or row[0] == '')
        c1_empty = (len(row) > 1 and (row[1] is None or row[1] == ''))
        c2_str = isinstance(row[dept_col], str) and row[dept_col].strip()
        c6_num = isinstance(row[amt_col], (int, float))
        if c0_empty and c1_empty and c2_str and c6_num:
            rows.append({
                'row_idx': ridx,
                'dept_name': str(row[dept_col]).strip(),
                'amount': float(row[amt_col]),
            })
            total += float(row[amt_col])
    return rows, total


# ============================================================
# Dispatch
# ============================================================
def _parse(ws):
    """识别模式 + 算 bill_totals + bill_persons
    优先级：project_summary > dept_subtotal > sum_amount_col
    返回 (mode, person_rows, total)
    """
    # 1. project_summary（梦寺达酒店类，含 '用工部门' + '实发服务费' 表头）
    headers_text = ' '.join(str(v) for row in ws.iter_rows(max_row=3, values_only=True)
                            for v in row if v)
    if '用工部门' in headers_text and '实发服务费' in headers_text:
        _, total = parse_project_summary(ws)
        if total > 0:
            return 'project_summary', [], total

    # 2. sum_amount_col + dept_subtotal 双轨（南斗星类）
    person_rows, person_total, _ = parse_sum_amount_col(ws)
    dept_rows, dept_total = parse_dept_subtotal(ws)

    if not person_rows and not dept_rows:
        return None, [], 0.0

    mode = 'dept_subtotal' if dept_total > 0 else 'sum_amount_col'
    total = dept_total if dept_total > 0 else person_total
    # bill_persons 用个人实发（这两个 mode 共享）
    return mode, person_rows, total


# ============================================================
# 统一接口：process_sheet
# 注：dispatcher 调用时可能通过额外参数传 fallback_business_month（来自 mini_a_bill.bill_month）
# ============================================================
def process_sheet(cur, *, project_id, enterprise_id, business_cycle,
                  source_file_id, sheet_name, ws, fallback_bm=None,
                  precomputed_rows=None, format_id=None,
                  aggregate_rules=None, collector=None):
    """precomputed_rows: dispatcher 走 by-handler 路由时传入；
    形态约定为 standard.bill 输出 list of {name_raw, amount, is_valid, extra_data, ...}
    走旧路径时为 None，自动 _parse(ws)

    aggregate_rules: dispatcher 检测到项目配了聚合规则就传入（不再要求 handler='aggregate_label'）；
    list[{sheet_pattern, label, col_name, cell_ref, ...}]，非空 → 用聚合命中值覆盖 bill_totals.amount；
    bill_persons 仍按 precomputed_rows / _parse(ws) 标准解析照常入库
    """
    from etl._attribution import sheet_passes

    # sheet 层双重过滤：考账规则共用，先过 enterprise 再过 project
    # 与 parse_attendance / parse_bill_pdf 行为一致
    if not sheet_passes(cur, project_id, 'kaoqin_bill', 'enterprise', sheet_name, format_id=format_id):
        return {'inserted': 0, 'updated': 0, 'skipped': 0, 'parsed': 0,
                'note': f'sheet {sheet_name!r} 不属于本企业（kaoqin_bill/enterprise sheet 规则未命中）'}
    if not sheet_passes(cur, project_id, 'kaoqin_bill', 'project', sheet_name, format_id=format_id):
        return {'inserted': 0, 'updated': 0, 'skipped': 0, 'parsed': 0,
                'note': f'sheet {sheet_name!r} 不属于本项目（kaoqin_bill/project sheet 规则未命中）'}

    bm = _parse_business_month_from_sheet_name(sheet_name) or fallback_bm
    if bm is None:
        return {'inserted': 0, 'updated': 0, 'skipped': 0, 'parsed': 0,
                'note': f'no business_month from sheet_name={sheet_name!r} or fallback'}

    # ============================================================
    # 聚合标签账单：扫单 sheet → 命中值用作 bill_totals.amount（覆盖 SUM(persons)）
    # 仅在本 sheet 命中时生效；person 解析照常走，bill_persons 不变
    # ============================================================
    agg_total = None
    agg_details = None
    if aggregate_rules:
        from etl.parsers.handlers.aggregate_label import scan_aggregate_labels
        agg_hits = scan_aggregate_labels(ws, sheet_name, aggregate_rules)
        if agg_hits:
            agg_total = sum(h['amount'] for h in agg_hits)
            parts = []
            for h in agg_hits[:3]:
                pos = h.get('cell_ref') or f"R{h['row']}C{h['col']}"
                parts.append(f"{h['sheet']}#{pos}={h['amount']}")
            agg_details = '; '.join(parts)
            if len(agg_hits) > 3:
                agg_details += f' ...({len(agg_hits)-3} more)'

    if precomputed_rows is not None:
        # by-handler 路径：rows 已是 person 行（standard 不产 totals/dept_subtotal/project_summary）
        # 直接 SUM 当 bill_total（仅 is_valid=1 行参与）
        valid_rows = [r for r in precomputed_rows if r.get('is_valid', 1)]
        person_rows = valid_rows
        total_for_bill = sum(float(r.get('amount') or 0) for r in valid_rows)
        mode = 'standard_handler'
        # 兜底：如果有 invalid 行，仍记录 person 但 is_valid=0
        person_rows_full = precomputed_rows
    else:
        mode, person_rows, total_for_bill = _parse(ws)
        person_rows_full = person_rows
        if mode is None:
            # 标准解析失败：若聚合命中，仍写 bill_totals（0 persons）
            if agg_total is not None:
                mode = 'aggregate_label'
                person_rows = []
                person_rows_full = []
                total_for_bill = agg_total
            else:
                return {'inserted': 0, 'updated': 0, 'skipped': 0, 'parsed': 0,
                        'note': 'unsupported bill mode'}

    # 聚合命中：覆盖 bill_totals.amount + source_type，bill_persons 仍按标准解析照常入库
    if agg_total is not None:
        bill_total_amount = agg_total
        bill_total_src_type = 'aggregate_label'
        bill_total_src_ref = agg_details or sheet_name
    else:
        bill_total_amount = total_for_bill
        bill_total_src_type = mode
        bill_total_src_ref = sheet_name

    # ============================================================
    # collector 模式（方案 B）：push 到 collector，跨文件 dedup + 批量写
    # ============================================================
    if collector is not None:
        rows_to_write = person_rows_full if precomputed_rows is not None else person_rows
        collector.add(
            project_id=project_id, enterprise_id=enterprise_id,
            source_file_id=source_file_id, sheet_name=sheet_name,
            business_month=bm, person_rows=rows_to_write,
            bill_total_amount=bill_total_amount,
            bill_total_src_type=bill_total_src_type,
            bill_total_src_ref=bill_total_src_ref,
        )
        return {'collected': len(rows_to_write), 'parsed': len(person_rows),
                'mode': bill_total_src_type,
                'bill_totals_amount': round(bill_total_amount, 2),
                'bm': bm, 'aggregate_hit': agg_total is not None}

    n_ins = n_upd = n_skip = 0
    # 1. bill_totals 一行
    action_t = upsert_bill_total(cur,
        enterprise_id=enterprise_id, project_id=project_id,
        business_month=bm, amount=round(bill_total_amount, 2),
        source_type=bill_total_src_type, source_file_id=source_file_id,
        source_ref=bill_total_src_ref)
    if action_t == 'insert': n_ins += 1
    else: n_upd += 1

    # 2. bill_persons N 行（project_summary 模式不写人员）
    # by-handler 路径写全部行（含 is_valid=0），旧路径行已无 is_valid 概念
    rows_to_write = person_rows_full if precomputed_rows is not None else person_rows
    person_total = sum(float(r.get('amount') or 0) for r in person_rows)  # totals 只算有效行
    for r in rows_to_write:
        wid = get_or_create_worker(cur, r['name_raw'], project_id)
        if not wid:
            n_skip += 1
            continue
        action_p = upsert_bill_person(cur,
            enterprise_id=enterprise_id, project_id=project_id,
            worker_id=wid, business_month=bm,
            name_raw=r['name_raw'], amount=r['amount'],
            source_type='person_actual', source_file_id=source_file_id,
            source_ref=f'{sheet_name}#R{r.get("row_idx", "?")}',
            is_valid=int(r.get('is_valid', 1)),
            invalid_reason=r.get('invalid_reason'),
            extra_data=r.get('extra_data'))
        if action_p == 'insert': n_ins += 1
        else: n_upd += 1

    return {'inserted': n_ins, 'updated': n_upd, 'skipped': n_skip,
            'parsed': len(person_rows),
            'mode': bill_total_src_type,
            'bill_totals_amount': round(bill_total_amount, 2),
            'bill_persons_amount': round(person_total, 2),
            'bm': bm,
            'aggregate_hit': agg_total is not None}
