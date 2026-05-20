"""wage_sheet 跨文件批量装载（方案 B 实现）

工作流：
  collector = WageCollector()
  for fid in fids:
      ... parse sheet → person_rows + bm ...
      collector.add(project_id, source_file_id, sheet_name, business_month, person_rows)
  collector.flush(cur)

去重规则：
  natural_key = (project_id, name_raw, business_month)
  winner = source_file_id 大者
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from etl._utils import bulk_get_or_create_workers


class WageCollector:
    """收集多文件 wage_sheet rows，flush 时 dedup + 批量 INSERT"""

    def __init__(self):
        # entries[i] = {
        #   project_id, enterprise_id, source_file_id, sheet_name, business_month,
        #   person_rows: [{name_raw, payable_amount, is_substitute, substitute_name,
        #                  is_valid, invalid_reason, extra_data, row_idx}, ...]
        # }
        self.entries = []

    def add(self, *, project_id, enterprise_id, source_file_id, sheet_name,
            business_month, person_rows):
        if not person_rows:
            return
        self.entries.append({
            'project_id': int(project_id),
            'enterprise_id': enterprise_id,
            'source_file_id': source_file_id,
            'sheet_name': sheet_name,
            'business_month': business_month,
            'person_rows': person_rows,
        })

    def flush(self, cur):
        if not self.entries:
            return {'inserted': 0, 'dedup_dropped': 0, 'skipped_no_worker': 0}

        # dedup：(project_id, name_raw, business_month) → latest source_file_id
        winners = {}
        n_drop = 0
        names_per_pid = {}
        for ent in self.entries:
            pid = ent['project_id']
            bm = ent['business_month']
            names_per_pid.setdefault(pid, set())
            for r in ent['person_rows']:
                name = (r.get('name_raw') or '').strip()
                if not name:
                    continue
                names_per_pid[pid].add(name)
                key = (pid, name, bm)
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
        for (pid, name, bm), (ent, r) in winners.items():
            wid = worker_cache_per_pid.get(pid, {}).get(name)
            if not wid:
                n_skip_worker += 1
                continue
            extra = r.get('extra_data')
            extra_json = json.dumps(extra, ensure_ascii=False) if extra else None
            batch.append((
                ent['enterprise_id'], pid, wid, bm,
                r.get('payable_amount'),
                int(r.get('is_substitute', 0)),
                r.get('substitute_name'),
                'xirui_wage_sheet_xlsx', ent['source_file_id'],
                f"{ent['sheet_name']}#R{r.get('row_idx', '?')}",
                name,
                int(r.get('is_valid', 1)),
                r.get('invalid_reason'),
                extra_json,
            ))

        if batch:
            cur.executemany("""INSERT INTO wage_sheets
                (etl_batch_id, ingested_at, enterprise_id, project_id, worker_id,
                 business_month, payable_amount, is_substitute, substitute_name,
                 source_type, source_file_id, source_ref, name_raw,
                 is_valid, invalid_reason, extra_data)
                VALUES (0, NOW(), %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                batch)

        return {
            'inserted': len(batch),
            'dedup_dropped': n_drop,
            'skipped_no_worker': n_skip_worker,
        }
