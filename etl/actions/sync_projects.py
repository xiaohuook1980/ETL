"""项目/企业/实控人同步（fish-prod → fish-test）

老库（fish-prod）→ 新库（fish-test）的单向只读同步。
- sync_full(): 全量重刷（不删旧的，只 upsert）
- sync_incremental(since_date): 仅拉老库 mini_project.create_time >= since_date 的项目

同步范围：
  enterprises（biz_enterprise）→ enterprises
  mini_project (mark=1)        → projects + project_registrations(default 'unregistered')
  mini_pre_project.actual_ctr_id + mini_actual_ctr → controllers
  以上关系 → controller_enterprise_map

UI 注册可改字段（business_cycle / daishou_threshold / profit_ratio）
  - 同步首次插入：default '自然月' / 2000 / mini_project.proportion(若有)
  - 已存在记录：保留用户已编辑值（不覆盖 daishou_threshold / profit_ratio）
  - business_cycles 表完全由用户注册写入，sync 不触碰
"""
import sys
from datetime import datetime, date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts._db import connect


def _truncate(s, n):
    if s is None:
        return None
    s = str(s)
    return s[:n] if len(s) > n else s


def _upsert_enterprise(dc, ent_id, full_name, unified_credit_code):
    """企业 upsert：存在则更新 full_name/unified_credit_code，新插入则用 full_name 截断 60 作 short_name。

    UNIQUE 冲突短名：fallback 加 _<id> 后缀。
    """
    dc.execute("SELECT id FROM enterprises WHERE id=%s", (ent_id,))
    if dc.fetchone():
        dc.execute("""
            UPDATE enterprises
            SET full_name=%s, unified_credit_code=%s
            WHERE id=%s
        """, (full_name, unified_credit_code, ent_id))
        return False  # 不是新增

    short_name = _truncate(full_name or f'enterprise_{ent_id}', 60)
    try:
        dc.execute("""
            INSERT INTO enterprises (id, full_name, short_name, unified_credit_code, status)
            VALUES (%s, %s, %s, %s, 'active')
        """, (ent_id, full_name, short_name, unified_credit_code))
    except Exception:
        short_name = _truncate(f'{full_name}_{ent_id}', 60) if full_name else f'enterprise_{ent_id}'
        dc.execute("""
            INSERT INTO enterprises (id, full_name, short_name, unified_credit_code, status)
            VALUES (%s, %s, %s, %s, 'active')
        """, (ent_id, full_name, short_name, unified_credit_code))
    return True  # 新增


def _upsert_controller(dc, ctrl_id, name, id_card, mobile):
    """实控人 upsert：以 id 为主键。脱敏/异常 idcard 视为 NULL（避免 UNIQUE 冲突）。"""
    if id_card and (len(str(id_card)) != 18 or '*' in str(id_card)):
        id_card = None

    dc.execute("SELECT id FROM controllers WHERE id=%s", (ctrl_id,))
    if dc.fetchone():
        dc.execute("""
            UPDATE controllers SET name=%s, id_card=%s, mobile=%s WHERE id=%s
        """, (name, id_card, mobile, ctrl_id))
        return False

    try:
        dc.execute("""
            INSERT INTO controllers (id, name, id_card, mobile)
            VALUES (%s, %s, %s, %s)
        """, (ctrl_id, name, id_card, mobile))
    except Exception:
        dc.execute("""
            INSERT INTO controllers (id, name, id_card, mobile)
            VALUES (%s, %s, NULL, %s)
        """, (ctrl_id, name, mobile))
    return True


def _link_controller_enterprise(dc, controller_id, enterprise_id):
    dc.execute("""
        INSERT INTO controller_enterprise_map (controller_id, enterprise_id, role)
        VALUES (%s, %s, '实控人')
        ON DUPLICATE KEY UPDATE role='实控人'
    """, (controller_id, enterprise_id))


def _upsert_project(dc, p, default_profit=0.800):
    """项目 upsert。已存在的项目不覆盖 daishou_threshold / profit_ratio（保留用户编辑）。

    p 是从 fish-prod 取的 dict：
      id / pre_id / sid(enterprise_id) / project_title / proportion / create_time
    """
    dc.execute("SELECT id FROM projects WHERE id=%s", (p['id'],))
    exists = dc.fetchone() is not None

    proportion = p.get('proportion')
    init_profit = float(proportion) if proportion not in (None, 0) else default_profit

    if exists:
        # 仅更新同步可信字段，保留用户编辑
        dc.execute("""
            UPDATE projects
            SET pre_id=%s, source_created_at=%s, enterprise_id=%s, title=%s
            WHERE id=%s
        """, (p['pre_id'], p['create_time'], p['enterprise_id'],
              _truncate(p['project_title'], 128), p['id']))
        return False

    short_name = _truncate(p['project_title'] or f'project_{p["id"]}', 60)
    try:
        dc.execute("""
            INSERT INTO projects (
                id, pre_id, source_created_at, enterprise_id, title, short_name,
                finance_mode, business_cycle, profit_ratio, daishou_threshold, status
            ) VALUES (%s, %s, %s, %s, %s, %s, 'normal', '自然月', %s, 2000, 'active')
        """, (p['id'], p['pre_id'], p['create_time'], p['enterprise_id'],
              _truncate(p['project_title'], 128), short_name, init_profit))
    except Exception:
        short_name = _truncate(f'{p["project_title"]}_{p["id"]}', 60)
        dc.execute("""
            INSERT INTO projects (
                id, pre_id, source_created_at, enterprise_id, title, short_name,
                finance_mode, business_cycle, profit_ratio, daishou_threshold, status
            ) VALUES (%s, %s, %s, %s, %s, %s, 'normal', '自然月', %s, 2000, 'active')
        """, (p['id'], p['pre_id'], p['create_time'], p['enterprise_id'],
              _truncate(p['project_title'], 128), short_name, init_profit))

    dc.execute("""
        INSERT IGNORE INTO project_registrations (project_id, status)
        VALUES (%s, 'unregistered')
    """, (p['id'],))
    return True


def _do_sync(since_date=None, enterprise_keyword=None, project_keyword=None):
    """共享同步主流程。

    过滤参数（可任选其一/组合）：
      since_date：mini_project.create_time >= since_date
      enterprise_keyword：biz_enterprise.title LIKE %keyword%
      project_keyword：mini_project.project_title LIKE %keyword%
    全为 None 即全量。
    """
    src = connect('fish-prod')
    dst = connect('fish-test')

    counters = {
        'enterprises_inserted': 0,
        'enterprises_updated': 0,
        'projects_inserted': 0,
        'projects_updated': 0,
        'controllers_inserted': 0,
        'controllers_updated': 0,
        'maps_added': 0,
    }

    try:
        sc = src.cursor()
        dc = dst.cursor()

        need_ent_join = bool(enterprise_keyword)
        sql = """
        SELECT mp.id, mp.sid AS enterprise_id, mp.project_title, mp.pre_project_id AS pre_id,
               mp.proportion, mp.create_time
        FROM mini_project mp
        """
        if need_ent_join:
            sql += " JOIN biz_enterprise be ON be.id = mp.sid "
        sql += " WHERE mp.mark = 1 AND mp.sid IS NOT NULL AND mp.if_approval = 1 "
        # if_approval=1: 已审批 → 小鱼系统前端可见；=2 是未审批草稿/测试，不拉
        args = []
        if since_date is not None:
            sql += " AND mp.create_time >= %s"
            args.append(since_date)
        if enterprise_keyword:
            sql += " AND be.title LIKE %s"
            args.append(f'%{enterprise_keyword}%')
        if project_keyword:
            sql += " AND mp.project_title LIKE %s"
            args.append(f'%{project_keyword}%')
        sql += " ORDER BY mp.id"
        sc.execute(sql, args)
        proj_cols = [d[0] for d in sc.description]
        projects_src = [dict(zip(proj_cols, r)) for r in sc.fetchall()]

        ent_ids = sorted({p['enterprise_id'] for p in projects_src})
        if ent_ids:
            placeholders = ','.join(['%s'] * len(ent_ids))
            sc.execute(f"""
                SELECT id, title, unified_credit_code
                FROM biz_enterprise
                WHERE id IN ({placeholders})
            """, ent_ids)
            for ent_id, title, ucc in sc.fetchall():
                inserted = _upsert_enterprise(dc, ent_id, title, ucc)
                counters['enterprises_inserted' if inserted else 'enterprises_updated'] += 1

        for p in projects_src:
            inserted = _upsert_project(dc, p)
            counters['projects_inserted' if inserted else 'projects_updated'] += 1

        pre_ids = sorted({p['pre_id'] for p in projects_src if p['pre_id']})
        ctrl_ids = set()
        ent_to_ctrl = {}
        if pre_ids:
            placeholders = ','.join(['%s'] * len(pre_ids))
            sc.execute(f"""
                SELECT pp.id AS pre_id, pp.actual_ctr_id, mp.sid AS enterprise_id
                FROM mini_pre_project pp
                JOIN mini_project mp ON mp.pre_project_id = pp.id
                WHERE pp.id IN ({placeholders}) AND pp.actual_ctr_id IS NOT NULL
            """, pre_ids)
            for _, actual_ctr_id, enterprise_id in sc.fetchall():
                ctrl_ids.add(actual_ctr_id)
                ent_to_ctrl[enterprise_id] = actual_ctr_id

        if ctrl_ids:
            placeholders = ','.join(['%s'] * len(ctrl_ids))
            sc.execute(f"""
                SELECT id, user_name, idcard, mobile
                FROM mini_actual_ctr
                WHERE id IN ({placeholders}) AND mark=1
            """, list(ctrl_ids))
            for ctrl_id, name, id_card, mobile in sc.fetchall():
                if not name:
                    continue
                inserted = _upsert_controller(dc, ctrl_id, name, id_card, mobile)
                counters['controllers_inserted' if inserted else 'controllers_updated'] += 1

        for ent_id, ctrl_id in ent_to_ctrl.items():
            dc.execute("""
                SELECT 1 FROM controller_enterprise_map
                WHERE controller_id=%s AND enterprise_id=%s
            """, (ctrl_id, ent_id))
            if not dc.fetchone():
                _link_controller_enterprise(dc, ctrl_id, ent_id)
                counters['maps_added'] += 1

        dst.commit()
    finally:
        src.close()
        dst.close()

    return counters


def sync_full():
    """全量同步老库所有项目/企业/实控人到 fish-test。"""
    return _do_sync(since_date=None)


def sync_incremental(since_date):
    """增量同步：仅拉 mini_project.create_time >= since_date 的项目（含其企业/实控人）。

    Args:
        since_date: 'YYYY-MM-DD' 字符串 或 date/datetime 对象
    """
    if isinstance(since_date, str):
        since_date = datetime.strptime(since_date, '%Y-%m-%d').date()
    return _do_sync(since_date=since_date)


def sync_one(enterprise_keyword=None, project_keyword=None):
    """按劳务公司+项目名关键字拉单个/多个项目（含其企业/实控人）。

    两个关键字至少有一个非空，均为 LIKE %keyword% 子串匹配。
    """
    e = (enterprise_keyword or '').strip()
    p = (project_keyword or '').strip()
    if not e and not p:
        raise ValueError('劳务公司 / 项目名 至少填一个')
    return _do_sync(enterprise_keyword=e or None, project_keyword=p or None)


if __name__ == '__main__':
    import json
    if len(sys.argv) > 1 and sys.argv[1] == 'incremental':
        d = sys.argv[2] if len(sys.argv) > 2 else '2026-05-01'
        print(f'sync_incremental(since={d}) ...')
        print(json.dumps(sync_incremental(d), ensure_ascii=False, indent=2))
    else:
        print('sync_full() ...')
        print(json.dumps(sync_full(), ensure_ascii=False, indent=2))
