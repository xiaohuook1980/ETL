"""项目注册写动作（写 fish-test）

给 web/projects 页面提供四个注册动作：
- register_project：unregistered → registered，同时落 business_cycles + 改 daishou/profit
- update_project：编辑已注册项目的可编辑字段
- disable_project：注销（保留数据，软删）
- enable_project：恢复注销
"""
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts._db import connect


_VALID_CYCLES = {'natural', '26_25', 'custom'}


def _ensure_business_cycle(cur, project_id, cycle_key, custom_start_day=None):
    """根据 UI 传入的 cycle_key 写一行 business_cycles（旧记录置 effective_end=今天-1）。

    cycle_key:
      'natural'  → cycle_type='自然月', start_day=1
      '26_25'    → cycle_type='非自然月', start_day=26
      'custom'   → cycle_type='非自然月', start_day=custom_start_day（必填）
    """
    if cycle_key == 'natural':
        cycle_type, start_day = '自然月', 1
    elif cycle_key == '26_25':
        cycle_type, start_day = '非自然月', 26
    elif cycle_key == 'custom':
        if not custom_start_day:
            raise ValueError("cycle_key='custom' 时必须传 custom_start_day")
        cycle_type, start_day = '非自然月', int(custom_start_day)
    else:
        raise ValueError(f'无效 cycle_key: {cycle_key!r}（合法值: {_VALID_CYCLES}）')

    today = datetime.now().date()

    cur.execute("""
        SELECT id, cycle_type, start_day, effective_end
        FROM business_cycles
        WHERE project_id=%s
        ORDER BY effective_start DESC, id DESC
        LIMIT 1
    """, (project_id,))
    row = cur.fetchone()
    if row:
        old_id, old_type, old_start, _ = row
        if old_type == cycle_type and old_start == start_day:
            return
        cur.execute("""
            UPDATE business_cycles SET effective_end=%s WHERE id=%s
        """, (today, old_id))

    cur.execute("""
        INSERT INTO business_cycles
            (project_id, cycle_type, start_day, effective_start, note)
        VALUES (%s, %s, %s, %s, %s)
    """, (project_id, cycle_type, start_day, today, 'web 注册写入'))


def register_project(project_id, cycle_key, daishou_threshold, profit_ratio,
                     custom_start_day=None):
    """注册项目（unregistered → registered）。

    Args:
        project_id (int): 项目 ID
        cycle_key (str): 'natural' / '26_25' / 'custom'
        daishou_threshold (int): 代收阈值（元）
        profit_ratio (float): 出款比例（0~1）
        custom_start_day (int, optional): cycle_key='custom' 时必填

    Returns:
        dict: {'project_id': int, 'status': 'registered'}
    """
    conn = connect('fish-test')
    try:
        cur = conn.cursor()

        cur.execute("SELECT id FROM projects WHERE id=%s", (project_id,))
        if not cur.fetchone():
            raise ValueError(f'project_id {project_id} 不存在')

        _ensure_business_cycle(cur, project_id, cycle_key, custom_start_day)

        cur.execute("UPDATE projects SET daishou_threshold=%s, profit_ratio=%s WHERE id=%s",
                    (int(daishou_threshold), float(profit_ratio), project_id))

        cur.execute("""
            INSERT INTO project_registrations
                (project_id, status, registered_at)
            VALUES (%s, 'registered', NOW())
            ON DUPLICATE KEY UPDATE
                status='registered',
                registered_at=NOW(),
                disabled_at=NULL
        """, (project_id,))

        conn.commit()
    finally:
        conn.close()

    return {'project_id': project_id, 'status': 'registered'}


def update_project(project_id, cycle_key=None, daishou_threshold=None,
                   profit_ratio=None, custom_start_day=None,
                   payroll_offset_n=None, payroll_offset_unit=None,
                   use_zhifa_as_faxin=None):
    """编辑已注册项目（任一字段可改）。

    只更新传入的非 None 字段。
    """
    conn = connect('fish-test')
    try:
        cur = conn.cursor()

        cur.execute("""
            SELECT pr.status FROM project_registrations pr
            WHERE pr.project_id=%s
        """, (project_id,))
        row = cur.fetchone()
        if not row:
            raise ValueError(f'project_id {project_id} 未注册，请先 register')
        if row[0] == 'disabled':
            raise ValueError(f'project_id {project_id} 已注销，请先 enable')

        if cycle_key is not None:
            _ensure_business_cycle(cur, project_id, cycle_key, custom_start_day)

        sets, args = [], []
        if daishou_threshold is not None:
            sets.append('daishou_threshold=%s')
            args.append(int(daishou_threshold))
        if profit_ratio is not None:
            sets.append('profit_ratio=%s')
            args.append(float(profit_ratio))
        if payroll_offset_n is not None:
            sets.append('payroll_offset_n=%s')
            args.append(int(payroll_offset_n))
        if payroll_offset_unit is not None:
            if payroll_offset_unit not in ('day', 'month'):
                raise ValueError(f'payroll_offset_unit 非法: {payroll_offset_unit}')
            sets.append('payroll_offset_unit=%s')
            args.append(payroll_offset_unit)
        if use_zhifa_as_faxin is not None:
            sets.append('use_zhifa_as_faxin=%s')
            args.append(1 if use_zhifa_as_faxin else 0)
        if sets:
            args.append(project_id)
            cur.execute(f"UPDATE projects SET {', '.join(sets)} WHERE id=%s", args)

        conn.commit()
    finally:
        conn.close()

    return {'project_id': project_id, 'status': 'registered'}


def save_payroll_bm_rules(project_id, extract_rules=None, infer_rules=None):
    """保存项目的发薪业务日期规则 (attribution_rules.payroll_bm)。

    extract_rules: list of {file_columns, column_names, enabled}
        指定规则:从列抽 ymd/ym
    infer_rules:   list of {file_columns, column_names, offset_n, offset_unit, enabled}
        推断规则:读列里的 date - offset → 业务日

    全量替换该项目的所有 payroll_bm 规则。
    """
    project_id = int(project_id)
    import json as _j
    conn = connect('fish-test')
    try:
        cur = conn.cursor()
        cur.execute("""DELETE FROM project_attribution_rules
                       WHERE project_id=%s AND category='payroll_bm'
                         AND scope='project' AND rule_type='column'""",
                    (project_id,))
        n_inserted = 0
        for r in extract_rules or []:
            cn = (r.get('column_names') or '').strip()
            fc = (r.get('file_columns') or '').strip()
            if not cn:
                continue
            cur.execute("""INSERT INTO project_attribution_rules
                           (project_id, category, scope, rule_type,
                            file_columns, column_names, mode, keywords, enabled,
                            offset_n, offset_unit)
                           VALUES (%s, 'payroll_bm', 'project', 'column',
                                   %s, %s, 'extract', %s, %s, 0, 'day')""",
                        (project_id, fc, cn, _j.dumps([], ensure_ascii=False),
                         1 if r.get('enabled') else 0))
            n_inserted += 1
        for r in infer_rules or []:
            cn = (r.get('column_names') or '').strip()
            fc = (r.get('file_columns') or '').strip()
            if not cn:
                continue
            n = int(r.get('offset_n') or 0)
            unit = r.get('offset_unit') or 'day'
            if unit not in ('day', 'month'):
                unit = 'day'
            cur.execute("""INSERT INTO project_attribution_rules
                           (project_id, category, scope, rule_type,
                            file_columns, column_names, mode, keywords, enabled,
                            offset_n, offset_unit)
                           VALUES (%s, 'payroll_bm', 'project', 'column',
                                   %s, %s, 'infer', %s, %s, %s, %s)""",
                        (project_id, fc, cn, _j.dumps([], ensure_ascii=False),
                         1 if r.get('enabled') else 0, n, unit))
            n_inserted += 1
        conn.commit()
    finally:
        conn.close()
    return {'project_id': project_id, 'inserted': n_inserted}


# 兼容旧调用
def save_payroll_bm_rule(project_id, column_names, enabled):
    """旧接口保留：等价于单条规则保存（file_columns=''）"""
    return save_payroll_bm_rules(project_id, [{
        'column_names': column_names,
        'file_columns': '',
        'enabled': enabled,
    }])


def disable_project(project_id):
    """注销（registered → disabled）。"""
    conn = connect('fish-test')
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO project_registrations
                (project_id, status, disabled_at)
            VALUES (%s, 'disabled', NOW())
            ON DUPLICATE KEY UPDATE
                status='disabled',
                disabled_at=NOW()
        """, (project_id,))
        conn.commit()
    finally:
        conn.close()
    return {'project_id': project_id, 'status': 'disabled'}


def enable_project(project_id):
    """恢复（disabled → registered）。"""
    conn = connect('fish-test')
    try:
        cur = conn.cursor()
        cur.execute("""
            UPDATE project_registrations
            SET status='registered', disabled_at=NULL, registered_at=NOW()
            WHERE project_id=%s
        """, (project_id,))
        if cur.rowcount == 0:
            raise ValueError(f'project_id {project_id} 不在 project_registrations，无法恢复')
        conn.commit()
    finally:
        conn.close()
    return {'project_id': project_id, 'status': 'registered'}
