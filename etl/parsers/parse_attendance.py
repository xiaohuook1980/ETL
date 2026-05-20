"""考勤 sheet 解析 → mart attendance

支持格式（按表头自动 dispatch）：
  1. 丽盈考勤：R1=标识 R2=表头 R3+=数据；列：序号/日期/班次/楼层/姓名/工时/总工时/备注
  2. 日结工合体：表头含'名字'+'中介|派遣'；列：序号/日期/名字/中介/班组/生产小时/班次/.../单价/金额
     —— 单价/金额业务无意义被丢弃，本质是考勤
  3. 计件考勤（待扩）：含'件数'/'数量'列 → 写 quantity 字段

四重过滤（feedback_kaoqin_filter_laowu_company）：
  - 工时>0：已实现（hours/quantity 为空跳过）
  - 劳务公司=本企业：xlsx 通常单一劳务，跳过；混合 xlsx 需要 laowu_company 列才能过滤
  - 审核通过 / 非异常：xlsx 通常无这两列，待真出现再加

CLI：
  python etl/parsers/parse_attendance.py --project-id N --xlsx 路径
"""
import sys
from datetime import date
sys.path.insert(0, 'D:/小鱼AI数据')
from etl._utils import (parse_excel_date, normalize_shift, safe_float,
                        find_col, get_or_create_worker, derive_business_month,
                        derive_business_period)
from etl.mart.attendance import upsert_attendance, upsert_attendance_summary


def _infer_worker_class(sheet_name):
    """按 sheet 名推断长期/短期工。
    sheet 名含'长期'/'长工' → 长期工
    sheet 名含'短期'/'短工'/纯日期 → 短期工
    其他 → None（兼容）
    """
    if not sheet_name:
        return None
    s = str(sheet_name)
    if '长期' in s or '长工' in s:
        return '长期工'
    if '短期' in s or '短工' in s:
        return '短期工'
    # 纯日期 sheet 名（如 '3.26' / '4.20'）默认短期工
    import re as _re
    if _re.match(r'^\d{1,2}\.\d{1,2}$', s):
        return '短期工'
    return None


# ============================================================
# 格式 1：丽盈/简洁考勤（按 find_col 动态识别表头，支持 5/8 列变体 + 多组并列）
# 表头行：含'序号'+'日期'+'班次'+'姓名'+'工时'（楼层/班组/总工时/备注 可选）
# 多组并列：R1 中可能有多个 '序号' 列（如澳思美 col A-E 白班 + col H-L 夜班，中间空列分隔）
# 数据从表头行后开始
# ============================================================
def parse_liying(ws):
    # 找表头行（前 5 行内含 '日期' + '姓名' + '工时'）
    header_row_idx = None
    headers = []
    for ridx, row in enumerate(ws.iter_rows(max_row=5, values_only=True), start=1):
        if not row: continue
        text = '|'.join(str(v) for v in row if v)
        if '日期' in text and ('姓名' in text or '名字' in text) and '工时' in text:
            header_row_idx = ridx
            headers = list(row)
            break
    if header_row_idx is None:
        return []

    # 找所有"序号"起始列 → 分组
    group_starts = [i for i, v in enumerate(headers) if v == '序号']
    if not group_starts:
        # 没"序号"列：兜底全表头一组
        group_starts = [0]

    # 为每组在 [start_col, next_start_col) 范围内找列
    groups = []
    for gi, start in enumerate(group_starts):
        end = group_starts[gi + 1] if gi + 1 < len(group_starts) else len(headers)
        sub = headers[start:end]
        di = find_col(sub, '日期')
        si = find_col(sub, '班次')
        fi = find_col(sub, '楼层', '班组', '部门')
        ni = find_col(sub, '姓名', '名字')
        hi = find_col(sub, '工时', '小时')
        if di is None or ni is None or hi is None:
            continue
        # 转回绝对列索引
        groups.append({
            'di': start + di, 'si': start + si if si is not None else None,
            'fi': start + fi if fi is not None else None,
            'ni': start + ni, 'hi': start + hi,
        })
    if not groups:
        return []

    rows = []
    for ridx, row in enumerate(ws.iter_rows(min_row=header_row_idx + 1, values_only=True),
                                start=header_row_idx + 1):
        if not row or all(v is None for v in row):
            continue
        for g in groups:
            ni, di, si, fi, hi = g['ni'], g['di'], g['si'], g['fi'], g['hi']
            if ni >= len(row) or row[ni] is None: continue
            nm = str(row[ni]).strip()
            if not nm or nm in ('合计', '小计', '总计', '姓名', '名字'): continue
            d = parse_excel_date(row[di] if di < len(row) else None)
            if not d: continue
            h = safe_float(row[hi]) if hi < len(row) else None
            if h is None or h <= 0: continue
            rows.append({
                'row_idx': ridx,
                'shift_date': d,
                'shift_name': (normalize_shift(row[si])
                               if si is not None and si < len(row) else None),
                'worker_type': None,
                'floor_or_group': (str(row[fi]).strip()
                                   if fi is not None and fi < len(row) and row[fi] else None),
                'name_raw': nm,
                'hours': h,
                'quantity': None,
            })
    return rows


# ============================================================
# 格式 2：日结工合体（表头含"名字"+"中介|派遣"）
# ============================================================
def parse_rijiegong(ws):
    header_row_idx = None
    headers = []
    for ridx, row in enumerate(ws.iter_rows(max_row=5, values_only=True), start=1):
        if not row: continue
        for v in row:
            if v and str(v).strip() in ('序号', '日期'):
                header_row_idx = ridx
                headers = list(row)
                break
        if header_row_idx: break
    if header_row_idx is None:
        return []

    ni = find_col(headers, '名字', '姓名')
    di = find_col(headers, '日期')
    fi = find_col(headers, '班组', '楼层', '部门')
    si = find_col(headers, '班次')
    hi = find_col(headers, '生产小时', '工时', '小时')
    ti = find_col(headers, '工种', '岗位')
    if ni is None or di is None:
        return []

    rows = []
    for ridx, row in enumerate(ws.iter_rows(min_row=header_row_idx + 1, values_only=True),
                                start=header_row_idx + 1):
        if not row or ni >= len(row) or row[ni] is None: continue
        name = str(row[ni]).strip()
        if not name or name in ('合计', '小计', '总计'): continue
        d = parse_excel_date(row[di])
        if d is None: continue
        h = safe_float(row[hi]) if hi is not None and hi < len(row) else None
        if h is None or h <= 0:
            continue
        rows.append({
            'row_idx': ridx,
            'shift_date': d,
            'shift_name': normalize_shift(row[si]) if si is not None and si < len(row) else None,
            'worker_type': str(row[ti]).strip() if ti is not None and ti < len(row) and row[ti] else None,
            'floor_or_group': str(row[fi]).strip() if fi is not None and fi < len(row) and row[fi] else None,
            'name_raw': name,
            'hours': h,
            'quantity': None,
        })
    return rows


# ============================================================
# 格式 3：南斗星打卡格式（"离职"/"在职" sheet）
# 表头 R1：人员编号/姓名/部门名称/打卡日期/打卡次数/最早时间/最晚时间/打卡时间/上班小时/夜班小时
# 在职 sheet 末列可能有"楼层"信息（C11）
# ============================================================
def parse_dakaq(ws):
    headers = []
    header_row_idx = None
    for ridx, row in enumerate(ws.iter_rows(max_row=3, values_only=True), start=1):
        if not row: continue
        if any(v and '打卡日期' in str(v) for v in row):
            header_row_idx = ridx
            headers = list(row)
            break
    if header_row_idx is None:
        return []

    ni = find_col(headers, '姓名', '名字')
    di = find_col(headers, '打卡日期', '日期')
    fi = find_col(headers, '部门')   # 部门名称 也含 '部门'
    hi = find_col(headers, '上班小时', '工时', '生产小时')
    nhi = find_col(headers, '夜班小时')
    if ni is None or di is None:
        return []

    rows = []
    for ridx, row in enumerate(ws.iter_rows(min_row=header_row_idx + 1, values_only=True),
                                start=header_row_idx + 1):
        if not row or ni >= len(row) or row[ni] is None: continue
        name = str(row[ni]).strip()
        if not name or name in ('合计', '小计', '总计'): continue
        d = parse_excel_date(row[di])
        if d is None: continue
        h_day = safe_float(row[hi]) if hi is not None and hi < len(row) else None
        h_night = safe_float(row[nhi]) if nhi is not None and nhi < len(row) else None
        # 工时 = 上班小时 + 夜班小时
        h = (h_day or 0) + (h_night or 0)
        if h <= 0:
            continue
        # 末列可能是楼层
        floor = None
        if len(row) > 10 and row[10]:
            floor = str(row[10]).strip()
        rows.append({
            'row_idx': ridx,
            'shift_date': d,
            'shift_name': '夜班' if h_night and h_night > 0 and (not h_day or h_day == 0) else
                          ('白班' if h_day and h_day > 0 else None),
            'worker_type': None,
            'floor_or_group': floor or (str(row[fi]).strip() if fi is not None and fi < len(row) and row[fi] else None),
            'name_raw': name,
            'hours': h,
            'quantity': None,
        })
    return rows


# ============================================================
# 格式 4：万汇/菜鸟出勤记录格式（"Sheet1"）
# 表头 R1：审核状态/工号/姓名/组织/大组/大组主管/小组/小组组长/岗位/劳务公司/类型/
#          出勤日期/班次ID/上班时间/下班时间/上班打卡时间/下班打卡时间/上班点名时间/下班点名时间/
#          出勤状态/工时属性/结算工时/点名工时/加班工时/休息时长/出勤记录/异常类型/备注
#
# feedback_kaoqin_filter_laowu_company 四重过滤实现：
#   - 审核通过：col 0='审核通过'
#   - 非异常：col 19='正常' 且 col 26 异常类型为空
#   - 工时>0：col 21 结算工时
#   - 劳务公司=本企业：暂不实现（劳务公司列存在但子公司/品牌问题，待业务方确认配置）
# ============================================================
def parse_wanhui_chuqin(ws, laowu_keywords=None):
    """万汇/菜鸟出勤记录格式。
    laowu_keywords: 劳务公司过滤关键词列表，列9 必须含任一关键词；空则不过滤
    """
    headers = list(next(ws.iter_rows(max_row=1, values_only=True), ()))
    if not headers:
        return []

    audit_i = find_col(headers, '审核状态')
    name_i = find_col(headers, '姓名')
    laowu_i = find_col(headers, '劳务公司')
    pos_i = find_col(headers, '岗位', '工种')
    group_i = find_col(headers, '大组', '小组')
    date_i = find_col(headers, '出勤日期')
    shift_attr_i = find_col(headers, '工时属性')
    hours_i = find_col(headers, '结算工时')
    status_i = find_col(headers, '出勤状态')
    abnormal_i = find_col(headers, '异常类型')

    if name_i is None or date_i is None or hours_i is None:
        return []

    rows = []
    for ridx, row in enumerate(ws.iter_rows(min_row=2, values_only=True), start=2):
        if not row or name_i >= len(row) or row[name_i] is None: continue
        name = str(row[name_i]).strip()
        if not name or name in ('合计', '小计', '总计'): continue

        # 四重过滤（feedback_kaoqin_filter_laowu_company）
        # 1. 审核通过
        if audit_i is not None and audit_i < len(row):
            if row[audit_i] and str(row[audit_i]).strip() != '审核通过':
                continue
        # 2. 非异常 = col 19 出勤状态 != '异常'（早退/迟到/正常 都算入；col 26 异常类型为辅助信息）
        if status_i is not None and status_i < len(row):
            if row[status_i] and str(row[status_i]).strip() == '异常':
                continue
        # 3. 劳务公司=本企业
        if laowu_keywords and laowu_i is not None and laowu_i < len(row):
            laowu_val = str(row[laowu_i] or '').strip()
            if not any(kw in laowu_val for kw in laowu_keywords):
                continue

        d = parse_excel_date(row[date_i])
        if d is None: continue
        h = safe_float(row[hours_i]) if hours_i < len(row) else None
        if h is None or h <= 0: continue

        rows.append({
            'row_idx': ridx,
            'shift_date': d,
            'shift_name': normalize_shift(row[shift_attr_i]) if shift_attr_i is not None and shift_attr_i < len(row) else None,
            'worker_type': str(row[pos_i]).strip() if pos_i is not None and pos_i < len(row) and row[pos_i] else None,
            'floor_or_group': str(row[group_i]).strip() if group_i is not None and group_i < len(row) and row[group_i] else None,
            'name_raw': name,
            'hours': h,
            'quantity': None,
        })
    return rows


# ============================================================
# 格式 5：月度横向模板（梦寺达酒店）— 每行=工人，每列=日期
# R1：标题（含 'XX酒店2026年X月劳务人员计时/计间考勤表'）
# R3：部门：XXX
# R4：序号/工号/姓名/1/2/.../31（每日一列）
# R5：星期：一/二/.../日（跳过）
# R6+：数据行（数字时长 + None / '/'）
# ============================================================
import re as _re
def parse_yuedu_hengxiang(ws):
    rows = list(ws.iter_rows(values_only=True))
    # R1 标题解析年月
    title = ''
    if rows and rows[0]:
        title = ' '.join(str(v) for v in rows[0] if v)
    m = _re.search(r'(\d{4})年(\d{1,2})月', title)
    if not m:
        return []
    year, month = int(m.group(1)), int(m.group(2))
    # 仅装入 >= 2026-02（过滤掉 sheet 名='1月'/'2月' 等旧模板里 R1='企鹅酒店劳务外包人员2017年01月做房考勤表' 的 2017 年；1 月数据业务上不需要）
    bm = year * 100 + month
    if bm < 202602:
        return []

    # 部门 R3
    department = None
    if len(rows) > 2 and rows[2]:
        text = ' '.join(str(v) for v in rows[2] if v)
        dm = _re.search(r'部门[:：]\s*([^部\s]+)', text)
        if dm:
            department = dm.group(1).strip()[:60]

    # 找表头行：含'序号'+'姓名'+(工号/员工编码/兼职卡号/员工号)
    header_row_idx = None
    headers = []
    for ri, row in enumerate(rows):
        if not row: continue
        # 单元格内换行/空白 normalize:'序\n号' → '序号'
        text = '|'.join(_re.sub(r'\s+', '', str(v)) for v in row if v)
        if ('序号' in text and '姓名' in text and
                any(k in text for k in ('工号', '员工编码', '兼职卡号', '员工号'))):
            header_row_idx = ri
            # 同样 normalize headers,让后续 find_col '序号' 等命中
            headers = [_re.sub(r'\s+', '', str(v)) if v is not None else None for v in row]
            break
    if header_row_idx is None:
        return []

    sn_i = find_col(headers, '序号')
    wn_i = find_col(headers, '工号', '员工编码', '兼职卡号', '员工号')
    ni = find_col(headers, '姓名')
    if ni is None: return []

    # 日期列：表头 col 中能解析为 1-31 数字的
    date_cols = []  # [(col_idx, day)]
    for ci, h in enumerate(headers):
        if h is None: continue
        try:
            d = int(float(h))
            if 1 <= d <= 31:
                date_cols.append((ci, d))
        except (ValueError, TypeError):
            pass

    # 月汇总列：日列全 '/' 时，回退到这一列（飞船 3 月计时格式）
    sum_hours_i = find_col(headers, '工时合计', '小时合计', '总工时', '累计工时')
    bm_str = f'{year:04d}-{month:02d}'

    out_rows = []
    for ri, row in enumerate(rows[header_row_idx + 2:], start=header_row_idx + 3):  # 跳过表头+星期
        if not row or ni >= len(row) or row[ni] is None: continue
        name = str(row[ni]).strip()
        if not name or name in ('合计', '小计', '总计'): continue

        # 先尝试日级
        day_records = []
        for ci, day in date_cols:
            if ci >= len(row): continue
            cell = row[ci]
            h = safe_float(cell)
            if h is None or h <= 0: continue
            try:
                d = date(year, month, day)
            except ValueError:
                continue
            day_records.append({
                'row_idx': ri,
                'shift_date': d,
                'shift_name': None,
                'worker_type': None,
                'floor_or_group': department,
                'name_raw': name,
                'hours': h,
                'quantity': None,
            })

        if day_records:
            out_rows.extend(day_records)
        elif sum_hours_i is not None and sum_hours_i < len(row):
            # 月汇总分支：日列全无值但有"工时合计"列
            sum_h = safe_float(row[sum_hours_i])
            if sum_h is not None and sum_h > 0:
                out_rows.append({
                    'is_summary': True,
                    'row_idx': ri,
                    'shift_date': None,
                    'shift_name': None,
                    'worker_type': None,
                    'floor_or_group': department,
                    'name_raw': name,
                    'hours': sum_h,
                    'quantity': None,
                    'bm': bm_str,
                })
    return out_rows


# ============================================================
# 格式 6：长期工月度横向（澳思美）— R2 表头 + R3 日期数字（25,26,...,31,1,2,...,24 跨月）
# R2: '序号', '姓名', _, [Excel 序列号], _..., '总工时'
# R3: None, None, 25, 26, 27, ..., 31, 1, 2, ..., 24
# R4+: 序号, 姓名, [每日工时...], 总工时
# 月份锚点：R2 中第一个 Excel 序列号确定起始月
# ============================================================
def parse_changqi_hengxiang(ws):
    rows = list(ws.iter_rows(values_only=True))
    if len(rows) < 4:
        return []

    # 找 R2 表头（含 '姓名' + '总工时'，'序号' 可选）
    header_row_idx = None
    r2 = None
    for ri, row in enumerate(rows[:5]):
        if not row: continue
        text = '|'.join(str(v) for v in row if v)
        if '姓名' in text and '总工时' in text:
            header_row_idx = ri
            r2 = list(row)
            break
    if header_row_idx is None or header_row_idx + 1 >= len(rows):
        return []

    r3 = list(rows[header_row_idx + 1])
    ni = next((i for i, v in enumerate(r2) if v == '姓名'), None)
    if ni is None:
        return []

    # R2 中第一个 Excel 序列号 → 锚点月（推算起始月）
    anchor_year = anchor_month = None
    for ci, v in enumerate(r2):
        if isinstance(v, (int, float)) and 30000 < v < 60000:
            d = parse_excel_date(int(v))
            if d:
                anchor_year, anchor_month = d.year, d.month
                break
    if anchor_year is None:
        return []

    # R3 日期数字 → 解析跨月：day < prev_day 时月+1
    from datetime import date as _date
    date_cols = []
    current_year, current_month = anchor_year, anchor_month
    prev_day = None
    for ci, v in enumerate(r3):
        if v is None: continue
        try:
            day = int(float(v))
        except (ValueError, TypeError):
            continue
        if not (1 <= day <= 31): continue
        if prev_day is not None and day < prev_day:
            current_month += 1
            if current_month > 12:
                current_month = 1
                current_year += 1
        try:
            date_cols.append((ci, _date(current_year, current_month, day)))
        except ValueError:
            pass
        prev_day = day

    # 数据行 R4+
    out_rows = []
    for ri, row in enumerate(rows[header_row_idx + 2:], start=header_row_idx + 3):
        if not row or ni >= len(row) or row[ni] is None:
            continue
        nm = str(row[ni]).strip()
        if not nm or nm in ('合计', '小计', '总计'):
            continue
        for ci, d in date_cols:
            if ci >= len(row): continue
            h = safe_float(row[ci])
            if h is None or h <= 0: continue
            out_rows.append({
                'row_idx': ri,
                'shift_date': d,
                'shift_name': None,
                'worker_type': None,
                'floor_or_group': None,
                'name_raw': nm,
                'hours': h,
                'quantity': None,
            })
    return out_rows


# ============================================================
# 格式 9.5：月度考勤核对表（恒众源/中集等）— 日级符号化（√/半）
# R1 表头: 劳务公司 / 雇佣类型 / 工号 / 姓名 / 一级部门 / 班组 / 进司日期 / 出勤天数 / ...
#         + 31 个日期列（'日\\n1','一\\n2',...）
# R2+ 数据行: '√'=全勤(1天=日工时小时), '半'=半天(0.5*日工时), 空=缺勤
# R1 标题在文件名,日期列从 1-31 推断业务月（按文件名上下文 / sheet 名）
# ============================================================
"""月度考勤核对表(恒众源/中集等)按"天"结算:
   √ → quantity=1.0(全勤一天)
   半 → quantity=0.5
   空 → 跳过
attendance.hours 留空(N/A);attendance.quantity 存天数。calc 时按单价单位(元/天)算。
"""

def parse_yuedu_hekedui(ws, year=None, month=None):
    """year/month 是 fallback(来自 mini_a_bill.bill_month);先从文件内容找,找不到才用 fallback"""
    rows = list(ws.iter_rows(values_only=True))
    if len(rows) < 2:
        return []

    # 优先从文件内容(R1 标题行/sheet 名)抠业务月。
    # 严格要求"YYYY年X月"格式,避免误命中 R2 的"进司日期"等字段
    text = ' '.join(str(v) for v in (rows[0] or ()) if v)
    text += ' ' + str(getattr(ws, 'title', '') or '')
    found_y, found_m = None, None
    m_match = _re.search(r'(\d{4})年(\d{1,2})月', text)
    if m_match:
        try:
            yy, mm = int(m_match.group(1)), int(m_match.group(2))
            if 2020 <= yy <= 2099 and 1 <= mm <= 12:
                found_y, found_m = yy, mm
        except (ValueError, TypeError):
            pass
    # 文件内容找到 → 用文件内的;没找到 → 用参数(fallback bill_month)
    if found_y is not None:
        year, month = found_y, found_m

    headers = list(rows[0])
    name_i = find_col(headers, '姓名')
    wn_i = find_col(headers, '工号', '员工编码')
    dept_i = find_col(headers, '一级部门', '部门')
    if name_i is None:
        return []

    # 找日期列: 列里含 "日 1"/"一 2" 格式或仅数字 1-31
    date_cols = []  # [(col_idx, day)]
    for ci, h in enumerate(headers):
        if h is None:
            continue
        s = str(h)
        m_d = _re.search(r'(\d{1,2})', s)
        if m_d:
            try:
                d = int(m_d.group(1))
                if 1 <= d <= 31 and ci > (name_i or 0):  # 排除"工号"等含数字的非日期列
                    # 进一步排除:含中文数字日期(如"3.15"这种)
                    if any(zw in s for zw in ('日', '一', '二', '三', '四', '五', '六')) or s.strip().isdigit() or _re.match(r'^\d+\.?\d*$', s.strip()):
                        date_cols.append((ci, d))
            except (ValueError, TypeError):
                pass

    if not date_cols or year is None:
        return []

    bm_str = f'{year:04d}-{month:02d}'
    out = []
    for ri, row in enumerate(rows[1:], start=2):
        if not row or name_i >= len(row) or row[name_i] is None:
            continue
        name = str(row[name_i]).strip()
        if not name or name in ('合计', '小计', '总计'):
            continue
        dept = str(row[dept_i]).strip() if dept_i is not None and dept_i < len(row) and row[dept_i] else None
        for ci, day in date_cols:
            if ci >= len(row):
                continue
            cell = row[ci]
            if cell is None:
                continue
            s = str(cell).strip()
            if not s:
                continue
            # 符号映射(按"天"维度,装 quantity 列;hours 留空)
            if s == '√' or s == '✓' or s == 'V':
                qty = 1.0
            elif s == '半' or s == '0.5':
                qty = 0.5
            else:
                # 兼容数字直接当天数(如"1.0"/"0.5")
                try:
                    qty = float(s)
                    if qty <= 0:
                        continue
                except ValueError:
                    continue  # 其他符号(假/旷)跳过
            try:
                d = date(year, month, day)
            except ValueError:
                continue
            out.append({
                'row_idx': ri,
                'shift_date': d,
                'shift_name': None,
                'worker_type': None,
                'floor_or_group': dept,
                'name_raw': name,
                'hours': None,      # 按天结算,hours 不填
                'quantity': qty,    # 天数:1.0 全勤,0.5 半天
            })
    return out


# ============================================================
# 格式 9：新广益月度横向（R2 标题含"YYYY年（M.D-M.D）" / R3 序号+姓名+部门+班别+日期 / R4 数字日期跨月 / R5+ 数据）
# 跟梦寺达 yuedu_hengxiang 类似但年月从 R2 标题提取
# ============================================================
def parse_yuedu_xinguangyi(ws):
    rows = list(ws.iter_rows(values_only=True))
    if len(rows) < 5:
        return []

    # R1/R2 找业务周期，两种格式：
    #   'YYYY年（M.D-M.D）'  生产部格式
    #   '(YYYY.M.D-YYYY.M.D)' 或 '(YYYY.M.D-M.D)' 涂布格式
    title = ' '.join(str(v) for row in rows[:3] for v in (row or ()) if v)
    import re as _re
    m = _re.search(r'(\d{4})年\s*[（(]\s*(\d{1,2})\.(\d{1,2})\s*-\s*(\d{1,2})\.(\d{1,2})', title)
    if not m:
        # 涂布格式: (YYYY.M.D-YYYY.M.D)  忽略第二年份（默认相同周期）
        m = _re.search(r'[（(]\s*(\d{4})\.(\d{1,2})\.(\d{1,2})\s*-\s*(?:\d{4}\.)?(\d{1,2})\.(\d{1,2})', title)
        if not m:
            return []
    year = int(m.group(1))
    start_month = int(m.group(2))
    start_day_field = int(m.group(3))
    end_month = int(m.group(4))

    # R3 找表头（含 '序号' '姓名'）
    header_row_idx = None
    for ri, row in enumerate(rows[:5]):
        if not row: continue
        text = '|'.join(str(v) for v in row if v)
        if '序号' in text and '姓名' in text:
            header_row_idx = ri
            break
    if header_row_idx is None or header_row_idx + 1 >= len(rows):
        return []

    r3 = list(rows[header_row_idx])
    r4 = list(rows[header_row_idx + 1])
    ni = next((i for i, v in enumerate(r3) if v == '姓名'), None)
    fi = next((i for i, v in enumerate(r3) if v == '所在部门'), None)
    si = next((i for i, v in enumerate(r3) if v == '班别'), None)
    if ni is None: return []

    # R4 日期：数字 1-31 或 datetime.date
    from datetime import date as _date, datetime as _dt
    date_cols = []
    current_year, current_month = year, start_month
    prev_day = None
    for ci, v in enumerate(r4):
        if v is None: continue
        if isinstance(v, (_dt, _date)):
            d = v.date() if isinstance(v, _dt) else v
            date_cols.append((ci, d))
            current_year, current_month = d.year, d.month
            prev_day = d.day
            continue
        try:
            day = int(float(v))
        except (ValueError, TypeError):
            continue
        if not (1 <= day <= 31): continue
        if prev_day is not None and day < prev_day:
            current_month += 1
            if current_month > 12:
                current_month = 1
                current_year += 1
        try:
            date_cols.append((ci, _date(current_year, current_month, day)))
        except ValueError:
            pass
        prev_day = day

    out_rows = []
    for ri, row in enumerate(rows[header_row_idx + 2:], start=header_row_idx + 3):
        if not row or ni >= len(row) or row[ni] is None: continue
        nm = str(row[ni]).strip()
        if not nm or nm in ('合计', '小计', '总计'): continue
        for ci, d in date_cols:
            if ci >= len(row): continue
            cell = row[ci]
            h = safe_float(cell)
            if h is None or h <= 0: continue
            out_rows.append({
                'row_idx': ri,
                'shift_date': d,
                'shift_name': (normalize_shift(row[si])
                               if si is not None and si < len(row) and row[si] else None),
                'worker_type': None,
                'floor_or_group': (str(row[fi]).strip()
                                   if fi is not None and fi < len(row) and row[fi] else None),
                'name_raw': nm,
                'hours': h,
                'quantity': None,
            })
    return out_rows


# ============================================================
# 格式 8：康丽达月度横向（R1 'XX X月考勤' / R3 序号+姓名+...+日期+合计 / R4 '1日'-'31日' / R5+ 数据）
# sheet 名 'X月' 推断月份；R5+ 每行 = 一个工人，col 是按日工时
# ============================================================
def parse_yuedu_riziduan(ws, sheet_name=None, year=None):
    """康丽达 / 类似格式：日期行用 'X日' 字符串，sheet name 是 'X月'"""
    rows = list(ws.iter_rows(values_only=True))
    if len(rows) < 5:
        return []

    # R1 找 '考勤' 标题中含 'X月'
    title_text = ' '.join(str(v) for row in rows[:2] for v in (row or ()) if v)
    sn = str(sheet_name or '')
    # 尝试从 sheet name / R1 提取月份
    import re as _re
    m = _re.search(r'(\d{1,2})月', sn) or _re.search(r'(\d{1,2})月', title_text)
    if not m:
        return []
    month = int(m.group(1))
    if not year:
        # 默认当年
        from datetime import date as _date
        year = _date.today().year

    # 找 R3 表头（含 '序号' '姓名'）
    header_row_idx = None
    for ri, row in enumerate(rows[:5]):
        if not row: continue
        text = '|'.join(str(v) for v in row if v)
        if '序号' in text and '姓名' in text:
            header_row_idx = ri
            break
    if header_row_idx is None or header_row_idx + 1 >= len(rows):
        return []

    r3 = list(rows[header_row_idx])
    r4 = list(rows[header_row_idx + 1])
    sn_i = next((i for i, v in enumerate(r3) if v == '序号'), None)
    ni = next((i for i, v in enumerate(r3) if v == '姓名'), None)
    if ni is None:
        return []

    # 日期列：R4 中 'X日' 字符串
    from datetime import date as _date
    date_cols = []
    for ci, v in enumerate(r4):
        if v is None: continue
        s = str(v).strip()
        m2 = _re.match(r'^(\d{1,2})日$', s)
        if m2:
            day = int(m2.group(1))
            try:
                date_cols.append((ci, _date(year, month, day)))
            except ValueError:
                pass

    out_rows = []
    for ri, row in enumerate(rows[header_row_idx + 2:], start=header_row_idx + 3):
        if not row or ni >= len(row) or row[ni] is None: continue
        nm = str(row[ni]).strip()
        if not nm or nm in ('合计', '小计', '总计'): continue
        for ci, d in date_cols:
            if ci >= len(row): continue
            cell = row[ci]
            h = safe_float(cell)
            if h is None or h <= 0: continue
            out_rows.append({
                'row_idx': ri,
                'shift_date': d,
                'shift_name': None,
                'worker_type': None,
                'floor_or_group': None,
                'name_raw': nm,
                'hours': h,
                'quantity': None,
            })
    return out_rows


# ============================================================
# 格式 7：顺丰结算数据 / 出勤数据
# 结算 R1: 报名ID/站点/劳务机构/姓名/身份证号/日期/需求/班次/班次时间/结算时长(小时)/结算金额(元)
# 出勤 R1: 报名ID/姓名/手机号/工号/身份证号/任务名称/日期/班次名称/班次工作时间/工序/上班打卡时间/下班打卡时间
# ============================================================
def parse_shunfeng_jiesuan(ws):
    """顺丰结算/出勤数据：表头 R1，按列名动态找。
    hours 优先 '结算时长'；无则按打卡时间差算（出勤 xlsx）"""
    headers = list(next(ws.iter_rows(max_row=1, values_only=True), ()))
    if not headers:
        return []
    ni = find_col(headers, '姓名', '收款人姓名')
    di = find_col(headers, '日期', '出勤日期')
    si = find_col(headers, '班次名称', '班次')
    hi = find_col(headers, '结算出勤时长', '结算时长', '工时')
    ici = find_col(headers, '身份证号', '身份证')
    in_i = find_col(headers, '上班打卡时间')
    out_i = find_col(headers, '下班打卡时间')
    if ni is None or di is None:
        return []

    rows = []
    for ridx, row in enumerate(ws.iter_rows(min_row=2, values_only=True), start=2):
        if not row or ni >= len(row) or row[ni] is None: continue
        nm = str(row[ni]).strip()
        if not nm or nm in ('合计', '小计', '总计'): continue
        d = parse_excel_date(row[di] if di < len(row) else None)
        if not d: continue
        h = None
        if hi is not None and hi < len(row):
            h = safe_float(row[hi])
        if (h is None or h <= 0) and in_i is not None and out_i is not None:
            # 按打卡时间差算
            from datetime import datetime as _dt, timedelta as _td
            t_in = row[in_i] if in_i < len(row) else None
            t_out = row[out_i] if out_i < len(row) else None
            if isinstance(t_in, _dt) and isinstance(t_out, _dt):
                diff = (t_out - t_in).total_seconds() / 3600
                h = round(diff, 2) if diff > 0 else None
        if h is None or h <= 0: continue
        id_card = (str(row[ici]).strip()
                   if ici is not None and ici < len(row) and row[ici] else None)
        rows.append({
            'row_idx': ridx,
            'shift_date': d,
            'shift_name': (normalize_shift(row[si])
                           if si is not None and si < len(row) and row[si] else None),
            'worker_type': None,
            'floor_or_group': None,
            'name_raw': nm,
            'id_card_raw': id_card,
            'hours': h,
            'quantity': None,
        })
    return rows


# ============================================================
# Dispatch：按 sheet 表头自动选择 parser
# ============================================================
def _parse(ws, laowu_keywords=None, fallback_bm=None):
    headers_text = []
    for row in ws.iter_rows(max_row=4, values_only=True):
        for v in row:
            if v is not None:
                # normalize:'序\n号' → '序号'(单元格内换行/空白统一压成空)
                headers_text.append(_re.sub(r'\s+', '', str(v)))
    head = '|'.join(headers_text)
    # 万汇/菜鸟：'出勤日期' + '结算工时'
    if '出勤日期' in head and '结算工时' in head:
        return parse_wanhui_chuqin(ws, laowu_keywords=laowu_keywords)
    # 南斗星打卡：'打卡日期' + '上班小时'
    if '打卡日期' in head and ('上班小时' in head or '打卡次数' in head):
        return parse_dakaq(ws)
    # 月度横向模板（梦寺达）：'考勤表' + '部门' + '序号' + ('工号'|'员工编码'|'兼职卡号')
    if ('考勤表' in head and '部门' in head and '序号' in head
            and any(k in head for k in ('工号', '员工编码', '兼职卡号', '员工号'))):
        return parse_yuedu_hengxiang(ws)
    # 月度考勤核对表（恒众源/中集）：'劳务公司' + '工号' + '姓名' + '出勤天数' + 日级符号化
    if '劳务公司' in head and '工号' in head and '姓名' in head and '出勤天数' in head:
        # 业务月从 fallback_bm(mini_a_bill.bill_month)取
        year, month = None, None
        if fallback_bm:
            m_fb = _re.match(r'(\d{4})-(\d{1,2})', fallback_bm)
            if m_fb:
                year, month = int(m_fb.group(1)), int(m_fb.group(2))
        return parse_yuedu_hekedui(ws, year=year, month=month)
    # 长期工月度横向（澳思美）：含'长期工考勤' + '总工时'，或退化版（'姓名'+'总工时'）
    if '总工时' in head and ('长期工考勤' in head or
                            ('姓名' in head and '日期' not in head and '班次' not in head)):
        return parse_changqi_hengxiang(ws)
    # 顺丰结算/出勤数据：报名ID + 姓名 + 日期 + 结算时长 / 上班打卡时间
    if ('姓名' in head and '日期' in head
            and ('结算时长' in head or '上班打卡时间' in head)):
        return parse_shunfeng_jiesuan(ws)
    # 康丽达月度横向：R3 表头 / R4 'X日' 日期行
    if '考勤' in head and ('1日' in head or '2日' in head):
        return parse_yuedu_riziduan(ws, sheet_name=getattr(ws, 'title', None), year=2026)
    # 新广益月度横向：R2 含"YYYY年（M.D-M.D）" + R3 序号+姓名+所在部门 (班别可选，光伏/阳光浮体没有)
    if ('序号' in head and '姓名' in head and '所在部门' in head
            and '统计表' in head):
        return parse_yuedu_xinguangyi(ws)
    # 日结工：'名字'+'中介|派遣'
    if ('名字' in head or '姓名' in head) and ('中介' in head or '派遣' in head):
        return parse_rijiegong(ws)
    # 默认丽盈
    return parse_liying(ws)


# ============================================================
# 统一接口：process_sheet
# ============================================================
def process_sheet(cur, *, project_id, enterprise_id, business_cycle,
                  source_file_id, sheet_name, ws, fallback_bm=None,
                  precomputed_rows=None, format_id=None, collector=None):
    """precomputed_rows: 若给定（如 dispatcher 走 by-handler 路由后传入），
    跳过 ws 的 _parse 重新解析，直接用传入的 rows 写库。
    rows 形态约定与 _parse 输出一致 + 可含 is_valid/from_bill/invalid_reason/extra_data。
    format_id: format 模式下从命中 rule 透传，归属规则按 format_id 过滤。
    collector: 给定 AttendanceCollector 时，仅向 collector 追加 rows，**不写库**；
        由 dispatcher 在所有文件解析完毕后统一 flush（方案 B：跨文件 dedup + 批量写）。
        sheet 过滤、laowu 关键字、worker_class 推断仍在此处完成。
    """
    import json
    from etl._attribution import sheet_passes

    # sheet 层双重过滤：考账要先过 enterprise 再过 project
    if not sheet_passes(cur, project_id, 'kaoqin_bill', 'enterprise', sheet_name, format_id=format_id):
        return {'inserted': 0, 'skipped': 0, 'parsed': 0,
                'note': f'sheet {sheet_name!r} 不属于本企业（kaoqin_bill/enterprise sheet 规则未命中）'}
    if not sheet_passes(cur, project_id, 'kaoqin_bill', 'project', sheet_name, format_id=format_id):
        return {'inserted': 0, 'skipped': 0, 'parsed': 0,
                'note': f'sheet {sheet_name!r} 不属于本项目（kaoqin_bill/project sheet 规则未命中）'}

    # 防 force re-parse 重复：collector 模式下 wipe_for_pull 已清，跳过此处 DELETE
    if collector is None:
        cur.execute("""DELETE FROM attendance
                       WHERE project_id=%s AND source_file_id=%s
                         AND source_ref LIKE %s""",
                    (project_id, source_file_id, f'{sheet_name}%'))
        cur.execute("""DELETE FROM attendance_summary
                       WHERE project_id=%s AND source_file_id=%s
                         AND source_ref LIKE %s""",
                    (project_id, source_file_id, f'{sheet_name}%'))

    # 行级 enterprise 过滤：用 enabled=1 的 enterprise 列规则关键词喂给 _parse 内部按"劳务公司"列过滤
    cur.execute("""SELECT keywords FROM project_attribution_rules
                   WHERE project_id=%s AND category='kaoqin_bill'
                     AND scope='enterprise' AND rule_type='column' AND enabled=1""",
                (project_id,))
    laowu_keywords = []
    for (raw,) in cur.fetchall():
        try:
            kws = raw if isinstance(raw, list) else json.loads(raw or '[]')
            laowu_keywords.extend([k for k in kws if k])
        except (json.JSONDecodeError, TypeError):
            pass
    # TODO project 层列过滤暂未启用（大多数考账分流靠 sheet/enterprise 列就够）

    rows = (precomputed_rows
            if precomputed_rows is not None
            else _parse(ws, laowu_keywords=laowu_keywords, fallback_bm=fallback_bm))
    worker_class = _infer_worker_class(sheet_name)

    # ============================================================
    # collector 模式（方案 B）：仅向 collector 追加 rows，不写库
    # 跨文件 dedup + 批量 INSERT 由 collector.flush 统一完成
    # ============================================================
    if collector is not None:
        collector.add(project_id=project_id, enterprise_id=enterprise_id,
                       business_cycle=business_cycle,
                       source_file_id=source_file_id, sheet_name=sheet_name,
                       worker_class=worker_class, rows=rows)
        return {'collected': len(rows), 'parsed': len(rows),
                'worker_class': worker_class}

    n_ins = n_upd = n_skip = 0
    n_summary = 0

    # 批量建 worker 缓存（按 name_only 档，对应 attendance xlsx 没 id_card 的常见情况）
    from etl._utils import bulk_get_or_create_workers
    worker_cache = bulk_get_or_create_workers(
        cur, [r['name_raw'] for r in rows], project_id)

    # 五元组去重 (name_raw, shift_date, shift_name, hours, quantity)：
    # 覆盖累积窗口型考勤多版本场景（同 bill 多个时段快照都通过 dispatcher dedup_basename 入库）。
    # 同 project 已有同五元组（任何 source_file_id） → skip，本批次新增的也加进去防自重。
    cur.execute("""SELECT name_raw, shift_date, shift_name, hours, quantity
                   FROM attendance WHERE project_id=%s""", (project_id,))
    seen_quints = set()
    for n, sd, sn, h, q in cur.fetchall():
        try:
            seen_quints.add((n, sd, sn or '', float(h or 0), float(q or 0)))
        except (TypeError, ValueError):
            pass
    n_dup_quint = 0

    # 收集所有 INSERT 行，最后 executemany 批量装入（process_sheet 入口已 DELETE 同 source_file_id 旧行，无 dup 风险）
    att_batch = []
    sum_batch = []
    from datetime import date as _date
    for r in rows:
        wid = worker_cache.get(r['name_raw'].strip()) if r.get('name_raw') else None
        if not wid:
            n_skip += 1
            continue
        # 新字段（standard handler 输出会带；旧 _parse 输出没有 → 默认 1/0/None）
        is_valid = int(r.get('is_valid', 1))
        from_bill = int(r.get('from_bill', 0))
        invalid_reason = r.get('invalid_reason')
        extra_data = r.get('extra_data')
        extra_json = json.dumps(extra_data, ensure_ascii=False) if extra_data else None
        if r.get('is_summary'):
            y, m = int(r['bm'][:4]), int(r['bm'][5:7])
            ps, pe = derive_business_period(_date(y, m, 15), business_cycle)
            sum_batch.append((
                enterprise_id, project_id, wid, r['bm'], ps, pe,
                r['hours'], r['quantity'],
                r['worker_type'], worker_class, r['floor_or_group'],
                'attendance_xlsx_summary', source_file_id,
                f'{sheet_name}#R{r["row_idx"]}',
                r['name_raw'], None,
                from_bill, is_valid, invalid_reason, extra_json,
            ))
            n_summary += 1
        else:
            bm = derive_business_month(r['shift_date'], business_cycle)
            if bm is None:
                n_skip += 1
                continue
            quint_key = (r['name_raw'], r['shift_date'], r.get('shift_name') or '',
                         float(r.get('hours') or 0), float(r.get('quantity') or 0))
            if quint_key in seen_quints:
                n_dup_quint += 1
                continue
            seen_quints.add(quint_key)
            ps, pe = derive_business_period(r['shift_date'], business_cycle)
            att_batch.append((
                enterprise_id, project_id, wid, bm, ps, pe,
                r['shift_date'], r.get('shift_name'),
                r.get('worker_type'), worker_class, r.get('floor_or_group'),
                r['hours'], r.get('quantity'),
                'attendance_xlsx', source_file_id,
                f'{sheet_name}#R{r["row_idx"]}',
                r['name_raw'], r.get('id_card_raw'),
                from_bill, is_valid, invalid_reason, extra_json,
            ))
            n_ins += 1

    if att_batch:
        cur.executemany("""INSERT INTO attendance
            (etl_batch_id, ingested_at, enterprise_id, project_id, worker_id,
             business_month, business_period_start, business_period_end,
             shift_date, shift_name, worker_type, worker_class, floor_or_group,
             hours, quantity,
             source_type, source_file_id, source_ref, name_raw, id_card_raw,
             from_bill, is_valid, invalid_reason, extra_data)
            VALUES (0, NOW(), %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            att_batch)
    if sum_batch:
        cur.executemany("""INSERT INTO attendance_summary
            (etl_batch_id, ingested_at, enterprise_id, project_id, worker_id,
             business_month, business_period_start, business_period_end,
             hours, quantity, worker_type, worker_class, floor_or_group,
             source_type, source_file_id, source_ref, name_raw, id_card_raw,
             from_bill, is_valid, invalid_reason, extra_data)
            VALUES (0, NOW(), %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            sum_batch)
    return {'inserted': n_ins, 'updated': n_upd, 'skipped': n_skip,
            'dup_quint': n_dup_quint,
            'parsed': len(rows), 'summary_rows': n_summary,
            'worker_class': worker_class}
