"""attendance 跨文件批量装载（方案 B 实现）

工作流：
  collector = AttendanceCollector()
  for fid in fids:
      ... parse sheet → rows ...
      collector.add(project_id, source_file_id, sheet_name, rows, ...)
  collector.flush(cur)   # 跨文件 dedup + executemany INSERT

去重规则（attendance）：
  natural_key = (project_id, name_raw, shift_date, shift_name 归一)
  winner = source_file_id 大者（=后上传文件覆盖先上传）
  shift_name 归一：None / 空字符串视为同一

汇总行（is_summary=True，attendance_summary 表）：
  natural_key = (project_id, name_raw, business_month)
  winner = source_file_id 大者
"""
import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from etl._utils import (bulk_get_or_create_workers, derive_business_month,
                        derive_business_period)


def _shift_key(s):
    """shift_name 归一：None / '' / 空白 → ''"""
    if s is None:
        return ''
    s = str(s).strip()
    return s


class AttendanceCollector:
    """收集多文件多 sheet 的 attendance rows，flush 时 dedup + 批量写"""

    def __init__(self):
        # entries: list[dict]，每条 = 一次 add 调用的上下文 + rows
        # 每条 entry: {
        #   'project_id', 'enterprise_id', 'business_cycle',
        #   'source_file_id', 'sheet_name', 'worker_class',
        #   'rows': [parser 输出的 row dict, ...]
        # }
        self.entries = []

    def add(self, *, project_id, enterprise_id, business_cycle,
            source_file_id, sheet_name, worker_class, rows):
        if not rows:
            return
        self.entries.append({
            'project_id': int(project_id),
            'enterprise_id': enterprise_id,
            'business_cycle': business_cycle,
            'source_file_id': source_file_id,
            'sheet_name': sheet_name,
            'worker_class': worker_class,
            'rows': rows,
        })

    def flush(self, cur):
        """跨 entries 按 natural_key dedup → INSERT executemany
        返回 {'inserted': N, 'dedup_dropped': M, 'summary_inserted': K, 'skipped_no_worker': S}
        """
        if not self.entries:
            return {'inserted': 0, 'dedup_dropped': 0,
                    'summary_inserted': 0, 'skipped_no_worker': 0}

        # 1. 解析每条 entry 的 rows → 拍平到 "candidate" 列表
        #    candidate: dict 含完整字段 + source_file_id + sheet_name + business_cycle 上下文
        candidates_daily = []  # is_summary=False 的行
        candidates_summary = []  # is_summary=True 的行
        names_per_pid = {}  # {project_id: set(name_raw)}
        for ent in self.entries:
            pid = ent['project_id']
            names_per_pid.setdefault(pid, set())
            for r in ent['rows']:
                name = (r.get('name_raw') or '').strip()
                if not name:
                    continue
                names_per_pid[pid].add(name)
                base = {
                    'project_id': pid,
                    'enterprise_id': ent['enterprise_id'],
                    'business_cycle': ent['business_cycle'],
                    'source_file_id': ent['source_file_id'],
                    'sheet_name': ent['sheet_name'],
                    'worker_class': ent['worker_class'],
                    'row': r,
                }
                if r.get('is_summary'):
                    candidates_summary.append(base)
                else:
                    candidates_daily.append(base)

        # 2. 批量建 worker（同项目一次性 bulk）
        worker_cache_per_pid = {}
        for pid, names in names_per_pid.items():
            worker_cache_per_pid[pid] = bulk_get_or_create_workers(
                cur, list(names), pid)

        # 3. dedup：daily 行
        #    key = (project_id, name_raw, shift_date, shift_name_normalized)
        #    winner = source_file_id 大者
        daily_winners = {}  # key → candidate
        n_drop_daily = 0
        for c in candidates_daily:
            r = c['row']
            sd = r.get('shift_date')
            if not sd:
                continue
            sn = _shift_key(r.get('shift_name'))
            name = (r.get('name_raw') or '').strip()
            key = (c['project_id'], name, sd, sn)
            cur_winner = daily_winners.get(key)
            if cur_winner is None:
                daily_winners[key] = c
            else:
                # 比较 source_file_id：大者胜
                if (c['source_file_id'] or 0) > (cur_winner['source_file_id'] or 0):
                    daily_winners[key] = c
                    n_drop_daily += 1  # 旧的被丢
                else:
                    n_drop_daily += 1  # 自己被丢

        # 4. dedup：summary 行
        #    key = (project_id, name_raw, business_month)
        summary_winners = {}
        n_drop_summary = 0
        for c in candidates_summary:
            r = c['row']
            bm = r.get('bm')
            if not bm:
                continue
            name = (r.get('name_raw') or '').strip()
            key = (c['project_id'], name, bm)
            cur_winner = summary_winners.get(key)
            if cur_winner is None:
                summary_winners[key] = c
            else:
                if (c['source_file_id'] or 0) > (cur_winner['source_file_id'] or 0):
                    summary_winners[key] = c
                    n_drop_summary += 1
                else:
                    n_drop_summary += 1

        # 5. 构建 INSERT batch
        att_batch = []
        sum_batch = []
        n_skip_worker = 0

        for c in daily_winners.values():
            r = c['row']
            wid = worker_cache_per_pid.get(c['project_id'], {}).get(
                (r.get('name_raw') or '').strip())
            if not wid:
                n_skip_worker += 1
                continue
            bm = derive_business_month(r['shift_date'], c['business_cycle'])
            if bm is None:
                n_skip_worker += 1
                continue
            ps, pe = derive_business_period(r['shift_date'], c['business_cycle'])
            extra = r.get('extra_data')
            extra_json = json.dumps(extra, ensure_ascii=False) if extra else None
            att_batch.append((
                c['enterprise_id'], c['project_id'], wid, bm, ps, pe,
                r['shift_date'], r.get('shift_name'),
                r.get('worker_type'), c['worker_class'], r.get('floor_or_group'),
                r['hours'], r.get('quantity'),
                'attendance_xlsx', c['source_file_id'],
                f"{c['sheet_name']}#R{r.get('row_idx', '?')}",
                r.get('name_raw'), r.get('id_card_raw'),
                int(r.get('from_bill', 0)),
                int(r.get('is_valid', 1)),
                r.get('invalid_reason'),
                extra_json,
            ))

        for c in summary_winners.values():
            r = c['row']
            wid = worker_cache_per_pid.get(c['project_id'], {}).get(
                (r.get('name_raw') or '').strip())
            if not wid:
                n_skip_worker += 1
                continue
            bm = r['bm']
            y, m = int(bm[:4]), int(bm[5:7])
            ps, pe = derive_business_period(date(y, m, 15), c['business_cycle'])
            extra = r.get('extra_data')
            extra_json = json.dumps(extra, ensure_ascii=False) if extra else None
            sum_batch.append((
                c['enterprise_id'], c['project_id'], wid, bm, ps, pe,
                r['hours'], r.get('quantity'),
                r.get('worker_type'), c['worker_class'], r.get('floor_or_group'),
                'attendance_xlsx_summary', c['source_file_id'],
                f"{c['sheet_name']}#R{r.get('row_idx', '?')}",
                r.get('name_raw'), None,
                int(r.get('from_bill', 0)),
                int(r.get('is_valid', 1)),
                r.get('invalid_reason'),
                extra_json,
            ))

        # 6. executemany INSERT
        if att_batch:
            cur.executemany("""INSERT INTO attendance
                (etl_batch_id, ingested_at, enterprise_id, project_id, worker_id,
                 business_month, business_period_start, business_period_end,
                 shift_date, shift_name, worker_type, worker_class, floor_or_group,
                 hours, quantity,
                 source_type, source_file_id, source_ref, name_raw, id_card_raw,
                 from_bill, is_valid, invalid_reason, extra_data)
                VALUES (0, NOW(), %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                att_batch)
        if sum_batch:
            cur.executemany("""INSERT INTO attendance_summary
                (etl_batch_id, ingested_at, enterprise_id, project_id, worker_id,
                 business_month, business_period_start, business_period_end,
                 hours, quantity, worker_type, worker_class, floor_or_group,
                 source_type, source_file_id, source_ref, name_raw, id_card_raw,
                 from_bill, is_valid, invalid_reason, extra_data)
                VALUES (0, NOW(), %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                sum_batch)

        return {
            'inserted': len(att_batch),
            'summary_inserted': len(sum_batch),
            'dedup_dropped': n_drop_daily + n_drop_summary,
            'dedup_dropped_daily': n_drop_daily,
            'dedup_dropped_summary': n_drop_summary,
            'skipped_no_worker': n_skip_worker,
        }
