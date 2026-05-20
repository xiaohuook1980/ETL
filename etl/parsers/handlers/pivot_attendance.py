"""横向 pivot 考勤 handler

输入：openpyxl worksheet + pivot 模板配置（scan_row_start/end, min_consecutive_digits, static_column_mapping）
输出：list[dict] (shift_date, name_raw, hours, floor_or_group, shift_name, extra_data, ...)

核心算法：
1. 扫前 N 行找"≥M 个连续数字 cell"那行 = 日期标号行
2. 该行上一行 = 静态字段表头行
3. 上方 1-3 行 + sheet 名 → 解析周期起始
4. 按 row 数字序列 + 起始日 → 生成具体日期列表（数字递减 → 跨月）
5. unpivot 每数据行 × 每日期列 → 一条 attendance 记录

不识别 quantity（计件）—— 计件项目按 unit_prices.unit 单位 calc 阶段动态决定。
"""
import re
from datetime import date


NAME_BLACKLIST = {'合计', '小计', '总计', '汇总', '累计', '姓名', '名字', '户名', '收款人'}


def _to_date_num(v):
    """26 / '26' / 26.0 / '26.0' / '26日' / '26号' / '2026年04月21日' /
    datetime(2026,4,1) → 1-31 int 或 None
    支持 datetime/date 对象（威玛斯这种 r3 整列 datetime 的横向考勤）。"""
    if v is None:
        return None
    from datetime import datetime as _dt, date as _dt_date
    # datetime / date 对象：直接取 day
    if isinstance(v, (_dt, _dt_date)):
        return v.day
    s = str(v).strip()
    if not s:
        return None
    # 完整日期 "YYYY年MM月DD日" → 取 day（京东每日期占 2 列模板）
    m = re.match(r'^\d{4}\s*年\s*\d{1,2}\s*月\s*(\d{1,2})\s*日?$', s)
    if m:
        n = int(m.group(1))
        return n if 1 <= n <= 31 else None
    if s.endswith('日') or s.endswith('号'):
        s = s[:-1].strip()
    try:
        n = int(float(s))
        return n if 1 <= n <= 31 else None
    except (ValueError, TypeError):
        return None


def find_date_row(ws, scan_row_start=1, scan_row_end=10, min_consecutive=10):
    """找日期标号行。返回 (date_row_idx, list_of_(col_idx, day_num)) 或 (None, [])

    容忍每个日期 cell 后 1 个 None 占位（"每日期占 2 列"模板：工时 + 夜班类型），
    连续 ≥ 2 个 None 才算断点。
    """
    max_row = min(scan_row_end, ws.max_row or 0)
    for ridx in range(scan_row_start, max_row + 1):
        cells = [ws.cell(ridx, c).value for c in range(1, (ws.max_column or 0) + 1)]
        runs = []
        run_days = []
        gap = 0
        for ci, v in enumerate(cells, start=1):
            n = _to_date_num(v)
            if n is not None:
                run_days.append((ci, n))
                gap = 0
            else:
                gap += 1
                if gap >= 2:
                    if len(run_days) >= min_consecutive:
                        runs.append(list(run_days))
                    run_days = []
                    gap = 0
        if len(run_days) >= min_consecutive:
            runs.append(list(run_days))
        if runs:
            longest = max(runs, key=len)
            return ridx, longest
    return None, []


def _find_col_idx(headers, pattern):
    """headers 是 1-based 列名 list；pattern 含逗号则取第一个找到的候选。
    匹配优先级：完全相等（strip 后）> 子串匹配。
    避免"姓名"误匹中"姓名主管"等场景。"""
    if not pattern:
        return None
    candidates = [c.strip() for c in re.split(r'[,，]', str(pattern)) if c.strip()]
    # Pass 1: 完全相等
    for cand in candidates:
        for i, h in enumerate(headers, start=1):
            if h is None:
                continue
            if str(h).strip() == cand:
                return i
    # Pass 2: 子串匹配（fallback）
    for cand in candidates:
        for i, h in enumerate(headers, start=1):
            if h is None:
                continue
            if cand in str(h):
                return i
    return None


def _infer_year(ws, date_row_idx):
    """从前几行扫 '20YY年'；找不到 → 当前年"""
    from datetime import date as _date
    for r in range(1, min(date_row_idx, 5) + 1):
        for c in range(1, (ws.max_column or 0) + 1):
            v = ws.cell(r, c).value
            if v:
                m = re.search(r'(20\d{2})\s*年', str(v))
                if m:
                    return int(m.group(1))
    return _date.today().year


def resolve_start_date(ws, date_row_idx, date_cells, sheet_name=None):
    """扫日期行上方 1-3 行 + sheet 名 → 起始 date

    优先：date_row 第一个 cell 本身是 datetime/date → 直接取该 cell 的年月日（法雷奥场景）
    fallback：regex 扫文本"""
    from datetime import datetime as _dt, date as _dt_date, timedelta as _td
    if not date_cells:
        return None
    first_day = date_cells[0][1]
    # 优先 0：date_row 的 cell 本身是 datetime / "YYYY年MM月DD日" 字符串
    first_col_idx = date_cells[0][0]
    first_cell = ws.cell(date_row_idx, first_col_idx).value
    if isinstance(first_cell, (_dt, _dt_date)):
        return first_cell.date() if isinstance(first_cell, _dt) else first_cell
    if first_cell:
        m0 = re.match(r'^(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日?$',
                       str(first_cell).strip())
        if m0:
            try:
                return date(int(m0.group(1)), int(m0.group(2)), int(m0.group(3)))
            except ValueError:
                pass

    # 收集上方 1-3 行所有文本 + sheet 名
    text = ''
    for r in range(max(1, date_row_idx - 3), date_row_idx):
        for c in range(1, (ws.max_column or 0) + 1):
            v = ws.cell(r, c).value
            if v:
                text += ' ' + str(v).strip()
    text += ' ' + (sheet_name or '')

    # P1: 完整周期 "M.D-M.D" / "YYYY.M.D-YYYY.M.D"
    m = re.search(r'(?:(\d{4})\s*年?\s*[（(]?\s*)?(\d{1,2})\.(\d{1,2})\s*[-~至]\s*(?:(\d{4})\.)?(\d{1,2})\.(\d{1,2})', text)
    if m:
        try:
            sy = int(m.group(1)) if m.group(1) else _infer_year(ws, date_row_idx)
            sm = int(m.group(2)); sd = int(m.group(3))
            em = int(m.group(5))
            # 跨年（如 12.26-1.25）：起始年 = sy-1（如果 row1 写 "2026年（12.26-1.25)" 实际起始 2025-12-26）
            if sm > em:
                sy -= 1
            return date(sy, sm, sd)
        except (ValueError, TypeError):
            pass

    # P2: 起始月+日 "2026年（4.26-" 截断
    m = re.search(r'(?:(\d{4})\s*年\s*[（(]?\s*)(\d{1,2})\.(\d{1,2})', text)
    if m:
        try:
            sy = int(m.group(1))
            sm = int(m.group(2)); sd = int(m.group(3))
            return date(sy, sm, sd)
        except (ValueError, TypeError):
            pass

    # P3: 仅月份"4 月考勤" / "2026年4月" → 自然月 → 起始 = 1
    m = re.search(r'(?:(\d{4})\s*年)?\s*(\d{1,2})\s*月', text)
    if m:
        try:
            sy = int(m.group(1)) if m.group(1) else _infer_year(ws, date_row_idx)
            sm = int(m.group(2))
            return date(sy, sm, first_day)  # 用 row4 第一个数字当起始日
        except (ValueError, TypeError):
            pass

    return None


def gen_dates(start, date_cells):
    """按 row 数字序列 + 起始日 生成 N 个 date。
    数字递减点 = 跨月。月份>12 跨年。
    """
    if not start or not date_cells:
        return []
    cur_y, cur_m = start.year, start.month
    out = []
    prev_day = None
    for ci, day in date_cells:
        if prev_day is not None and day < prev_day:
            cur_m += 1
            if cur_m > 12:
                cur_m = 1
                cur_y += 1
        try:
            out.append(date(cur_y, cur_m, day))
        except ValueError:
            out.append(None)  # 月份没这天（如 4-31）
        prev_day = day
    return out


def parse(ws, *, config, sheet_name=None):
    """主入口。返回 list[dict] 兼容 mart_attendance。"""
    scan_start = int(config.get('scan_row_start', 1))
    scan_end = int(config.get('scan_row_end', 10))
    min_digits = int(config.get('min_consecutive_digits', 10))

    date_row_idx, date_cells = find_date_row(ws, scan_start, scan_end, min_digits)
    if date_row_idx is None:
        return []

    static_map = config.get('static_column_mapping') or {}

    def _locate(headers):
        static_idx = {}
        extra_idxs = []
        for mart_field, col_pat in static_map.items():
            if not col_pat:
                continue
            if mart_field == 'extra_data':
                # 与"特征列"一致：空格分隔；逗号兼容老配置；!!! 还原真实空格
                for cand in re.split(r'[\s,，]+', str(col_pat)):
                    cand = cand.replace('!!!', ' ').strip()
                    if not cand:
                        continue
                    idx = _find_col_idx(headers, cand)
                    if idx is not None:
                        extra_idxs.append(idx)
            else:
                idx = _find_col_idx(headers, col_pat)
                if idx is not None:
                    static_idx[mart_field] = idx
        return static_idx, extra_idxs

    # 多布局 fallback：找字段表头行（含 name_raw）
    # 布局 1（标准）: field_row = date_row - 1（字段在日期上方）
    # 布局 2（混合）: field_row = date_row 同一行（雷悦：r1 合并标题，r2 = 字段+日期混排）
    # 布局 3（反序）: field_row = date_row + 1（法雷奥：r5 日期行，r6 字段，r7+ 数据）
    headers = None
    static_idx = {}
    extra_idxs = []
    field_row = None
    max_col = ws.max_column or 0

    # 布局 1
    if date_row_idx > 1:
        h1 = [ws.cell(date_row_idx - 1, c).value for c in range(1, max_col + 1)]
        static_idx, extra_idxs = _locate(h1)
        if 'name_raw' in static_idx:
            headers = h1
            field_row = date_row_idx - 1

    # 布局 2: 字段+日期混排同一行，屏蔽数字 cell
    if headers is None:
        h2_raw = [ws.cell(date_row_idx, c).value for c in range(1, max_col + 1)]
        h2 = [v if _to_date_num(v) is None else None for v in h2_raw]
        static_idx, extra_idxs = _locate(h2)
        if 'name_raw' in static_idx:
            headers = h2
            field_row = date_row_idx

    # 布局 3: 字段在日期下一行（法雷奥）
    if headers is None and date_row_idx < (ws.max_row or 0):
        h3 = [ws.cell(date_row_idx + 1, c).value for c in range(1, max_col + 1)]
        static_idx, extra_idxs = _locate(h3)
        if 'name_raw' in static_idx:
            headers = h3
            field_row = date_row_idx + 1

    if headers is None or 'name_raw' not in static_idx:
        return []

    start_date = resolve_start_date(ws, date_row_idx, date_cells, sheet_name)

    # sheet 没年月文本（仅纯日数字）时：用项目 business_cycle + business_month 自动推
    # config.business_cycle 例 '上月26-本月25'；config.business_month 例 '2026-05'
    if not start_date:
        bc = config.get('business_cycle')
        bm = config.get('business_month')
        if bc and bm:
            m_bc = re.match(r'上月(\d+)-本月(\d+)', bc)
            if m_bc:
                cycle_start_day = int(m_bc.group(1))
                try:
                    y, mn = int(bm[:4]), int(bm[5:7])
                    # 业务月 2026-05 + 上月26-本月25 → 起点 2026-04-26
                    sy, sm = (y, mn - 1) if mn > 1 else (y - 1, 12)
                    # 找 date_cells 中第一个 = cycle_start_day 的位置作为周期起点
                    for ci, day in date_cells:
                        if day == cycle_start_day:
                            start_date = date(sy, sm, cycle_start_day)
                            # 把前面的 cells 移除（属于上一个周期，不装入本次）
                            date_cells = [(c, d) for (c, d) in date_cells
                                          if not (c < ci)]
                            break
                except (ValueError, TypeError):
                    pass
    if not start_date:
        return []
    dates = gen_dates(start_date, date_cells)

    # 数据起始：max(date_row, field_row) + 1
    data_start = max(date_row_idx, field_row) + 1
    rows = []
    ni = static_idx['name_raw']
    fi = static_idx.get('floor_or_group')
    si = static_idx.get('shift_name')

    for ridx in range(data_start, (ws.max_row or 0) + 1):
        nm = ws.cell(ridx, ni).value
        if nm is None:
            continue
        nm = str(nm).strip()
        if not nm or nm in NAME_BLACKLIST:
            continue

        floor = ws.cell(ridx, fi).value if fi else None
        shift = ws.cell(ridx, si).value if si else None

        extra = {}
        for ei in extra_idxs:
            v = ws.cell(ridx, ei).value
            if v is not None and v != '':
                col_name = str(headers[ei - 1]).strip() if ei <= len(headers) and headers[ei - 1] else f'col{ei}'
                extra[col_name] = v

        for (col_idx, _day_num), bd in zip(date_cells, dates):
            if bd is None:
                continue
            v = ws.cell(ridx, col_idx).value
            if v is None or v == '':
                continue
            try:
                hours = float(v)
            except (ValueError, TypeError):
                continue
            if hours <= 0:
                continue
            rows.append({
                'row_idx': f'{ridx}C{col_idx}',  # 一数据行 unpivot 出多条 → 用 R{ridx}C{col_idx} 唯一化
                'shift_date': bd,
                'name_raw': nm,
                'hours': hours,
                'quantity': None,
                'shift_name': str(shift).strip() if shift else None,
                'floor_or_group': str(floor).strip() if floor else None,
                'worker_type': None,
                'worker_class': None,
                'id_card_raw': None,
                'is_valid': 1,
                'from_bill': 0,
                'invalid_reason': None,
                'extra_data': extra or None,
            })
    return rows
