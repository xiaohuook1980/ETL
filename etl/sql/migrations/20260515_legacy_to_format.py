"""一次性迁移：把 format_id IS NULL 的老配置统一映射到 format

策略：
  - format_mode=1 项目（已有 format）：直接删 NULL 残留（用户已在 format 上重配）
  - format_mode=0 项目（无 format）：按 kind 自动建默认 format，把 NULL 规则
    UPDATE format_id=新id，最后切 projects.format_mode=1

表 → kind 映射：
  - project_classify_rules.target_kind     → kind
  - project_enterprise_rules.target_kind   → kind
  - project_validity_rules.target_kind     → kind
  - project_pivot_templates.target_kind    → kind (实际仅 attendance)
  - project_payroll_biz_date_rules.*       → payroll (整表)
  - project_attribution_rules.category     → kaoqin_bill→[attendance,bill] / wage→[wage_sheet] / payroll→[payroll]
  - project_aggregate_label_rules          → bill (整表，但 NULL=0 跳过)

attribution 的 kaoqin_bill 特殊：旧 NULL 同时对 attendance/bill 生效；
迁移时若两 kind 都建了 format，需复制成两份；只建一边则 UPDATE 到那边。

用法：
    python etl/sql/migrations/20260515_legacy_to_format.py            # dry-run（默认）
    python etl/sql/migrations/20260515_legacy_to_format.py --commit   # 实跑
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from scripts._db import connect


DEFAULT_FORMAT_NAME = '默认'

CATEGORY_TO_KINDS = {
    'kaoqin_bill': ['attendance', 'bill'],
    'wage': ['wage_sheet'],
    'payroll': ['payroll'],
    'payroll_bm': ['payroll'],
}

# (table, kind_column_or_None)：kind_column=None 表示整表只属于一个 kind
PER_KIND_TABLES = [
    ('project_classify_rules', 'target_kind'),
    ('project_enterprise_rules', 'target_kind'),
    ('project_validity_rules', 'target_kind'),
    ('project_pivot_templates', 'target_kind'),
]


def _collect_kinds_for_project(cur, pid):
    """该项目所有 NULL 配置涉及的 kind 集合"""
    kinds = set()
    for table, col in PER_KIND_TABLES:
        cur.execute(f"SELECT DISTINCT {col} FROM {table} "
                    f"WHERE project_id=%s AND format_id IS NULL", (pid,))
        for r in cur.fetchall():
            if r[0]:
                kinds.add(r[0])
    cur.execute("SELECT 1 FROM project_payroll_biz_date_rules "
                "WHERE project_id=%s AND format_id IS NULL LIMIT 1", (pid,))
    if cur.fetchone():
        kinds.add('payroll')
    cur.execute("SELECT DISTINCT category FROM project_attribution_rules "
                "WHERE project_id=%s AND format_id IS NULL", (pid,))
    for r in cur.fetchall():
        for k in CATEGORY_TO_KINDS.get(r[0], []):
            kinds.add(k)
    return kinds


def _create_default_format(cur, pid, kind):
    cur.execute("""INSERT INTO project_formats
                   (project_id, target_kind, name, handler, is_default, status, note)
                   VALUES (%s, %s, %s, 'standard', 1, 'active', '老配置自动迁移')""",
                (pid, kind, DEFAULT_FORMAT_NAME))
    return cur.lastrowid


def _migrate_project(cur, pid, dry_run=True):
    """返回 (kind_to_fid, stats)"""
    kinds = _collect_kinds_for_project(cur, pid)
    stats = {'kinds_built': [], 'updated_rows': {}, 'cloned_attribution': 0}
    if not kinds:
        return {}, stats

    kind_to_fid = {}
    for kind in sorted(kinds):
        if dry_run:
            kind_to_fid[kind] = f'<NEW:{pid}/{kind}>'
        else:
            kind_to_fid[kind] = _create_default_format(cur, pid, kind)
        stats['kinds_built'].append(kind)

    for table, col in PER_KIND_TABLES:
        cur.execute(f"SELECT {col}, COUNT(*) FROM {table} "
                    f"WHERE project_id=%s AND format_id IS NULL GROUP BY {col}", (pid,))
        for kind, n in cur.fetchall():
            if kind in kind_to_fid:
                stats['updated_rows'].setdefault(table, 0)
                stats['updated_rows'][table] += n
                if not dry_run:
                    cur.execute(f"UPDATE {table} SET format_id=%s "
                                f"WHERE project_id=%s AND {col}=%s AND format_id IS NULL",
                                (kind_to_fid[kind], pid, kind))

    if 'payroll' in kind_to_fid:
        cur.execute("SELECT COUNT(*) FROM project_payroll_biz_date_rules "
                    "WHERE project_id=%s AND format_id IS NULL", (pid,))
        n = cur.fetchone()[0]
        if n:
            stats['updated_rows']['project_payroll_biz_date_rules'] = n
            if not dry_run:
                cur.execute("UPDATE project_payroll_biz_date_rules SET format_id=%s "
                            "WHERE project_id=%s AND format_id IS NULL",
                            (kind_to_fid['payroll'], pid))

    # attribution：kaoqin_bill 可能复制到 attendance + bill 两个 format
    cur.execute("""SELECT id, category FROM project_attribution_rules
                   WHERE project_id=%s AND format_id IS NULL""", (pid,))
    attr_rows = cur.fetchall()
    for rid, category in attr_rows:
        target_kinds = CATEGORY_TO_KINDS.get(category, [])
        fids = [kind_to_fid[k] for k in target_kinds if k in kind_to_fid]
        if not fids:
            continue
        stats['updated_rows'].setdefault('project_attribution_rules', 0)
        stats['updated_rows']['project_attribution_rules'] += 1
        if not dry_run:
            cur.execute("UPDATE project_attribution_rules SET format_id=%s WHERE id=%s",
                        (fids[0], rid))
        for extra_fid in fids[1:]:
            stats['cloned_attribution'] += 1
            if not dry_run:
                cur.execute("""INSERT INTO project_attribution_rules
                               (project_id, category, scope, rule_type, column_names, mode,
                                keywords, enabled, file_columns, offset_n, offset_unit, format_id)
                               SELECT project_id, category, scope, rule_type, column_names, mode,
                                      keywords, enabled, file_columns, offset_n, offset_unit, %s
                               FROM project_attribution_rules WHERE id=%s""",
                            (extra_fid, rid))

    if not dry_run:
        cur.execute("UPDATE projects SET format_mode=1 WHERE id=%s", (pid,))
    return kind_to_fid, stats


def _delete_null_for_format_mode_projects(cur, dry_run=True):
    """format_mode=1 项目下的 NULL 残留直接删"""
    cur.execute("SELECT id FROM projects WHERE format_mode=1")
    pids = [r[0] for r in cur.fetchall()]
    if not pids:
        return {}
    deleted = {}
    tables = [t for t, _ in PER_KIND_TABLES] + [
        'project_payroll_biz_date_rules',
        'project_attribution_rules',
        'project_aggregate_label_rules',
    ]
    placeholders = ','.join(['%s'] * len(pids))
    for t in tables:
        cur.execute(f"SELECT COUNT(*) FROM {t} "
                    f"WHERE project_id IN ({placeholders}) AND format_id IS NULL", pids)
        n = cur.fetchone()[0]
        deleted[t] = n
        if n and not dry_run:
            cur.execute(f"DELETE FROM {t} "
                        f"WHERE project_id IN ({placeholders}) AND format_id IS NULL", pids)
    return deleted


def main(commit=False):
    dry_run = not commit
    conn = connect('fish-test')
    try:
        cur = conn.cursor()
        print(f'=== {"COMMIT" if commit else "DRY-RUN"} 迁移开始 ===\n')

        print('--- 阶段1：format_mode=1 项目，删 NULL 残留 ---')
        deleted = _delete_null_for_format_mode_projects(cur, dry_run=dry_run)
        for t, n in deleted.items():
            if n:
                print(f'  {t}: {n} 行')
        if not any(deleted.values()):
            print('  (无残留)')

        print('\n--- 阶段2：format_mode=0 项目，建默认 format ---')
        cur.execute("SELECT id, title FROM projects WHERE format_mode=0 ORDER BY id")
        projs = cur.fetchall()
        n_proj_touched = 0
        agg = {'projects': 0, 'formats_created': 0, 'rows_remapped': 0, 'cloned_attribution': 0}
        for pid, pname in projs:
            kind_to_fid, stats = _migrate_project(cur, pid, dry_run=dry_run)
            if not kind_to_fid:
                continue
            n_proj_touched += 1
            agg['projects'] += 1
            agg['formats_created'] += len(stats['kinds_built'])
            row_n = sum(stats['updated_rows'].values())
            agg['rows_remapped'] += row_n
            agg['cloned_attribution'] += stats['cloned_attribution']
            print(f'  [{pid}] {pname}: kinds={stats["kinds_built"]}, '
                  f'rows={stats["updated_rows"]}, clones={stats["cloned_attribution"]}')

        if n_proj_touched == 0:
            print('  (无 format_mode=0 项目需要迁移)')

        print('\n=== 汇总 ===')
        print(f'  阶段1 删除：{sum(deleted.values())} 行')
        print(f'  阶段2 迁移项目：{agg["projects"]} 个')
        print(f'  阶段2 新建 format：{agg["formats_created"]} 个')
        print(f'  阶段2 改 format_id：{agg["rows_remapped"]} 行')
        print(f'  阶段2 attribution 复制：{agg["cloned_attribution"]} 行')

        if commit:
            conn.commit()
            print('\n[COMMIT 完成]')
        else:
            conn.rollback()
            print('\n[DRY-RUN，未提交。加 --commit 实跑]')
    finally:
        conn.close()


if __name__ == '__main__':
    main(commit='--commit' in sys.argv)
