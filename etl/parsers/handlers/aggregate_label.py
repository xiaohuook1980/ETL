"""aggregate_label handler: 二维标签定位 / Excel cell 坐标定位金额

场景：账单文件总金额来自固定位置的汇总值（如汇总表底"含税合计 × 应付合计列 = ¥839,153.37"），
      而非按人累加。常见于甲方→乙方的费用结算单。

接口：
    scan_aggregate_labels(ws, sheet_name, rules) -> list[hit]
        ws         openpyxl worksheet (单 sheet)
        sheet_name 当前 sheet 名 (用于 sheet_pattern 匹配)
        rules      list[{sheet_pattern, label, col_name, cell_ref, ...}]  只传 enabled 规则
        返回       list[{sheet, label, col_name, amount, row, col, mode}]

定位算法（每条规则二选一，cell_ref 优先）：
    A. 坐标定位（cell_ref 非空）：
        cell_ref 如 "N3" → row=3, col=N(14) → 直接取 ws.cell(row, col)
    B. 标签定位（cell_ref 空 + label/col_name 都非空）：
        1. 限定到 sheet：sheet_pattern 子串匹配 sheet 名（空=所有 sheet）
        2. 找含 col_name 的表头 cell（子串匹配） → 确定 col_idx
        3. 找 cell 内容 strip 后等于 label 的行（精确匹配，避免"含税合计"误中"不含税合计"）
        4. 取 (row_idx, col_idx) 数字；非数字跳过

多命中行为（仅标签定位）：
    - 一条规则可能命中多行 → 全部收集
    - 上层 sum(hits.amount) → bill_totals.amount
"""
import re


_CELL_REF_RE = re.compile(r'^\s*([A-Za-z]+)\s*(\d+)\s*$')


def _parse_cell_ref(s):
    """'N3' -> (row=3, col=14, 1-based);  非法 -> None"""
    if not s:
        return None
    m = _CELL_REF_RE.match(str(s))
    if not m:
        return None
    letters, digits = m.group(1).upper(), m.group(2)
    col = 0
    for ch in letters:
        col = col * 26 + (ord(ch) - ord('A') + 1)
    return int(digits), col


def _to_number(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace(',', '').replace('¥', '').replace('￥', '').replace(' ', '')
    if not s or s in ('-', '--'):
        return None
    try:
        return float(s)
    except (ValueError, TypeError):
        return None


def _sheet_matches(sheet_name, pattern):
    """sheet_pattern 空 = 通配；否则子串匹配"""
    if not pattern or not pattern.strip():
        return True
    return pattern.strip() in str(sheet_name)


def _find_col_idx(all_rows, col_name):
    """找含 col_name 的 cell（子串匹配），返回 col_idx；未找到返回 None。"""
    if not col_name:
        return None
    col_name = col_name.strip()
    for row in all_rows:
        if not row:
            continue
        for c_idx, v in enumerate(row):
            if v is None:
                continue
            if col_name in str(v).strip():
                return c_idx
    return None


def scan_aggregate_labels(ws, sheet_name, rules):
    """扫单 sheet 找规则命中。返回 list[hit]"""
    hits = []
    if not rules:
        return hits
    all_rows = None  # lazy load
    for rule in rules:
        if not _sheet_matches(sheet_name, rule.get('sheet_pattern')):
            continue

        # A. 坐标定位优先
        cell_ref = (rule.get('cell_ref') or '').strip()
        if cell_ref:
            rc = _parse_cell_ref(cell_ref)
            if rc is None:
                continue
            r, c = rc
            try:
                amt = _to_number(ws.cell(row=r, column=c).value)
            except Exception:
                amt = None
            if amt is None:
                continue
            hits.append({
                'sheet': sheet_name,
                'label': '',
                'col_name': '',
                'cell_ref': cell_ref.upper(),
                'amount': amt,
                'row': r,
                'col': c,
                'mode': 'cell_ref',
            })
            continue

        # B. 标签定位
        label = (rule.get('label') or '').strip()
        col_name = (rule.get('col_name') or '').strip()
        if not label or not col_name:
            continue
        if all_rows is None:
            all_rows = list(ws.iter_rows(values_only=True))
        col_idx = _find_col_idx(all_rows, col_name)
        if col_idx is None:
            continue
        for r_idx, row in enumerate(all_rows):
            if not row:
                continue
            has_label = False
            for v in row:
                if v is None:
                    continue
                if str(v).strip() == label:
                    has_label = True
                    break
            if not has_label:
                continue
            if col_idx >= len(row):
                continue
            amt = _to_number(row[col_idx])
            if amt is None:
                continue
            hits.append({
                'sheet': sheet_name,
                'label': label,
                'col_name': col_name,
                'amount': amt,
                'row': r_idx + 1,
                'col': col_idx + 1,
                'mode': 'label',
            })
    return hits
