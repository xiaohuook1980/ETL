"""calc 层 validity 实时过滤工具

设计：validity 是分析层概念（"这条事实算什么用途"），不该物化在 mart。
mart 层 is_valid/count_as_faxin 字段保留但 calc 不读——calc 取数时同时取
extra_data，调 apply_validity 内存判定。改 validity 规则即时生效，无需重 parse。

接口：
    apply_calc_validity(cur, project_id, kind, rows) -> rows (in-place 标记)
        - 加载该项目本 kind 的 column_mapping（取首条 enabled classify rule）
        - 调 apply_validity 给每行打 is_valid / count_as_faxin / invalid_reason
        - 返回同一 rows，字段已就地写入
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from etl.parsers.handlers.validity import apply_validity


def _load_column_mapping(cur, project_id, kind):
    """取本项目本 kind 启用的 classify rule 的 column_mapping。
    多条按 priority 排序取首条（同 kind 通常共享一份列映射）。
    无规则 → 返回 None（apply_validity 仍能跑，只是用户列名反查不到 mart 字段）。
    """
    cur.execute("""SELECT column_mapping FROM project_classify_rules
                   WHERE project_id=%s AND target_kind=%s AND enabled=1
                   ORDER BY priority LIMIT 1""", (project_id, kind))
    row = cur.fetchone()
    if not row or not row[0]:
        return None
    cm = row[0]
    if isinstance(cm, str):
        try:
            cm = json.loads(cm)
        except (ValueError, TypeError):
            return None
    return cm


def apply_calc_validity(cur, project_id, kind, rows):
    """对 rows 应用项目 validity 规则，就地写 is_valid + count_as_faxin + invalid_reason。

    rows: list[dict]，必须有 extra_data 字段（dict 或 None）+ 其他 mart 字段。
    返回同一 rows（标记后）。

    DB 流 / xlsx 流统一走 project_validity_rules——用户在 UI 配的规则对两类来源
    都生效。无规则项目所有行默认 is_valid=1 / count_as_faxin=1（apply_validity
    内部 setdefault）。
    """
    if not rows:
        return rows
    cm = _load_column_mapping(cur, project_id, kind)
    apply_validity(rows, kind=kind, project_id=project_id,
                   conn=cur.connection, column_mapping=cm)
    return rows


def parse_extra_data(v):
    """mart 取出来的 extra_data 列：可能是 JSON 字符串 / dict / None"""
    if v is None or v == '':
        return None
    if isinstance(v, dict):
        return v
    if isinstance(v, str):
        try:
            return json.loads(v)
        except (ValueError, TypeError):
            return None
    return None


def fetch_attendance_rows(cur, project_id, *, where_extra='', where_args=(),
                          extra_select_cols=()):
    """取 attendance 行（calc-time validity 用）。
    - where_extra: 拼到 WHERE 后面的额外条件（已有 project_id 过滤；含 attendance_filters WHERE）
    - where_args: where_extra 用的参数
    - extra_select_cols: 想多取的列名 list（如 ['worker_id']）
    - 自动跑 apply_calc_validity (kind='attendance')，返回 list[dict]，含 is_valid 字段
    """
    base_cols = ['shift_date', 'shift_name', 'hours', 'quantity', 'name_raw',
                 'worker_type', 'worker_class', 'floor_or_group', 'extra_data']
    cols = base_cols + list(extra_select_cols)
    sql = f"SELECT {', '.join(cols)} FROM attendance WHERE project_id=%s {where_extra}"
    cur.execute(sql, [project_id] + list(where_args))
    rows = []
    for r in cur.fetchall():
        d = {col: r[i] for i, col in enumerate(cols)}
        d['extra_data'] = parse_extra_data(d['extra_data'])
        rows.append(d)
    return apply_calc_validity(cur, project_id, 'attendance', rows)


def fetch_unit_prices(cur, project_id, ref_date=None):
    """取项目单价配置（v2：project_price_config + project_price_rules）。
    返回 {'config': {dim1/2/3_col_name}, 'rules': list[rule]}。
    rules 已按 priority ASC 排序；时段过滤（ref_date 给定时）"""
    # config
    cur.execute("""SELECT dim1_col_name, dim2_col_name, dim3_col_name
                   FROM project_price_config WHERE project_id=%s""", (project_id,))
    cfg_row = cur.fetchone()
    config = {
        'dim1_col_name': (cfg_row[0] if cfg_row else '') or '',
        'dim2_col_name': (cfg_row[1] if cfg_row else '') or '',
        'dim3_col_name': (cfg_row[2] if cfg_row else '') or '',
    }
    # rules
    if ref_date:
        cur.execute("""SELECT id, dim1_keywords, dim2_keywords, dim3_keywords,
                              price, unit, effective_start, effective_end, priority
                       FROM project_price_rules
                       WHERE project_id=%s
                         AND (effective_start IS NULL OR effective_start <= %s)
                         AND (effective_end IS NULL OR effective_end >= %s)
                       ORDER BY priority ASC, id ASC""",
                    (project_id, ref_date, ref_date))
    else:
        cur.execute("""SELECT id, dim1_keywords, dim2_keywords, dim3_keywords,
                              price, unit, effective_start, effective_end, priority
                       FROM project_price_rules WHERE project_id=%s
                       ORDER BY priority ASC, id ASC""", (project_id,))
    rules = []
    for r in cur.fetchall():
        rules.append({
            'id': r[0],
            'dim1_keywords': r[1] or '',
            'dim2_keywords': r[2] or '',
            'dim3_keywords': r[3] or '',
            'price': float(r[4] or 0),
            'unit': r[5] or '',
            'effective_start': r[6],
            'effective_end': r[7],
            'priority': r[8],
        })
    return {'config': config, 'rules': rules}


# attendance 行 mart 字段到中文列名的反查（用户在 config 里填的列名一般是中文）
_MART_FIELD_CN_MAP = {
    '场地': 'floor_or_group',  # 老 area 兼容
    '楼层': 'floor_or_group',
    '部门': 'floor_or_group',
    '工种': 'worker_type',
    '岗位': 'worker_type',
    '班次': 'shift_name',
    '班别': 'shift_name',
    '工人级别': 'worker_class',
    '级别': 'worker_class',
}


def _extract_dim_value(row, col_name):
    """从 attendance row 提取某维度的值（col_name = 用户配的列名）。
    优先 _MART_FIELD_CN_MAP 反查 mart 字段；fallback 看 extra_data 里的 key"""
    if not col_name:
        return ''
    # 1. mart 标准字段（中文名反查）
    mart_field = _MART_FIELD_CN_MAP.get(col_name)
    if mart_field and row.get(mart_field):
        return str(row[mart_field])
    # 2. mart 直接含该 key（如已被升级到 floor_or_group 等）
    if row.get(col_name):
        return str(row[col_name])
    # 3. extra_data dict
    ed = row.get('extra_data')
    if isinstance(ed, dict):
        v = ed.get(col_name)
        if v:
            return str(v)
    return ''


def _keywords_match(value, keywords):
    """关键字逗号分隔 OR（兼容中文/英文逗号）；空 keywords = 通配（任意值都匹）;
    任一关键字是 value 的子串 → 命中"""
    if not keywords or keywords.strip() in ('', '*'):
        return True
    if not value:
        return False
    import re as _re
    parts = [kw.strip() for kw in _re.split(r'[,,]', keywords) if kw.strip()]
    if not parts:
        return True
    return any(kw in value for kw in parts)


def _rule_specificity(rule):
    """具体度 = 非通配字段数（0-3）"""
    s = 0
    for k in ('dim1_keywords', 'dim2_keywords', 'dim3_keywords'):
        v = (rule.get(k) or '').strip()
        if v and v != '*':
            s += 1
    return s


def match_unit_price_full(unit_prices_pack, row):
    """同 match_unit_price，但返回 (price, unit, rule_dict) — 含命中规则用于分组统计。
    无匹中 → (None, None, None)"""
    if isinstance(row, str):
        row = {'shift_name': row, 'extra_data': None}
    if not unit_prices_pack:
        return None, None, None
    cfg = unit_prices_pack.get('config') or {}
    rules = unit_prices_pack.get('rules') or []
    d1c, d2c, d3c = cfg.get('dim1_col_name', ''), cfg.get('dim2_col_name', ''), cfg.get('dim3_col_name', '')
    v1 = _extract_dim_value(row, d1c) if d1c else ''
    v2 = _extract_dim_value(row, d2c) if d2c else ''
    v3 = _extract_dim_value(row, d3c) if d3c else ''

    hits = []
    for rule in rules:
        ok1 = _keywords_match(v1, rule['dim1_keywords']) if d1c else True
        ok2 = _keywords_match(v2, rule['dim2_keywords']) if d2c else True
        ok3 = _keywords_match(v3, rule['dim3_keywords']) if d3c else True
        if ok1 and ok2 and ok3:
            hits.append((_rule_specificity(rule), rule['priority'], rule['id'], rule))
    if not hits:
        return None, None, None
    hits.sort(key=lambda x: (-x[0], x[1], x[2]))
    best = hits[0][3]
    return best['price'], best['unit'], best


def match_unit_price(unit_prices_pack, row):
    """按新 v2 模型匹中单价。返回 (price, unit) — 老接口兼容。"""
    p, u, _ = match_unit_price_full(unit_prices_pack, row)
    return p, u


def row_amount(row, unit=None):
    """直接拿 attendance 行的工时或件数作为乘数。
    hours 优先（> 0 时用 hours）；否则 fallback quantity。
    unit 参数保留兼容老调用方但不再使用（单价直接当数字乘）。"""
    h = float(row.get('hours') or 0)
    if h > 0:
        return h
    return float(row.get('quantity') or 0)
