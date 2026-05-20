"""sheet 分类引擎 v2 —— 项目级配置驱动。

接口：
    classify_sheet(ws, project_id=None, conn=None) -> dict
        返回 {'kind': str, 'rule_id': int|None, 'handler': str|None, 'priority': int|None}
        kind ∈ {wage_sheet, bill, attendance, payroll, empty, unknown}

规则来源：
    1. 项目级 DB 规则（project_classify_rules，按 project_id+enabled+priority）
    2. 缺规则（新项目）→ 回退到 DEFAULT_RULES（in-memory，不写 DB）

通配：
    pattern 含 '*' → 每个 '*' 匹配任意 1 字符（'*月平日工时' 匹 '3月平日工时'，'**月平日工时' 匹 '11月平日工时'）
    不含 '*' → 子串匹配（保持与旧 classify.py 兼容）

跨 kind 优先级硬编码：wage_sheet > bill > attendance > payroll
（业务约束：'应发/实发工资' 是 wage_sheet/bill 的强区分特征，不开放给用户改）
"""
import re

from etl.classify import collect_header_text
from etl.classify_default_rules import DEFAULT_RULES


KIND_ORDER = ('wage_sheet', 'bill', 'attendance', 'payroll')


def _collect_cells(ws, max_rows=4):
    """前 max_rows 行非空单元格（去空白后）。返回 cell 字符串列表。"""
    cells = []
    for row in ws.iter_rows(max_row=max_rows, values_only=True):
        for v in row:
            if v is None:
                continue
            s = re.sub(r'\s+', '', str(v).strip())
            if s:
                cells.append(s)
    return cells


def _has(pattern, head, cells):
    """pattern 是否命中表头。

    pattern 不含 * → 子串匹配 head（拼接串）
    pattern 含 *   → 每个 cell 内 regex search（不跨 cell，避免误匹）

    pattern 与 head/cells 都做空白规范化（去掉所有空格/换行/制表符），
    避免：表头 cell 含换行（如熊猫"出勤\n时数"被规范化成"出勤时数"）但
    用户配规则时输了带空格的"出勤 时数"导致匹不中。
    """
    pattern = re.sub(r'\s+', '', pattern)
    if not pattern:
        return False
    if '*' not in pattern:
        return pattern in head
    regex = re.escape(pattern).replace(r'\*', '.')
    return any(re.search(regex, c) for c in cells)


def _rule_matches(rule, head, cells):
    """单条规则判定：AND match_columns + OR match_columns_any + NOT(any) match_excludes"""
    mc = rule.get('match_columns') or []
    for kw in mc:
        if not _has(kw, head, cells):
            return False

    mca = rule.get('match_columns_any') or []
    if mca and not any(_has(kw, head, cells) for kw in mca):
        return False

    mex = rule.get('match_excludes') or []
    if mex and any(_has(kw, head, cells) for kw in mex):
        return False

    return True


def _load_rules_from_db(project_id, conn):
    """读项目级规则。若该项目无规则 → 返回 None 让上层 fallback 到 DEFAULT_RULES。"""
    cur = conn.cursor()
    # ORDER BY 设计：
    #   1. 按 target_kind 分组
    #   2. format_id 非空的规则优先（format_id IS NULL → 1，非空 → 0，0 排前）
    #      —— 新规则优先匹中，老的 NULL 规则只在新规则都不命中时兜底
    #   3. priority 升序
    # 小鱼发薪 format 不参与 xlsx classify（数据从 DB 流装入）
    # 禁用 format（status='disabled'）的规则也被剔除
    cur.execute("""
        SELECT r.id, r.target_kind, r.priority, r.match_columns, r.match_columns_any,
               r.match_excludes, r.scan_rows, r.handler, r.column_mapping, r.format_id
        FROM project_classify_rules r
        LEFT JOIN project_formats f ON r.format_id=f.id
        WHERE r.project_id=%s AND r.enabled=1
          AND COALESCE(f.is_xiaoyu_payroll, 0)=0
          AND COALESCE(f.status, 'active') <> 'disabled'
        ORDER BY r.target_kind, (r.format_id IS NULL), r.priority
    """, (project_id,))
    rows = cur.fetchall()
    if not rows:
        return None
    import json
    rules = []
    for r in rows:
        def _j(v):
            if v is None: return None
            if isinstance(v, (list, dict)): return v
            return json.loads(v)
        rules.append({
            'id': r[0],
            'target_kind': r[1],
            'priority': r[2],
            'match_columns': _j(r[3]),
            'match_columns_any': _j(r[4]),
            'match_excludes': _j(r[5]),
            'scan_rows': r[6],
            'handler': r[7],
            'column_mapping': _j(r[8]),
            'format_id': r[9],
        })
    return rules


def _has_duplicate_match_columns(rule):
    """规则 match_columns 是否含重复列名（双区域横向考勤的强信号）"""
    mc = rule.get('match_columns') or []
    if len(mc) < 2:
        return False
    return len(mc) != len(set(mc))


def _bump_match_count(conn, rule_id):
    """命中后异步累加 match_count + last_matched_at。失败不抛（统计字段，不影响主流程）。"""
    try:
        cur = conn.cursor()
        cur.execute("""UPDATE project_classify_rules
                       SET match_count=match_count+1, last_matched_at=NOW()
                       WHERE id=%s""", (rule_id,))
    except Exception:
        pass


def classify_sheet(ws, project_id=None, conn=None, bump_stats=True, all_matches=False):
    """识别 sheet kind。

    project_id+conn 都给 → 走 DB 规则（命中后累加 match_count）
    任一为 None        → 走 DEFAULT_RULES（不更新统计）

    all_matches=False（默认）：返回首个命中 dict（按 KIND_ORDER 优先级，向后兼容）
    all_matches=True：返回 list[dict]，按 KIND_ORDER 顺序，每 kind 内取首个命中
        —— 同 sheet 跨 kind 双装入场景（如雷悦：左半 pivot 考勤 + 右半账单）
    """
    cells = _collect_cells(ws, max_rows=4)
    if not cells:
        cells = _collect_cells(ws, max_rows=10)
        if not cells:
            empty = {'kind': 'empty', 'rule_id': None, 'handler': None, 'priority': None}
            return [] if all_matches else empty
    head = '|'.join(cells)

    # 缓存 scan_rows>4 的扩展表头（按需懒构建）
    extended_cache = {4: (head, cells)}

    def _get_head(scan_rows):
        if scan_rows in extended_cache:
            return extended_cache[scan_rows]
        ext_cells = _collect_cells(ws, max_rows=scan_rows)
        ext_head = '|'.join(ext_cells)
        extended_cache[scan_rows] = (ext_head, ext_cells)
        return ext_head, ext_cells

    db_rules = None
    use_db = bool(project_id and conn)
    if use_db:
        db_rules = _load_rules_from_db(project_id, conn)
    db_rules = db_rules or []
    db_kinds_present = {r.get('target_kind') for r in db_rules}

    # 项目分类规则策略：
    # - 项目有任意 rule → 严格按配置走，未配的 kind 不识别（不再走 DEFAULT_RULES fallback）
    # - 项目完全没配（新项目）→ 全 kind 用 DEFAULT_RULES 兜底，避免业务中断
    has_any_db_rules = bool(db_rules)

    matches = []
    for kind in KIND_ORDER:
        if has_any_db_rules:
            if kind not in db_kinds_present:
                continue  # 项目已配部分 rule，但没配该 kind → 不识别
            src_rules = [r for r in db_rules if r.get('target_kind') == kind]
            kind_used_db = True
        else:
            src_rules = [r for r in DEFAULT_RULES if r.get('target_kind') == kind]
            kind_used_db = False
        # 排序：format_id 非空优先（IS NULL 排后），同组按 priority
        kind_rules = sorted(src_rules,
                            key=lambda r: (1 if r.get('format_id') is None else 0,
                                           r.get('priority', 100)))
        for rule in kind_rules:
            sr = rule.get('scan_rows') or 4
            hd, cs = _get_head(sr)
            if _rule_matches(rule, hd, cs):
                rid = rule.get('id')
                if kind_used_db and bump_stats and rid:
                    _bump_match_count(conn, rid)
                handler = rule.get('handler') or 'standard'
                # 双区域横向考勤自动识别：match_columns 含重复列名（如"序号/日期/班次/姓名/工时"
                # 列两遍，左右半镜像）→ 升级 handler='two_region_attendance'，由专用解析器
                # 拆出双区域记录。rule.handler 字段保持 'standard'，纯代码层兜底。
                if (handler == 'standard' and kind == 'attendance'
                        and _has_duplicate_match_columns(rule)):
                    handler = 'two_region_attendance'
                matches.append({
                    'kind': kind,
                    'rule_id': rid,
                    'handler': handler,
                    'priority': rule.get('priority'),
                    'column_mapping': rule.get('column_mapping'),
                    'format_id': rule.get('format_id'),
                })
                break  # 每 kind 取首个命中（priority 最高）

    if all_matches:
        return matches
    if matches:
        return matches[0]
    return {'kind': 'unknown', 'rule_id': None, 'handler': None,
            'priority': None, 'column_mapping': None, 'format_id': None}
