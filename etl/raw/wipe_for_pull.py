"""业务月范围内强制清重建：上游有任何变化都按"清空+重拉"处理

跑在 pull_data step 1 之后（raw_mini_a_bill 已镜像上游最新状态），step 2 之前。

清理范围（project_id + business_month 双限定）：
  1. mart 6 表：WHERE project_id=P AND business_month=M → DELETE
  2. raw_files：source_bill_ids ∩ {本业务月账单 ID 集合} ≠ ∅ 的 raw_file
       - 从 source_bill_ids 移除属于本业务月的 bill_id
       - 从 source_urls/source_filenames 按位剥离对应条目
       - 若 source_bill_ids 剥光 且 source_project_ids 只剩本项目 → DELETE 整行
       - 否则 UPDATE（保留多项目/跨月共享）

ai 桶物理文件不动（保留作审计）。

理由：用户指示"一个项目的数据不会很多"，无须做"已删除 URL 检测"——
直接清空 + 全量重拉更鲁棒，避免 dedup/超集/补丁等隐性 bug 累积。
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts._db import connect


_MART_TABLES = ('attendance', 'attendance_summary', 'bill_totals', 'bill_persons',
                'payrolls', 'wage_sheets')


def _to_list(v):
    if v is None:
        return []
    if isinstance(v, list):
        return v
    if isinstance(v, str):
        try:
            return json.loads(v)
        except (ValueError, TypeError):
            return []
    return []


def _scope_bill_ids(cur, project_id, business_month):
    """从 raw_mini_a_bill 取该业务月对应的 bill_id 集合"""
    cur.execute("""SELECT id FROM raw_mini_a_bill
                   WHERE project_id=%s
                     AND (bill_month=%s OR bill_month LIKE %s)""",
                (project_id, business_month, f'{business_month}%'))
    return {r[0] for r in cur.fetchall()}


def wipe_business_month(project_id, business_month, dry_run=False):
    """强制清空 (project_id, business_month) 范围内的 mart + raw_files 痕迹。

    返回 {'mart_deleted': {table: rows}, 'raw_files_deleted': N,
          'raw_files_detached': M, 'detail': [...]}
    """
    if not business_month:
        raise ValueError('business_month 必填')
    project_id = int(project_id)
    conn = connect('fish-test')
    cur = conn.cursor()
    result = {
        'project_id': str(project_id),
        'business_month': business_month,
        'dry_run': bool(dry_run),
        'mart_deleted': {},
        'raw_files_deleted': 0,
        'raw_files_detached': 0,
        'detail': [],
    }
    try:
        scope_bill_ids = _scope_bill_ids(cur, project_id, business_month)

        # ===== 1. mart 6 表强制 DELETE =====
        for t in _MART_TABLES:
            if dry_run:
                cur.execute(f"SELECT COUNT(*) FROM {t} WHERE project_id=%s AND business_month=%s",
                            (project_id, business_month))
                result['mart_deleted'][t] = cur.fetchone()[0]
            else:
                cur.execute(f"DELETE FROM {t} WHERE project_id=%s AND business_month=%s",
                            (project_id, business_month))
                result['mart_deleted'][t] = cur.rowcount

        # ===== 2. raw_files 剥离/删除 =====
        if scope_bill_ids:
            # 兼容 5.7：先按 source_project_ids 含本项目筛 → Python 端做 bill_id 交集
            cur.execute("""
                SELECT id, source_urls, source_filenames, source_bill_ids, source_project_ids
                FROM raw_files
                WHERE JSON_CONTAINS(source_project_ids, CAST(%s AS JSON))
            """, (json.dumps(project_id),))
            raw_candidates = cur.fetchall()
            candidates = [r for r in raw_candidates
                          if any(b in scope_bill_ids for b in _to_list(r[3]))]

            # 关键修复：删 raw_files 前，先把它装入的所有 mart 行清掉（跨业务月）
            # 否则 raw_file 删了但 mart 残留，下次 pull 装新 raw_file 会跟残留共存（重复）
            # 仅对 action='delete' 的 raw_files 做（detach 的 raw_file 还在，mart 不能动）
            to_delete_fids = []
            for rid, urls_j, fnames_j, bills_j, projs_j in candidates:
                bills = _to_list(bills_j)
                projs = _to_list(projs_j)
                keep_mask = [b not in scope_bill_ids for b in bills]
                new_bills = [b for b, k in zip(bills, keep_mask) if k]
                if not new_bills:
                    new_projs = [p for p in projs if int(p) != project_id]
                    if not new_projs:
                        to_delete_fids.append(rid)
            if to_delete_fids and not dry_run:
                placeholders = ','.join(['%s'] * len(to_delete_fids))
                for t in _MART_TABLES:
                    cur.execute(f"DELETE FROM {t} WHERE project_id=%s "
                                f"AND source_file_id IN ({placeholders})",
                                [project_id] + to_delete_fids)
                    n = cur.rowcount
                    if n:
                        result['mart_deleted'][t] = result['mart_deleted'].get(t, 0) + n

            for rid, urls_j, fnames_j, bills_j, projs_j in candidates:
                urls = _to_list(urls_j)
                fnames = _to_list(fnames_j)
                bills = _to_list(bills_j)
                projs = _to_list(projs_j)

                # 计算剥离后的字段（按 bills 索引同步剥 urls/filenames，若长度对齐）
                keep_mask = [b not in scope_bill_ids for b in bills]
                new_bills = [b for b, k in zip(bills, keep_mask) if k]
                if len(urls) == len(bills):
                    new_urls = [u for u, k in zip(urls, keep_mask) if k]
                else:
                    new_urls = urls  # 长度不齐时不动，避免破坏
                if len(fnames) == len(bills):
                    new_fnames = [f for f, k in zip(fnames, keep_mask) if k]
                else:
                    new_fnames = fnames

                # source_project_ids：若 bills 全剥光，本项目对该 rf 的引用结束
                if not new_bills:
                    new_projs = [p for p in projs if int(p) != project_id]
                else:
                    new_projs = projs

                if not new_projs:
                    if not dry_run:
                        cur.execute("DELETE FROM raw_files WHERE id=%s", (rid,))
                    result['raw_files_deleted'] += 1
                    action = 'delete'
                else:
                    if not dry_run:
                        cur.execute("""UPDATE raw_files SET
                                       source_urls=%s, source_filenames=%s,
                                       source_bill_ids=%s, source_project_ids=%s
                                       WHERE id=%s""",
                                    (json.dumps(new_urls, ensure_ascii=False),
                                     json.dumps(new_fnames, ensure_ascii=False),
                                     json.dumps(new_bills),
                                     json.dumps(new_projs),
                                     rid))
                    result['raw_files_detached'] += 1
                    action = 'detach'

                result['detail'].append({
                    'raw_file_id': rid,
                    'action': action,
                    'filenames': fnames,
                    'bills_in_scope': [b for b in bills if b in scope_bill_ids],
                })

        if not dry_run:
            conn.commit()
        return result
    finally:
        conn.close()


if __name__ == '__main__':
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument('--project-id', type=int, required=True)
    ap.add_argument('--business-month', required=True,
                    help='YYYY-MM；强制清空该业务月范围')
    ap.add_argument('--commit', action='store_true',
                    help='不传 = dry-run；加 --commit 才真删')
    args = ap.parse_args()
    r = wipe_business_month(args.project_id, args.business_month,
                             dry_run=not args.commit)
    print(json.dumps(r, ensure_ascii=False, indent=2, default=str))
