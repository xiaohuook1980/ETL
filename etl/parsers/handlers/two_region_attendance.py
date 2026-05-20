"""两区域横向考勤 handler

格式：单个 sheet 含两组并列的考勤明细，左/右半镜像（如左白班 + 右夜班）。
表头行包含 2 组同名列：[序号|日期|班次|姓名|工时] + 中间空列 + [序号|日期|班次|姓名|工时]

输入：openpyxl worksheet + column_mapping（同 standard，标准 4 字段映射）
输出：list[dict] 兼容 mart_attendance（左半记录 + 右半记录合并）

设计：跟 standard handler 接口一致（kind='attendance' 唯一支持），dispatcher 走
specialized 路由调进来；不动 standard handler。
"""
import re
from etl._utils import parse_excel_date, normalize_shift, safe_float


NAME_BLACKLIST = {'合计', '小计', '总计', '汇总', '累计', '姓名', '名字', '户名', '收款人'}


def _all_col_indices(headers, pattern):
    """返回 headers 里所有能命中 pattern 的列索引（0-based）。
    pattern 含 ',' → 任一候选命中即算；不区分大小写子串匹配。"""
    if not isinstance(pattern, str):
        return []
    cands = [c.strip() for c in pattern.split(',') if c.strip()]
    out = []
    for i, h in enumerate(headers):
        if h is None:
            continue
        cell = str(h).strip()
        if not cell:
            continue
        for cand in cands:
            if '*' in cand:
                regex = re.escape(cand).replace(r'\*', '.')
                if re.search(regex, cell):
                    out.append(i)
                    break
            else:
                if cand in cell:
                    out.append(i)
                    break
    return out


def _locate_header_multi(ws, column_mapping, max_scan=8):
    """扫前 max_scan 行找表头行：name_raw 候选列在该行出现 ≥2 次。
    返回 (header_row_idx_1based, headers_list_0based, regions)
    regions: list[dict{mart_field: col_idx_0based}]，每个 dict 是一组区域映射"""
    required = ['name_raw', 'shift_date']  # 必填字段（hours/quantity 装入时再校验）
    rows = list(ws.iter_rows(max_row=max_scan, values_only=True))
    for ridx, row in enumerate(rows, start=1):
        if not row:
            continue
        headers = list(row)
        # 收集每个字段的所有命中列
        field_cols = {}
        for mart_field, col_pat in column_mapping.items():
            if not col_pat or mart_field == 'extra_data':
                continue
            field_cols[mart_field] = _all_col_indices(headers, col_pat)
        # 必填字段必须 ≥2 次出现才认作"双区域表头"
        name_cols = field_cols.get('name_raw') or []
        date_cols = field_cols.get('shift_date') or []
        if len(name_cols) < 2 or len(date_cols) < 2:
            continue
        # 按 name_raw 列数定区域数
        n_regions = len(name_cols)
        regions = []
        for ri in range(n_regions):
            region = {'name_raw': name_cols[ri]}
            anchor = name_cols[ri]
            for f, cs in field_cols.items():
                if f == 'name_raw' or not cs:
                    continue
                # 找跟 anchor 距离最近的列（跨区域时离最近的就是本区域的）
                nearest = min(cs, key=lambda c: abs(c - anchor))
                region[f] = nearest
            regions.append(region)
        # 所有 region 的必填字段都齐才有效
        if all(all(f in r for f in required) for r in regions):
            return ridx, headers, regions
    return None, None, []


def _cell(row, idx):
    if idx is None or idx >= len(row):
        return None
    return row[idx]


def _str_or_none(v):
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def parse(ws, *, column_mapping, sheet_name=None, business_month=None, fallback_bm=None, **kwargs):
    """主入口：跟 standard.parse(kind='attendance') 输出格式一致。
    双区域识别失败（单区域 sheet）→ 退化调 standard.parse 走单区域。"""
    header_row_idx, headers, regions = _locate_header_multi(ws, column_mapping)
    if not regions:
        # fallback: 单区域 sheet（同文件混合时，比如 4.30 只有左半 5 列）
        from etl.parsers.handlers.standard import parse as std_parse
        return std_parse(ws, kind='attendance', column_mapping=column_mapping,
                         sheet_name=sheet_name, business_month=business_month,
                         fallback_bm=fallback_bm)

    rows = []
    for ridx, row in enumerate(ws.iter_rows(min_row=header_row_idx + 1, values_only=True),
                                start=header_row_idx + 1):
        if not row or all(v is None for v in row):
            continue
        # 每行展开 N 个区域 → N 条记录
        for reg_i, reg in enumerate(regions):
            ni = reg.get('name_raw')
            nm = _str_or_none(_cell(row, ni))
            if not nm or nm in NAME_BLACKLIST:
                continue
            di = reg.get('shift_date')
            d = parse_excel_date(_cell(row, di))
            if not d:
                continue
            hi = reg.get('hours')
            qi = reg.get('quantity')
            h = safe_float(_cell(row, hi)) if hi is not None else None
            q = safe_float(_cell(row, qi)) if qi is not None else None
            if (h is None or h <= 0) and (q is None or q <= 0):
                continue
            si = reg.get('shift_name')
            fi = reg.get('floor_or_group')
            wti = reg.get('worker_type')
            wci = reg.get('worker_class')
            ici = reg.get('id_card_raw')
            rows.append({
                'row_idx': f'{ridx}R{reg_i + 1}',  # 一行展开多记录 → 后缀区分
                'shift_date': d,
                'name_raw': nm,
                'hours': h,
                'quantity': q,
                'shift_name': normalize_shift(_cell(row, si)) if si is not None else None,
                'floor_or_group': _str_or_none(_cell(row, fi)) if fi is not None else None,
                'worker_type': _str_or_none(_cell(row, wti)) if wti is not None else None,
                'worker_class': _str_or_none(_cell(row, wci)) if wci is not None else None,
                'id_card_raw': _str_or_none(_cell(row, ici)) if ici is not None else None,
                'extra_data': None,
            })
    return rows
