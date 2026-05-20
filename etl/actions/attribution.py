"""项目归属规则写动作（写 fish-test.project_attribution_rules）

UI 提交单条规则 → upsert
UI 删除单条规则 → delete
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts._db import connect


_VALID_CATEGORIES = {'kaoqin_bill', 'wage', 'payroll', 'payroll_bm'}
_VALID_SCOPES = {'enterprise', 'project'}
_VALID_RULE_TYPES = {'sheet', 'column'}
_VALID_MODES = {'include', 'exclude', 'extract', 'infer', 'eq', 'neq'}


def _normalize_keywords(kws):
    """前端可能传字符串（'a, b, c'）或数组 → 统一为去空白的数组"""
    if kws is None:
        return []
    if isinstance(kws, str):
        parts = [p.strip() for p in kws.replace('，', ',').replace(';', ',').split(',')]
    elif isinstance(kws, list):
        parts = [str(p).strip() for p in kws]
    else:
        return []
    return [p for p in parts if p]


def _upsert_one(cur, project_id, category, scope, rule_type, column_names, mode,
                keywords, enabled, file_columns='', format_id=None):
    """内部：单条 upsert（已开启事务，不提交）"""
    kws = _normalize_keywords(keywords)
    if rule_type == 'column':
        column_names = (column_names or '').strip()
    else:
        column_names = ''
    file_columns = (file_columns or '').strip()
    if mode not in _VALID_MODES:
        mode = 'extract' if category == 'payroll_bm' else 'include'

    fmt = int(format_id) if format_id else None

    # 删除条件: column 规则 file_columns / column_names / keywords 三者皆空(无任何信息)
    # 否则保留 — file_columns 单独非空 = 白名单语义(parser 端按 file_columns 命中即全装入)
    is_empty = (rule_type == 'column' and not column_names and not file_columns and not kws)
    if rule_type == 'sheet' and not kws:
        is_empty = True
    if is_empty:
        # 删除按 format_id 限定（避免误删其他 format 的同形规则）
        if fmt is not None:
            cur.execute("""
                DELETE FROM project_attribution_rules
                WHERE project_id=%s AND category=%s AND scope=%s
                  AND rule_type=%s AND file_columns=%s AND column_names=%s AND mode=%s
                  AND format_id=%s
            """, (project_id, category, scope, rule_type, file_columns, column_names, mode, fmt))
        else:
            cur.execute("""
                DELETE FROM project_attribution_rules
                WHERE project_id=%s AND category=%s AND scope=%s
                  AND rule_type=%s AND file_columns=%s AND column_names=%s AND mode=%s
                  AND format_id IS NULL
            """, (project_id, category, scope, rule_type, file_columns, column_names, mode))
        return {'action': 'deleted', 'scope': scope, 'mode': mode,
                'file_columns': file_columns,
                'column_names': column_names, 'keywords': kws}

    cur.execute("""
        INSERT INTO project_attribution_rules
            (project_id, category, scope, rule_type, file_columns, column_names,
             mode, keywords, enabled, format_id)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE
            keywords=VALUES(keywords),
            enabled=VALUES(enabled),
            updated_at=NOW()
    """, (project_id, category, scope, rule_type, file_columns, column_names, mode,
          json.dumps(kws, ensure_ascii=False), 1 if enabled else 0, fmt))
    return {'action': 'upserted', 'scope': scope, 'mode': mode,
            'file_columns': file_columns,
            'column_names': column_names, 'keywords': kws, 'enabled': bool(enabled)}


_CATEGORY_TO_KINDS = {
    'kaoqin_bill': ('attendance', 'bill'),
    'wage':        ('wage_sheet',),
    'payroll':     ('payroll',),
    'payroll_bm':  ('payroll',),
}


def _link_columns_to_extra_data(cur, project_id, category, columns, format_id):
    """把 attribution 规则的 column_names 加到对应 kind 的 classify rule.column_mapping.extra_data。
    这样 standard handler 装 mart 时这些列会进 extra_data，下游 apply_row_filter_to_dicts
    才能取到值（rows[0]['extra_data']['摘要'] 而不是丢失）。

    column_names 支持 'A|B|C' 多候选；全部都加。
    跳过已经在 column_mapping 任一字段值中的列名（已映射到顶层 mart 字段，不必再进 extra_data）。
    """
    kinds = _CATEGORY_TO_KINDS.get(category, ())
    if not kinds:
        return
    # 收集所有列名（拆 | 多候选）
    all_cols = []
    for c in columns:
        cn = (c.get('column_names') or '').strip()
        if not cn:
            continue
        for c2 in cn.split('|'):
            c2 = c2.strip()
            if c2 and c2 not in all_cols:
                all_cols.append(c2)
    if not all_cols:
        return
    # 找对应的 classify rule（按 format_id 过滤）
    placeholders = ','.join(['%s'] * len(kinds))
    if format_id is not None:
        cur.execute(f"""SELECT id, column_mapping FROM project_classify_rules
                        WHERE project_id=%s AND target_kind IN ({placeholders})
                          AND enabled=1 AND format_id=%s""",
                    [project_id] + list(kinds) + [format_id])
    else:
        cur.execute(f"""SELECT id, column_mapping FROM project_classify_rules
                        WHERE project_id=%s AND target_kind IN ({placeholders})
                          AND enabled=1 AND format_id IS NULL""",
                    [project_id] + list(kinds))
    for rid, cm_raw in cur.fetchall():
        cm = cm_raw if isinstance(cm_raw, dict) else (json.loads(cm_raw) if cm_raw else {})
        if not isinstance(cm, dict):
            cm = {}
        # 已映射的（任一字段值含该列名，逗号/中文逗号分隔）→ 跳过
        already_mapped = set()
        for k, v in cm.items():
            if not isinstance(v, str):
                continue
            for c2 in v.replace('，', ',').split(','):
                already_mapped.add(c2.strip())
        existing_extra = (cm.get('extra_data') or '').strip()
        merged = [c.strip() for c in existing_extra.replace('，', ',').split(',') if c.strip()]
        changed = False
        for col in all_cols:
            if col in already_mapped or col in merged:
                continue
            merged.append(col)
            changed = True
        if changed:
            cm['extra_data'] = ','.join(merged)
            cur.execute("UPDATE project_classify_rules SET column_mapping=%s WHERE id=%s",
                        (json.dumps(cm, ensure_ascii=False), rid))


def save_scope(project_id, category, scope, sheet=None, columns=None, format_id=None):
    """保存某 (category, scope, format_id) 的全部规则（sheet 1 行 + column N 行）。
    format_id 给定时：仅替换本 format 的规则；其他 format 的规则不动"""
    project_id = int(project_id)
    if category not in _VALID_CATEGORIES:
        raise ValueError(f'category 非法: {category}')
    if scope not in _VALID_SCOPES:
        raise ValueError(f'scope 非法: {scope}')

    fmt = int(format_id) if format_id else None

    conn = connect('fish-test')
    results = []
    try:
        cur = conn.cursor()

        if sheet is not None:
            # mode 切换时 UNIQUE 含 mode → ON DUPLICATE 不命中老记录会留幽灵
            # 先按 (project,category,scope,rule_type='sheet',format_id) 全删，再插新的
            if fmt is not None:
                cur.execute("""
                    DELETE FROM project_attribution_rules
                    WHERE project_id=%s AND category=%s AND scope=%s
                      AND rule_type='sheet' AND format_id=%s
                """, (project_id, category, scope, fmt))
            else:
                cur.execute("""
                    DELETE FROM project_attribution_rules
                    WHERE project_id=%s AND category=%s AND scope=%s
                      AND rule_type='sheet' AND format_id IS NULL
                """, (project_id, category, scope))
            results.append(_upsert_one(cur, project_id, category, scope, 'sheet',
                                       column_names='',
                                       mode=sheet.get('mode', 'include'),
                                       keywords=sheet.get('keywords', []),
                                       enabled=bool(sheet.get('enabled')),
                                       format_id=fmt))

        if columns is not None:
            # 全量替换该 (category, scope, format_id) 的 column 规则
            if fmt is not None:
                cur.execute("""
                    DELETE FROM project_attribution_rules
                    WHERE project_id=%s AND category=%s AND scope=%s
                      AND rule_type='column' AND format_id=%s
                """, (project_id, category, scope, fmt))
            else:
                cur.execute("""
                    DELETE FROM project_attribution_rules
                    WHERE project_id=%s AND category=%s AND scope=%s
                      AND rule_type='column' AND format_id IS NULL
                """, (project_id, category, scope))
            for c in columns:
                cn = (c.get('column_names') or '').strip()
                fc = (c.get('file_columns') or '').strip()
                kws = _normalize_keywords(c.get('keywords'))
                # 三者皆空才跳过;file_columns 单独非空 = 白名单(全装入)
                if not cn and not fc and not kws:
                    continue
                results.append(_upsert_one(cur, project_id, category, scope, 'column',
                                           column_names=cn,
                                           mode=c.get('mode', 'include'),
                                           keywords=kws,
                                           enabled=bool(c.get('enabled')),
                                           file_columns=fc,
                                           format_id=fmt))

        # 联动：把所有非空 column_names 加到对应 kind 的 classify rule extra_data，
        # 让 standard handler 装行时把这些列装进 extra_data，下游过滤器才能取到值。
        _link_columns_to_extra_data(cur, project_id, category, columns or [], fmt)

        conn.commit()
    finally:
        conn.close()

    return results


def save_all_rules(project_id, rules, format_id=None):
    """批量保存。format_id 给定时所有规则挂在该 format 下。

    rules 格式：
        {
          'kaoqin_bill': {
              'enterprise': {'sheet': {...}, 'columns': [...]},
              'project':    {'sheet': {...}, 'columns': [...]}
          },
          'wage':    {'project': {...}},
          'payroll': {'project': {...}},
        }
    任一 cell 可以是 None（不变更该 cell）。
    """
    out = []
    for cat, by_scope in (rules or {}).items():
        if cat not in _VALID_CATEGORIES or not by_scope:
            continue
        for sc, body in by_scope.items():
            if sc not in _VALID_SCOPES or not body:
                continue
            out.extend(save_scope(project_id, cat, sc,
                                  sheet=body.get('sheet'),
                                  columns=body.get('columns'),
                                  format_id=format_id))
    return out
