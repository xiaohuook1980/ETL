"""工资表 sheet 解析 → mart wage_sheets

支持格式：
  1. 希锐合并表头：R1='YYYY年M月工资表', R2+R3=合并表头, R4+=数据，C='合计应付工资'
  2. 其他劳务公司格式：待加（按 task #4 扩项目时识别）

按 reference_4data_sources：工资表是劳务文员"瞎编"，业务月信息不能从内容自证，
依赖 sheet R1 标题"YYYY年M月" 解析。无法解析则跳过该 sheet。

按 schema v2：删除 department / hours / id_card_raw（劳务瞎编无意义）。
保留 is_substitute / substitute_name 用于代收/顶替标记。
"""
import re
import sys
import json
sys.path.insert(0, 'D:/小鱼AI数据')
from etl._utils import find_col, get_or_create_worker
from etl.mart.wage_sheets import upsert_wage_sheet


from etl._attribution import sheet_passes, build_row_filter


def parse_xirui(ws, row_filter=None):
    """希锐合并表头格式：R1='YYYY年M月工资表'，R2+R3 合并表头。返回 (rows, bm, headers)"""
    business_month = None
    for v in next(ws.iter_rows(max_row=1, values_only=True), ()):
        if v:
            m = re.search(r'(\d{4})年(\d{1,2})月', str(v))
            if m:
                business_month = f'{m.group(1)}-{int(m.group(2)):02d}'
                break

    header_row_idx = None
    headers = []
    for ridx, row in enumerate(ws.iter_rows(max_row=5, values_only=True), start=1):
        if row and any(v == '姓名' for v in row if v):
            header_row_idx = ridx
            headers = list(row)
            break
    if not header_row_idx:
        return [], business_month, list(headers)

    ni = find_col(headers, '姓名')
    # baseline 口径：优先"实发"/"实际"含字 → 飞船 sheet col '税前实发工资' / 横琴2 sheet col '税后实际工资'
    # 链式 find_col 控制优先级（每次按 col 顺序遍历，命中即返回）
    ai = (find_col(headers, '税前实发工资', '税前实发')
          or find_col(headers, '税后实发工资', '税后实发')
          or find_col(headers, '税后实际工资', '税后实际')
          or find_col(headers, '实发工资')
          or find_col(headers, '税前工资')
          or find_col(headers, '合计应付工资', '应付工资', '应发'))
    if ni is None or ai is None:
        return [], business_month, list(headers)

    rows_data = []
    # 自适应：希锐合并表头 R{idx+1} ni 列是 None（继续跳）；梦寺达单层表头 R{idx+1} 就是首数据
    data_start = header_row_idx + 1
    for ridx, row in enumerate(ws.iter_rows(min_row=data_start, values_only=True),
                                start=data_start):
        if not row or ni >= len(row) or row[ni] is None: continue
        name = str(row[ni]).strip()
        if not name or name in ('合计', '小计', '总计'): continue
        if row_filter is not None and not row_filter(row): continue
        try:
            amt = float(row[ai] or 0) if ai < len(row) else 0
        except (ValueError, TypeError):
            amt = 0
        if amt == 0: continue
        rows_data.append({
            'row_idx': ridx, 'name_raw': name, 'payable_amount': amt,
            'is_substitute': 0, 'substitute_name': None,
        })
    return rows_data, business_month, list(headers)


def parse_dakaq_wage(ws, row_filter=None):
    """南斗星工资表格式：R1=表头（入职日期/姓名/部门/打卡价/工时/水电/保险/.../应发工资）

    business_month 不在 sheet 内，由调用方传 fallback_bm
    row_filter: callable(row_values) → bool；返回 False 的行被丢弃（行级归属过滤）
    """
    headers = list(next(ws.iter_rows(max_row=1, values_only=True), ()))
    if not headers:
        return [], None, list(headers)

    ni = find_col(headers, '姓名', '名字')
    # baseline 口径：优先"实发"/"实际"
    ai = (find_col(headers, '税前实发工资', '税前实发')
          or find_col(headers, '税后实发工资', '税后实发')
          or find_col(headers, '税后实际工资', '税后实际')
          or find_col(headers, '实发工资')
          or find_col(headers, '税前工资')
          or find_col(headers, '应发工资', '应发', '合计应付工资'))
    if ni is None or ai is None:
        return [], None, list(headers)

    rows_data = []
    for ridx, row in enumerate(ws.iter_rows(min_row=2, values_only=True), start=2):
        if not row or ni >= len(row) or row[ni] is None: continue
        name = str(row[ni]).strip()
        if not name or name in ('合计', '小计', '总计'): continue
        if row_filter is not None and not row_filter(row): continue
        try:
            amt = float(row[ai] or 0) if ai < len(row) else 0
        except (ValueError, TypeError):
            amt = 0
        if amt == 0: continue
        rows_data.append({
            'row_idx': ridx, 'name_raw': name, 'payable_amount': amt,
            'is_substitute': 0, 'substitute_name': None,
        })
    return rows_data, None, list(headers)  # bm 由调用方决定；返回 headers 给调用方


# ============================================================
# Dispatch
# ============================================================
def _parse(ws, row_filter=None):
    """按表头特征 dispatch；row_filter 透传给 sub-parser 做行级过滤"""
    headers_text = []
    for row in ws.iter_rows(max_row=3, values_only=True):
        for v in row:
            if v is not None:
                headers_text.append(str(v).strip())
    head = '|'.join(headers_text)
    # 南斗星 / 梦寺达-长隆 单层表头格式：R1=表头含 '应发工资' + (打卡价|住宿|扣费|预支|餐费|时薪)
    if '应发工资' in head and any(k in head for k in ('打卡价', '住宿', '扣费', '预支', '餐费', '时薪')):
        return parse_dakaq_wage(ws, row_filter=row_filter)
    # 默认希锐合并表头格式
    return parse_xirui(ws, row_filter=row_filter)


# ============================================================
# 统一接口：process_sheet
# ============================================================
def process_sheet(cur, *, project_id, enterprise_id, business_cycle,
                  source_file_id, sheet_name, ws, fallback_bm=None,
                  precomputed_rows=None, format_id=None, collector=None):
    """业务月推断：优先 sheet 内部标题；fallback 从 mini_a_bill.bill_month（dispatcher 传入）

    sheet 过滤（attribution_rules + category='wage' + scope='project' + rule_type='sheet'）：
      启用规则未命中 → 跳过整个 sheet（不属于本项目）
      无规则 / 命中 → 装入本项目

    format_id: format 模式下从命中 rule 透传。
    """
    if not sheet_passes(cur, project_id, 'wage', 'project', sheet_name, format_id=format_id):
        return {'inserted': 0, 'updated': 0, 'skipped': 0, 'parsed': 0,
                'note': f'sheet {sheet_name!r} 不属于本项目（wage/project sheet 规则未命中）'}

    # 先用 _parse 的副产品 headers 构建行级过滤器
    # 因为 _parse 内部按表头识别 sub-parser，需要先取一次 headers 才能 build 行 filter
    # 用 max_row=5 扫描表头候选（兼容希锐合并表头）
    headers_for_filter = []
    for row in ws.iter_rows(max_row=5, values_only=True):
        if row and any('姓名' in str(v) for v in row if v):
            headers_for_filter = list(row)
            break

    if precomputed_rows is not None:
        rows = precomputed_rows
        bm_from_title = None
    else:
        row_filter = build_row_filter(cur, project_id, 'wage', 'project', headers_for_filter, format_id=format_id)
        rows, bm_from_title, _hdrs = _parse(ws, row_filter=row_filter)
    bm = bm_from_title or fallback_bm
    if bm is None:
        return {'inserted': 0, 'updated': 0, 'skipped': 0, 'parsed': 0,
                'note': f'no business_month from sheet title or fallback'}

    # ============================================================
    # collector 模式（方案 B）：push 到 collector，跨文件 dedup + 批量写
    # ============================================================
    if collector is not None:
        collector.add(project_id=project_id, enterprise_id=enterprise_id,
                       source_file_id=source_file_id, sheet_name=sheet_name,
                       business_month=bm, person_rows=rows)
        return {'collected': len(rows), 'parsed': len(rows),
                'project_id': project_id, 'sheet_name': sheet_name, 'bm': bm}

    n_ins = n_upd = n_skip = 0
    for r in rows:
        wid = get_or_create_worker(cur, r['name_raw'], project_id)
        if not wid:
            n_skip += 1
            continue
        action = upsert_wage_sheet(cur,
            enterprise_id=enterprise_id, project_id=project_id,
            worker_id=wid, business_month=bm,
            payable_amount=r['payable_amount'],
            is_substitute=r.get('is_substitute', 0),
            substitute_name=r.get('substitute_name'),
            source_type='xirui_wage_sheet_xlsx',
            source_file_id=source_file_id,
            source_ref=f'{sheet_name}#R{r["row_idx"]}',
            name_raw=r['name_raw'],
            is_valid=int(r.get('is_valid', 1)),
            invalid_reason=r.get('invalid_reason'),
            extra_data=r.get('extra_data'))
        if action == 'insert': n_ins += 1
        else: n_upd += 1
    return {'inserted': n_ins, 'updated': n_upd, 'skipped': n_skip, 'parsed': len(rows),
            'project_id': project_id, 'sheet_name': sheet_name}
