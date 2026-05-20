"""payroll 跨文件批量装载（方案 B 实现）

工作流：
  collector = PayrollCollector()
  for fid in fids:
      ... parse sheet → rows（每行含 resolved biz_d / bm）...
      collector.add(project_id, source_file_id, sheet_name, rows)
  collector.flush(cur)

去重规则：
  natural_key = (project_id, name_raw, pay_time, work_amount)
  winner = source_file_id 大者

注意：
  - 不做 fish-prod-mini 优先合并（wipe_for_pull 已清，dispatcher 跑完 standardize 会
    再写 fish-prod-mini 流；两路并存时由 mart 层 unique key + INSERT IGNORE 决定）
  - 业务日期 (biz_d / bm) 由 parser 在 push 前根据 project_payroll_biz_date_rules 算好
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from etl._utils import bulk_get_or_create_workers


class PayrollCollector:
    """收集多文件 payroll rows，flush 时 dedup + 批量 INSERT IGNORE"""

    def __init__(self):
        # entries[i] = {
        #   project_id, enterprise_id, source_file_id, sheet_name,
        #   rows: [{name_raw, id_card_raw, pay_time, biz_d, bm, work_amount,
        #           payroll_kind, is_valid, invalid_reason, count_as_faxin, extra_data,
        #           row_idx}, ...]
        # }
        self.entries = []

    def add(self, *, project_id, enterprise_id, source_file_id, sheet_name, rows):
        if not rows:
            return
        self.entries.append({
            'project_id': int(project_id),
            'enterprise_id': enterprise_id,
            'source_file_id': source_file_id,
            'sheet_name': sheet_name,
            'rows': rows,
        })

    def flush(self, cur):
        if not self.entries:
            return {'inserted': 0, 'dedup_dropped': 0, 'skipped_no_worker': 0}

        # 收集候选 + worker 名集
        candidates = []  # (entry, row)
        names_per_pid = {}  # {pid: set((name, id_card or ''))}
        for ent in self.entries:
            pid = ent['project_id']
            names_per_pid.setdefault(pid, set())
            for r in ent['rows']:
                name = (r.get('name_raw') or '').strip()
                if not name:
                    continue
                # bulk_get_or_create_workers 当前只按 name 缓存；id_card 维度分开走
                names_per_pid[pid].add(name)
                candidates.append((ent, r))

        # dedup：(project_id, name_raw, pay_time, work_amount) → latest source_file_id
        winners = {}
        n_drop = 0
        for ent, r in candidates:
            name = (r.get('name_raw') or '').strip()
            pt = r.get('pay_time')
            amt = r.get('work_amount')
            if not name or pt is None or amt is None:
                continue
            try:
                amt_f = float(amt)
            except (TypeError, ValueError):
                continue
            key = (ent['project_id'], name, pt, amt_f)
            cur_w = winners.get(key)
            cand = (ent, r)
            if cur_w is None:
                winners[key] = cand
            else:
                if (ent['source_file_id'] or 0) > (cur_w[0]['source_file_id'] or 0):
                    winners[key] = cand
                    n_drop += 1
                else:
                    n_drop += 1

        # 批量建 worker
        worker_cache_per_pid = {}
        for pid, names in names_per_pid.items():
            worker_cache_per_pid[pid] = bulk_get_or_create_workers(
                cur, list(names), pid)

        # 构 INSERT batch
        batch = []
        n_skip_worker = 0
        for key, (ent, r) in winners.items():
            pid = ent['project_id']
            name = (r.get('name_raw') or '').strip()
            wid = worker_cache_per_pid.get(pid, {}).get(name)
            if not wid:
                n_skip_worker += 1
                continue
            extra = r.get('extra_data')
            extra_json = json.dumps(extra, ensure_ascii=False) if extra else None
            batch.append((
                ent['enterprise_id'], pid, wid,
                r.get('bm'), r.get('pay_time'), r.get('biz_d'),
                r.get('work_amount'),
                r.get('payroll_kind') or 'shift_dated',
                ent['source_file_id'],
                f"{ent['sheet_name']}#R{r.get('row_idx', '?')}",
                name, r.get('id_card_raw'),
                int(r.get('is_valid', 1)), r.get('invalid_reason'),
                int(r.get('count_as_faxin', 1)), extra_json,
            ))

        if batch:
            cur.executemany("""INSERT IGNORE INTO payrolls
                (etl_batch_id, ingested_at, enterprise_id, project_id, worker_id,
                 business_month, pay_time, parsed_shift_date,
                 work_amount, payroll_kind, alipay_status,
                 source_type, source_file_id, source_ref, name_raw, id_card_raw,
                 is_valid, invalid_reason, count_as_faxin, extra_data)
                VALUES (0, NOW(), %s, %s, %s, %s, %s, %s, %s, %s, NULL,
                        'xlsx_payroll', %s, %s, %s, %s, %s, %s, %s, %s)""",
                batch)
            n_ins = cur.rowcount
        else:
            n_ins = 0

        return {
            'inserted': n_ins,
            'dedup_dropped': n_drop,
            'skipped_no_worker': n_skip_worker,
        }
