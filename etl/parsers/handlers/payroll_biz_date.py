"""发薪流水业务日期判定引擎（接 project_payroll_biz_date_rules）

resolve_business_date(pay_time, extra_data, bd_rules) -> date
  bd_rules: {extract: [...], infer: [...]} (来自 list_payroll_biz_date_rules)
  优先 extract，失败 infer，都失败 fallback pay_time.date()

date 抽取支持：'M月D日' / 'YYYY年M月D日' / 'YYYY.M.D' / 'YYYY-M-D' / 'YYYY/M/D' / 'YYYYMMDD'
"""
import re
import json
from datetime import date, datetime, timedelta


_DATE_PATTERNS = [
    # YYYY年M月D日
    (re.compile(r'(20\d{2})年(\d{1,2})月(\d{1,2})日'), 'ymd'),
    # YYYY.M.D / YYYY-M-D / YYYY/M/D
    (re.compile(r'(20\d{2})[.\-/](\d{1,2})[.\-/](\d{1,2})\b'), 'ymd'),
    # YYYYMMDD
    (re.compile(r'\b(20\d{2})(\d{2})(\d{2})\b'), 'ymd'),
    # M月D日（无年份，年用 pay_time）
    (re.compile(r'(\d{1,2})月(\d{1,2})日'), 'md'),
    # YYYY年M月 / YYYY-MM / YYYY/MM / YYYY.MM (无 day, 用月初 day=1)
    (re.compile(r'(20\d{2})年(\d{1,2})月(?!\d|日)'), 'ym'),
    (re.compile(r'\b(20\d{2})[.\-/](\d{1,2})(?![\d.\-/])'), 'ym'),
    # 仅 X月（如摘要"鸿富4月"），年用 pay_time
    (re.compile(r'(\d{1,2})月'), 'm_only'),
]


def _try_extract_date(s, pay_time):
    """从字符串抽日期。pay_time 用作年份兜底（M月D日 / X月 / YYYY-MM 用月初日）。"""
    if not s:
        return None
    s = str(s)
    for pat, kind in _DATE_PATTERNS:
        m = pat.search(s)
        if not m:
            continue
        try:
            if kind == 'ymd':
                return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            elif kind == 'md':
                return date(pay_time.year, int(m.group(1)), int(m.group(2)))
            elif kind == 'ym':
                return date(int(m.group(1)), int(m.group(2)), 1)
            elif kind == 'm_only':
                return date(pay_time.year, int(m.group(1)), 1)
        except ValueError:
            continue
    return None


def _file_columns_match(file_columns_str, headers):
    """检查文件特征列是否在 headers 中。空字符串=总是生效。

    file_columns: 空格分隔列名，'#$%' 转义空格。匹配=列名是 headers cell 的子串，按顺序连续相邻。
    """
    if not file_columns_str or not file_columns_str.strip():
        return True
    parts = [s.replace('#$%', ' ').replace('!!!', ' ') for s in file_columns_str.split() if s.strip()]
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


def load_bd_rules(cur, project_id, format_id=None):
    """读 project_payroll_biz_date_rules → {extract: [...], infer: [...], bill_month: [...]}（按 priority 升序，仅 enabled=1）

    format_id：dispatcher 命中 rule 透传的 format，按 format_id 过滤（NULL 视为对所有 format 生效）。
    """
    if format_id is not None:
        cur.execute("""SELECT id, rule_kind, priority, file_columns, target_columns,
                              offset_n, offset_unit, enabled, note
                       FROM project_payroll_biz_date_rules
                       WHERE project_id=%s AND enabled=1
                         AND (format_id IS NULL OR format_id=%s)
                       ORDER BY rule_kind, priority""", (project_id, int(format_id)))
    else:
        cur.execute("""SELECT id, rule_kind, priority, file_columns, target_columns,
                              offset_n, offset_unit, enabled, note
                       FROM project_payroll_biz_date_rules
                       WHERE project_id=%s AND enabled=1
                       ORDER BY rule_kind, priority""", (project_id,))
    out = {'extract': [], 'infer': [], 'bill_month': []}
    for r in cur.fetchall():
        item = {
            'id': r[0], 'rule_kind': r[1], 'priority': r[2],
            'file_columns': r[3] or '',
            'target_columns': r[4] or '',
            'offset_n': int(r[5] or 0),
            'offset_unit': r[6] or 'day',
            'note': r[8] or '',
        }
        if r[1] in out:
            out[r[1]].append(item)
    return out


def resolve_business_date(pay_time, extra_data, bd_rules, headers=None, bill_month=None):
    """返回业务日 date。

    extra_data: dict (key=列名, value=单元格值)
    headers: 表头列表，用于 file_columns 匹配（standard 路径已丢失，传 None 视为总是匹配）
    bill_month: 'YYYY-MM' 字符串，bill_month 规则启用时使用（达达类无时间字段项目）
    """
    if pay_time is None:
        return None
    extra = extra_data or {}

    # ① extract: 优先
    for rule in bd_rules.get('extract', []):
        if headers is not None and not _file_columns_match(rule['file_columns'], headers):
            continue
        for col in rule['target_columns'].split('|'):
            col = col.strip()
            if not col:
                continue
            v = extra.get(col)
            if v is None:
                continue
            d = _try_extract_date(v, pay_time)
            if d:
                return d

    # ② infer: 兜底
    for rule in bd_rules.get('infer', []):
        if headers is not None and not _file_columns_match(rule['file_columns'], headers):
            continue
        # 推断列从 extra_data 取，否则用 pay_time 本身
        target = None
        for col in rule['target_columns'].split('|'):
            col = col.strip()
            if not col:
                continue
            v = extra.get(col)
            if isinstance(v, datetime):
                target = v; break
            if isinstance(v, date):
                target = datetime.combine(v, datetime.min.time()); break
            if isinstance(v, str):
                # 尝试 parse
                for fmt in ('%Y-%m-%d %H:%M:%S', '%Y/%m/%d %H:%M:%S', '%Y-%m-%d', '%Y/%m/%d'):
                    try:
                        target = datetime.strptime(v.strip(), fmt); break
                    except ValueError:
                        continue
                if target: break
        if target is None:
            target = pay_time

        # 偏移
        n = rule['offset_n']
        unit = rule['offset_unit']
        if unit == 'day':
            return (target - timedelta(days=n)).date()
        elif unit == 'month':
            y, m = target.year, target.month - n
            while m <= 0:
                y -= 1; m += 12
            try:
                return target.replace(year=y, month=m).date()
            except ValueError:
                # 月底跨月（如 5/31 - 1月 → 4/31 不存在），用月末兜底
                from calendar import monthrange
                last = monthrange(y, m)[1]
                return target.replace(year=y, month=m, day=min(target.day, last)).date()

    # ③ bill_month: 用 raw_files 关联的账单月份首日（达达类无时间字段项目）
    if bd_rules.get('bill_month') and bill_month:
        try:
            y, m = bill_month.split('-')
            return date(int(y), int(m), 1)
        except (ValueError, AttributeError):
            pass

    # ④ 严格模式 vs 兜底：
    #   - 配了 extract 或 infer 规则 → 严格模式（用户期望按规则抽业务月）
    #     都失败 = 业务月不明 → 返回 None 让上层 skip 此行
    #   - 没配任何规则 → fallback pay_time 本身（兼容老项目无规则配置）
    has_strict_rules = bool(bd_rules.get('extract') or bd_rules.get('infer'))
    if has_strict_rules:
        return None
    return pay_time.date() if hasattr(pay_time, 'date') else pay_time
