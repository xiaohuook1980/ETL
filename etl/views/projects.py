"""项目注册视图层（读 fish-test）

给 web/projects 页面提供数据：项目列表、统计、公司下拉
所有业务定义（"已注册"等于哪些状态、业务周期文本怎么拼）都在这里，web 不参与
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts._db import connect


def _format_cycle(cycle_type, start_day):
    if cycle_type is None:
        return None
    if cycle_type == '自然月':
        return '自然月'
    end_day = start_day - 1 if start_day > 1 else 31
    return f'上月{start_day}-本月{end_day}'


def list_projects(status=None, company=None, keyword=None):
    """返回项目列表 dict[]，每行字段对应 projects.html 表格列。

    参数：
        status: 'unregistered' / 'registered' / 'disabled' / None(全部)
            (UI 展示用 'registered'，DB 里对应 project_registrations.status='registered'
             或老 seed 项目 projects.status='active' 但无 project_registrations 行)
        company: 劳务公司 short_name 精确匹配；None=全部
        keyword: 项目 title 模糊匹配；None=不过滤
    """
    sql = """
    SELECT
        p.id              AS project_id,
        p.title           AS project_title,
        p.short_name      AS project_short_name,
        e.id              AS enterprise_id,
        e.short_name      AS enterprise_short,
        e.full_name       AS enterprise_full,
        COALESCE(pr.status, 'unregistered') AS reg_status,
        bc.cycle_type     AS cycle_type,
        bc.start_day      AS cycle_start_day,
        p.daishou_threshold,
        p.profit_ratio,
        (SELECT GROUP_CONCAT(c2.name SEPARATOR ' / ')
         FROM controller_enterprise_map cem2
         JOIN controllers c2 ON c2.id = cem2.controller_id
         WHERE cem2.enterprise_id = e.id) AS controller_name,
        (SELECT MIN(cem3.controller_id)
         FROM controller_enterprise_map cem3
         WHERE cem3.enterprise_id = e.id) AS controller_id,
        p.source_created_at,
        pr.registered_at,
        pr.disabled_at
    FROM projects p
    JOIN enterprises e ON e.id = p.enterprise_id
    LEFT JOIN project_registrations pr ON pr.project_id = p.id
    LEFT JOIN business_cycles bc ON bc.id = (
        SELECT bc2.id FROM business_cycles bc2
        WHERE bc2.project_id = p.id
        ORDER BY bc2.effective_start DESC, bc2.id DESC
        LIMIT 1
    )
    WHERE 1=1
    """
    args = []
    if status:
        sql += " AND COALESCE(pr.status, 'unregistered') = %s"
        args.append(status)
    if company:
        sql += " AND e.short_name = %s"
        args.append(company)
    if keyword:
        sql += " AND p.title LIKE %s"
        args.append(f'%{keyword}%')
    sql += " ORDER BY p.source_created_at IS NULL, p.source_created_at DESC, p.id DESC"

    conn = connect('fish-test')
    try:
        cur = conn.cursor()
        cur.execute(sql, args)
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    finally:
        conn.close()

    return [
        {
            # 大整数 ID 必须转成 str：JS Number 上限 2^53，老库 ID（19 位）会丢精度
            'project_id': str(r['project_id']),
            'project_title': r['project_title'],
            'project_short_name': r['project_short_name'],
            'enterprise_id': str(r['enterprise_id']) if r['enterprise_id'] is not None else None,
            'enterprise_short': r['enterprise_short'],
            'enterprise_full': r['enterprise_full'],
            'reg_status': r['reg_status'],
            'business_cycle': _format_cycle(r['cycle_type'], r['cycle_start_day']),
            'daishou_threshold': r['daishou_threshold'],
            'profit_ratio': float(r['profit_ratio']) if r['profit_ratio'] is not None else None,
            'controller_id': str(r['controller_id']) if r['controller_id'] is not None else None,
            'controller_name': r['controller_name'],
            'source_created_at': r['source_created_at'].isoformat(sep=' ') if r['source_created_at'] else None,
            'registered_at': r['registered_at'].isoformat(sep=' ') if r['registered_at'] else None,
            'disabled_at': r['disabled_at'].isoformat(sep=' ') if r['disabled_at'] else None,
        }
        for r in rows
    ]


def get_sync_status():
    """返回顶部统计 + 最近同步时间。

    最近同步时间：取 enterprises.updated_at 与 projects.updated_at 的最大值
    （sync 动作会刷这两个表）
    """
    conn = connect('fish-test')
    try:
        cur = conn.cursor()

        cur.execute("""
            SELECT COUNT(*) FROM projects p
            LEFT JOIN project_registrations pr ON pr.project_id = p.id
        """)
        total = cur.fetchone()[0]

        cur.execute("""
            SELECT COALESCE(pr.status, 'unregistered') AS s, COUNT(*)
            FROM projects p
            LEFT JOIN project_registrations pr ON pr.project_id = p.id
            GROUP BY s
        """)
        by_status = {row[0]: row[1] for row in cur.fetchall()}

        cur.execute("SELECT MAX(updated_at) FROM enterprises")
        ent_t = cur.fetchone()[0]
        cur.execute("SELECT MAX(updated_at) FROM projects")
        proj_t = cur.fetchone()[0]
        candidates = [t for t in (ent_t, proj_t) if t is not None]
        last_sync = max(candidates) if candidates else None
    finally:
        conn.close()

    return {
        'total': total,
        'registered': by_status.get('registered', 0),
        'unregistered': by_status.get('unregistered', 0),
        'disabled': by_status.get('disabled', 0),
        'last_sync_at': last_sync.isoformat(sep=' ') if last_sync else None,
    }


def resolve_project_id(company, project):
    """按 (劳务公司 short_name, 项目 title 或 short_name) 解析 project_id。
    找不到返回 None。优先匹配已注册项目。"""
    conn = connect('fish-test')
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT p.id
            FROM projects p
            JOIN enterprises e ON e.id = p.enterprise_id
            LEFT JOIN project_registrations pr ON pr.project_id = p.id
            WHERE e.short_name = %s AND (p.title = %s OR p.short_name = %s)
            ORDER BY (pr.status = 'registered') DESC, p.id DESC
            LIMIT 1
        """, (company, project, project))
        row = cur.fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def list_companies():
    """返回劳务公司下拉选项（按 short_name 字典序）。"""
    conn = connect('fish-test')
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT DISTINCT e.short_name
            FROM enterprises e
            JOIN projects p ON p.enterprise_id = e.id
            ORDER BY e.short_name
        """)
        return [r[0] for r in cur.fetchall()]
    finally:
        conn.close()


if __name__ == '__main__':
    import json
    print('--- sync_status ---')
    print(json.dumps(get_sync_status(), ensure_ascii=False, indent=2))
    print('--- companies ---')
    print(list_companies())
    print('--- projects (first 5) ---')
    for p in list_projects()[:5]:
        print(p)
