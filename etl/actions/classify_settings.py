"""数据识别配置写操作（fish-test.project_classify_rules + project_validity_rules）"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts._db import connect
from etl.classify_default_rules import DEFAULT_RULES


KINDS = ('attendance', 'bill', 'wage_sheet', 'payroll')


def seed_default_rules(project_id, kind=None, replace=False):
    """把 DEFAULT_RULES 复制到 project_classify_rules。

    project_id: 目标项目
    kind: 'attendance' / 'bill' / 'wage_sheet' / 'payroll' / None=全部 4 类
    replace: True=先删本项目本 kind 现有规则再插入；False=已有规则则跳过
    """
    project_id = int(project_id)
    target_kinds = [kind] if kind else list(KINDS)

    inserted = {k: 0 for k in target_kinds}
    skipped = {k: 0 for k in target_kinds}

    conn = connect('fish-test')
    try:
        cur = conn.cursor()
        for k in target_kinds:
            if replace:
                cur.execute("DELETE FROM project_classify_rules WHERE project_id=%s AND target_kind=%s",
                            (project_id, k))
            else:
                cur.execute("SELECT COUNT(*) FROM project_classify_rules WHERE project_id=%s AND target_kind=%s",
                            (project_id, k))
                if cur.fetchone()[0] > 0:
                    skipped[k] = -1  # -1 表示"已有规则跳过"
                    continue
            for r in DEFAULT_RULES:
                if r.get('target_kind') != k:
                    continue
                cur.execute("""
                    INSERT INTO project_classify_rules
                        (project_id, target_kind, priority, match_columns, match_columns_any,
                         match_excludes, scan_rows, handler, column_mapping, enabled, note)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 1, %s)
                """, (
                    project_id, k, r['priority'],
                    json.dumps(r.get('match_columns') or [], ensure_ascii=False),
                    json.dumps(r['match_columns_any'], ensure_ascii=False) if r.get('match_columns_any') else None,
                    json.dumps(r['match_excludes'], ensure_ascii=False) if r.get('match_excludes') else None,
                    r.get('scan_rows', 10),
                    r.get('handler', 'standard'),
                    json.dumps(r['column_mapping'], ensure_ascii=False) if r.get('column_mapping') else None,
                    (r.get('note') or '')[:255],
                ))
                inserted[k] += 1
        conn.commit()
    finally:
        conn.close()

    return {'project_id': str(project_id), 'inserted': inserted, 'skipped': skipped}


def upsert_classify_rule(project_id, kind, rule_data):
    """新建或更新一条 project_classify_rules。

    rule_data: dict，键名对齐 schema：
        id (有则更新，无则插入)
        priority / match_columns (list) / match_columns_any (list|None) / match_excludes (list|None)
        scan_rows / handler / column_mapping (dict|None) / enabled / note
    """
    project_id = int(project_id)
    if kind not in KINDS:
        raise ValueError(f'invalid kind: {kind}')

    rid = rule_data.get('id')
    priority = int(rule_data.get('priority', 100))
    mc = rule_data.get('match_columns') or []
    mca = rule_data.get('match_columns_any') or None
    mex = rule_data.get('match_excludes') or None
    scan_rows = int(rule_data.get('scan_rows') or 10)
    handler = rule_data.get('handler') or 'standard'
    cm = rule_data.get('column_mapping') or None
    enabled = 1 if rule_data.get('enabled', True) else 0
    note = (rule_data.get('note') or '')[:255]
    format_id = rule_data.get('format_id')
    if format_id is not None:
        format_id = int(format_id) if format_id else None

    conn = connect('fish-test')
    try:
        cur = conn.cursor()
        if rid:
            cur.execute("""
                UPDATE project_classify_rules SET
                    priority=%s, match_columns=%s, match_columns_any=%s, match_excludes=%s,
                    scan_rows=%s, handler=%s, column_mapping=%s, enabled=%s, note=%s,
                    format_id=%s
                WHERE id=%s AND project_id=%s AND target_kind=%s
            """, (
                priority,
                json.dumps(mc, ensure_ascii=False),
                json.dumps(mca, ensure_ascii=False) if mca else None,
                json.dumps(mex, ensure_ascii=False) if mex else None,
                scan_rows, handler,
                json.dumps(cm, ensure_ascii=False) if cm else None,
                enabled, note,
                format_id,
                int(rid), project_id, kind,
            ))
            action = 'updated' if cur.rowcount > 0 else 'noop'
        else:
            cur.execute("""
                INSERT INTO project_classify_rules
                    (project_id, target_kind, priority, match_columns, match_columns_any,
                     match_excludes, scan_rows, handler, column_mapping, enabled, note, format_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (
                project_id, kind, priority,
                json.dumps(mc, ensure_ascii=False),
                json.dumps(mca, ensure_ascii=False) if mca else None,
                json.dumps(mex, ensure_ascii=False) if mex else None,
                scan_rows, handler,
                json.dumps(cm, ensure_ascii=False) if cm else None,
                enabled, note, format_id,
            ))
            rid = cur.lastrowid
            action = 'inserted'
        conn.commit()
    finally:
        conn.close()
    return {'id': rid, 'action': action}


def delete_classify_rule(project_id, rule_id):
    """删除单条规则。"""
    project_id = int(project_id)
    rule_id = int(rule_id)
    conn = connect('fish-test')
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM project_classify_rules WHERE id=%s AND project_id=%s",
                    (rule_id, project_id))
        n = cur.rowcount
        conn.commit()
    finally:
        conn.close()
    return {'deleted': n}


def _ensure_filter_column_in_mapping(cur, project_id, kind, filter_column, mode):
    """validity 规则保存后联动：filter_column 如不是 mart 友好名（如"姓名"）也不在
    列映射任一字段值里，自动追加到所有同 kind classify rule 的 column_mapping.extra_data。
    返回 [{'rule_id': N, 'added': '转账备注'}, ...] 给 UI 提示用。

    覆盖 mode：
      col_neq / col_eq: filter_column='列A,列B' 拆开两列分别处理
      其他 mode: filter_column 是单列
    """
    from etl.parsers.handlers.validity import USER_COL_TO_MART
    if not filter_column:
        return []
    if mode in ('col_neq', 'col_eq'):
        cols = [c.strip() for c in filter_column.split(',') if c.strip()]
    else:
        cols = [filter_column.strip()]

    friendly = set(USER_COL_TO_MART.get(kind, {}).keys())
    cols = [c for c in cols if c and c not in friendly]
    if not cols:
        return []

    cur.execute("""SELECT id, column_mapping FROM project_classify_rules
                   WHERE project_id=%s AND target_kind=%s AND enabled=1""",
                (project_id, kind))
    rules = cur.fetchall()
    updates = []
    for rid, cm_raw in rules:
        if isinstance(cm_raw, str):
            try:
                cm = json.loads(cm_raw)
            except (ValueError, TypeError):
                continue
        elif isinstance(cm_raw, dict):
            cm = cm_raw
        else:
            continue
        # 列在任一 mart 字段值（逗号分隔）里就算已覆盖
        already_mapped = set()
        for mart_field, col_pat in (cm or {}).items():
            if not isinstance(col_pat, str):
                continue
            for c in col_pat.split(','):
                already_mapped.add(c.strip())
        # 缺哪些列
        to_add = [c for c in cols if c not in already_mapped]
        if not to_add:
            continue
        # 追加到 extra_data（不在就建空字符串）
        existing_extra = (cm.get('extra_data') or '').strip()
        merged = [c.strip() for c in existing_extra.split(',') if c.strip()]
        for c in to_add:
            if c not in merged:
                merged.append(c)
        cm['extra_data'] = ','.join(merged)
        cur.execute("""UPDATE project_classify_rules SET column_mapping=%s
                       WHERE id=%s""",
                    (json.dumps(cm, ensure_ascii=False), rid))
        for c in to_add:
            updates.append({'rule_id': rid, 'added': c})
    return updates


def upsert_validity_rule(project_id, kind, rule_data):
    """新建/更新一条 project_validity_rules。"""
    project_id = int(project_id)
    if kind not in KINDS:
        raise ValueError(f'invalid kind: {kind}')

    rid = rule_data.get('id')
    priority = int(rule_data.get('priority', 100))
    fc = rule_data.get('feature_columns') or []
    fe = 1 if rule_data.get('feature_enabled', False) else 0
    filter_col = (rule_data.get('filter_column') or '')[:64]
    fle = 1 if rule_data.get('filter_enabled', True) else 0
    mode = rule_data.get('mode') or 'exclude'
    fv = (rule_data.get('filter_value') or '')[:255]
    is_builtin = 1 if rule_data.get('is_builtin', False) else 0
    enabled = 1 if rule_data.get('enabled', True) else 0
    note = (rule_data.get('note') or '')[:255]
    count_as_faxin = 1 if rule_data.get('count_as_faxin', True) else 0
    format_id = rule_data.get('format_id')
    if format_id is not None:
        format_id = int(format_id) if format_id else None

    conn = connect('fish-test')
    try:
        cur = conn.cursor()
        if rid:
            cur.execute("""
                UPDATE project_validity_rules SET
                    priority=%s, feature_columns=%s, feature_enabled=%s,
                    filter_column=%s, filter_enabled=%s,
                    mode=%s, filter_value=%s, is_builtin=%s,
                    enabled=%s, note=%s, count_as_faxin=%s, format_id=%s
                WHERE id=%s AND project_id=%s AND target_kind=%s
            """, (
                priority,
                json.dumps(fc, ensure_ascii=False), fe,
                filter_col, fle,
                mode, fv, is_builtin,
                enabled, note, count_as_faxin, format_id,
                int(rid), project_id, kind,
            ))
            action = 'updated' if cur.rowcount > 0 else 'noop'
        else:
            cur.execute("""
                INSERT INTO project_validity_rules
                    (project_id, target_kind, priority, feature_columns, feature_enabled,
                     filter_column, filter_enabled, mode, filter_value, is_builtin, enabled, note, count_as_faxin, format_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (
                project_id, kind, priority,
                json.dumps(fc, ensure_ascii=False), fe,
                filter_col, fle,
                mode, fv, is_builtin,
                enabled, note, count_as_faxin, format_id,
            ))
            rid = cur.lastrowid
            action = 'inserted'
        # 联动：filter_column 是 raw 列名（非 mart 友好名）→ 自动补进 column_mapping.extra_data
        cm_updates = _ensure_filter_column_in_mapping(cur, project_id, kind, filter_col, mode)
        conn.commit()
    finally:
        conn.close()
    return {'id': rid, 'action': action, 'column_mapping_updates': cm_updates}


def delete_validity_rule(project_id, rule_id):
    """删除单条有效性规则（is_builtin=1 拒绝删）"""
    project_id = int(project_id)
    rule_id = int(rule_id)
    conn = connect('fish-test')
    try:
        cur = conn.cursor()
        cur.execute("SELECT is_builtin FROM project_validity_rules WHERE id=%s AND project_id=%s",
                    (rule_id, project_id))
        row = cur.fetchone()
        if not row:
            return {'deleted': 0, 'reason': 'not_found'}
        if row[0]:
            return {'deleted': 0, 'reason': 'builtin_protected'}
        cur.execute("DELETE FROM project_validity_rules WHERE id=%s AND project_id=%s",
                    (rule_id, project_id))
        n = cur.rowcount
        conn.commit()
    finally:
        conn.close()
    return {'deleted': n}


def _link_target_columns_to_extra_data(cur, project_id, target_columns, format_id):
    """把业务日期规则的 target_columns（'A|B|C' 多候选）加到对应 payroll classify rule
    的 column_mapping.extra_data。让 standard handler 装行时这些列进 extra_data，
    后续 resolve_business_date 能从 row['extra_data'] 取到值。"""
    cols = [c.strip() for c in (target_columns or '').split('|') if c.strip()]
    if not cols:
        return
    if format_id is not None:
        cur.execute("""SELECT id, column_mapping FROM project_classify_rules
                       WHERE project_id=%s AND target_kind='payroll'
                         AND enabled=1 AND format_id=%s""",
                    (project_id, format_id))
    else:
        cur.execute("""SELECT id, column_mapping FROM project_classify_rules
                       WHERE project_id=%s AND target_kind='payroll'
                         AND enabled=1 AND format_id IS NULL""",
                    (project_id,))
    for rid, cm_raw in cur.fetchall():
        cm = cm_raw if isinstance(cm_raw, dict) else (json.loads(cm_raw) if cm_raw else {})
        if not isinstance(cm, dict):
            cm = {}
        already = set()
        for k, v in cm.items():
            if not isinstance(v, str):
                continue
            for c in v.replace('，', ',').split(','):
                already.add(c.strip())
        existing = (cm.get('extra_data') or '').strip()
        merged = [c.strip() for c in existing.replace('，', ',').split(',') if c.strip()]
        changed = False
        for c in cols:
            if c in already or c in merged:
                continue
            merged.append(c)
            changed = True
        if changed:
            cm['extra_data'] = ','.join(merged)
            cur.execute("UPDATE project_classify_rules SET column_mapping=%s WHERE id=%s",
                        (json.dumps(cm, ensure_ascii=False), rid))


def upsert_payroll_biz_date_rule(project_id, rule_data):
    """新建/更新一条发薪业务日期判定规则"""
    project_id = int(project_id)
    rid = rule_data.get('id')
    rule_kind = rule_data.get('rule_kind')
    if rule_kind not in ('extract', 'infer', 'bill_month'):
        raise ValueError(f'invalid rule_kind: {rule_kind}')

    priority = int(rule_data.get('priority', 100))
    file_columns = (rule_data.get('file_columns') or '')[:255]
    target_columns = (rule_data.get('target_columns') or '')[:255]
    offset_n = int(rule_data.get('offset_n', 0))
    offset_unit = rule_data.get('offset_unit') or 'day'
    enabled = 1 if rule_data.get('enabled', True) else 0
    note = (rule_data.get('note') or '')[:255]
    format_id = rule_data.get('format_id')
    if format_id is not None:
        format_id = int(format_id) if format_id else None

    conn = connect('fish-test')
    try:
        cur = conn.cursor()
        if rid:
            cur.execute("""
                UPDATE project_payroll_biz_date_rules SET
                    priority=%s, file_columns=%s, target_columns=%s,
                    offset_n=%s, offset_unit=%s, enabled=%s, note=%s, format_id=%s
                WHERE id=%s AND project_id=%s AND rule_kind=%s
            """, (priority, file_columns, target_columns, offset_n, offset_unit,
                  enabled, note, format_id, int(rid), project_id, rule_kind))
            action = 'updated' if cur.rowcount > 0 else 'noop'
        else:
            cur.execute("""
                INSERT INTO project_payroll_biz_date_rules
                    (project_id, rule_kind, priority, file_columns, target_columns,
                     offset_n, offset_unit, enabled, note, format_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (project_id, rule_kind, priority, file_columns, target_columns,
                  offset_n, offset_unit, enabled, note, format_id))
            rid = cur.lastrowid
            action = 'inserted'
        # 联动：target_columns 是用户配的列名（如"月份"/"摘要"），需进 classify rule
        # column_mapping.extra_data，否则 standard handler 装行时丢失，extractor 取不到值
        if target_columns:
            _link_target_columns_to_extra_data(cur, project_id, target_columns, format_id)
        conn.commit()
    finally:
        conn.close()
    return {'id': rid, 'action': action}


def delete_payroll_biz_date_rule(project_id, rule_id):
    project_id = int(project_id)
    rule_id = int(rule_id)
    conn = connect('fish-test')
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM project_payroll_biz_date_rules WHERE id=%s AND project_id=%s",
                    (rule_id, project_id))
        n = cur.rowcount
        conn.commit()
    finally:
        conn.close()
    return {'deleted': n}


def upsert_enterprise_rule(project_id, kind, rule_data):
    """新建/更新一条 project_enterprise_rules（行级企业归属过滤）"""
    project_id = int(project_id)
    if kind not in KINDS:
        raise ValueError(f'invalid kind: {kind}')
    rid = rule_data.get('id')
    priority = int(rule_data.get('priority', 100))
    filter_column = (rule_data.get('filter_column') or '')[:64]
    filter_value = (rule_data.get('filter_value') or '')[:512]
    mode = rule_data.get('mode') or 'include'
    if mode not in ('include', 'exclude'):
        mode = 'include'
    enabled = 1 if rule_data.get('enabled', True) else 0
    note = (rule_data.get('note') or '')[:255]
    format_id = rule_data.get('format_id')
    if format_id is not None:
        format_id = int(format_id) if format_id else None

    conn = connect('fish-test')
    try:
        cur = conn.cursor()
        if rid:
            cur.execute("""
                UPDATE project_enterprise_rules SET
                    priority=%s, filter_column=%s, filter_value=%s,
                    mode=%s, enabled=%s, note=%s, format_id=%s
                WHERE id=%s AND project_id=%s AND target_kind=%s
            """, (priority, filter_column, filter_value, mode, enabled, note, format_id,
                  int(rid), project_id, kind))
            action = 'updated' if cur.rowcount > 0 else 'noop'
        else:
            cur.execute("""
                INSERT INTO project_enterprise_rules
                    (project_id, target_kind, priority, filter_column, filter_value,
                     mode, enabled, note, format_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (project_id, kind, priority, filter_column, filter_value,
                  mode, enabled, note, format_id))
            rid = cur.lastrowid
            action = 'inserted'
        conn.commit()
    finally:
        conn.close()
    return {'id': rid, 'action': action}


def delete_enterprise_rule(project_id, rule_id):
    project_id = int(project_id)
    rule_id = int(rule_id)
    conn = connect('fish-test')
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM project_enterprise_rules WHERE id=%s AND project_id=%s",
                    (rule_id, project_id))
        n = cur.rowcount
        conn.commit()
    finally:
        conn.close()
    return {'deleted': n}


def delete_project_rules(project_id, kind=None):
    """删除本项目本 kind 所有 classify 规则（管理用，慎用）"""
    project_id = int(project_id)
    target_kinds = [kind] if kind else list(KINDS)
    conn = connect('fish-test')
    try:
        cur = conn.cursor()
        deleted = {}
        for k in target_kinds:
            cur.execute("DELETE FROM project_classify_rules WHERE project_id=%s AND target_kind=%s",
                        (project_id, k))
            deleted[k] = cur.rowcount
        conn.commit()
    finally:
        conn.close()
    return deleted
