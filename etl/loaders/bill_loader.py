"""bill 跨文件批量装载（方案 B 实现）

bill 写两张表：
  - bill_totals：一项目一业务月一行（同月多文件 → 后到 source_file_id 胜）
                聚合规则命中时 amount 由 agg_total 覆盖
  - bill_persons：按 (project_id, business_month, name_raw) 去重，后到胜

工作流：
  collector = BillCollector()
  for fid in fids:
      ... parse sheet → person_rows + (optional) agg_total ...
      collector.add(project_id, source_file_id, sheet_name, business_month,
                     person_rows, bill_total_amount, bill_total_src_type, bill_total_src_ref)
  collector.flush(cur)
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from etl._utils import bulk_get_or_create_workers


class BillCollector:
    """收集多文件多 sheet 的 bill rows，flush 时 dedup + 批量写"""

    def __init__(self):
        # entries[i] = {
        #   project_id, enterprise_id, source_file_id, sheet_name, business_month,
        #   person_rows: [{name_raw, amount, is_valid, invalid_reason, extra_data, row_idx}, ...],
        #   bill_total_amount, bill_total_src_type, bill_total_src_ref,
        # }
        self.entries = []

    def add(self, *, project_id, enterprise_id, source_file_id, sheet_name,
            business_month, person_rows, bill_total_amount, bill_total_src_type,
            bill_total_src_ref):
        self.entries.append({
            'project_id': int(project_id),
            'enterprise_id': enterprise_id,
            'source_file_id': source_file_id,
            'sheet_name': sheet_name,
            'business_month': business_month,
            'person_rows': person_rows or [],
            'bill_total_amount': bill_total_amount,
            'bill_total_src_type': bill_total_src_type,
            'bill_total_src_ref': bill_total_src_ref,
        })

    def flush(self, cur):
        if not self.entries:
            return {'totals_inserted': 0, 'persons_inserted': 0,
                    'totals_dedup_dropped': 0, 'persons_dedup_dropped': 0,
                    'skipped_no_worker': 0}

        # ============================================================
        # 1. bill_totals dedup：(project_id, business_month) → latest source_file_id
        # ============================================================
        total_winners = {}  # key=(pid, bm) → entry
        n_drop_total = 0
        for ent in self.entries:
            if ent['bill_total_amount'] is None:
                continue
            key = (ent['project_id'], ent['business_month'])
            cur_w = total_winners.get(key)
            if cur_w is None:
                total_winners[key] = ent
            else:
                if (ent['source_file_id'] or 0) > (cur_w['source_file_id'] or 0):
                    total_winners[key] = ent
                    n_drop_total += 1
                else:
                    n_drop_total += 1

        # ============================================================
        # 2. bill_persons dedup：(project_id, business_month, name_raw) → latest source_file_id
        # ============================================================
        person_winners = {}  # key=(pid, bm, name_raw) → (entry, row)
        n_drop_person = 0
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
                key = (pid, bm, name)
                cur_w = person_winners.get(key)
                cand = (ent, r)
                if cur_w is None:
                    person_winners[key] = cand
                else:
                    if (ent['source_file_id'] or 0) > (cur_w[0]['source_file_id'] or 0):
                        person_winners[key] = cand
                        n_drop_person += 1
                    else:
                        n_drop_person += 1

        # ============================================================
        # 3. 批量建 worker
        # ============================================================
        worker_cache_per_pid = {}
        for pid, names in names_per_pid.items():
            worker_cache_per_pid[pid] = bulk_get_or_create_workers(
                cur, list(names), pid)

        # ============================================================
        # 4. 写 bill_totals（INSERT，wipe 已清空）
        # ============================================================
        total_batch = []
        for ent in total_winners.values():
            total_batch.append((
                ent['enterprise_id'], ent['project_id'], ent['business_month'],
                round(float(ent['bill_total_amount']), 2),
                ent['bill_total_src_type'], ent['source_file_id'],
                ent['bill_total_src_ref'],
            ))
        if total_batch:
            cur.executemany("""INSERT INTO bill_totals
                (etl_batch_id, ingested_at, enterprise_id, project_id,
                 business_month, amount, source_type, source_file_id, source_ref)
                VALUES (0, NOW(), %s, %s, %s, %s, %s, %s, %s)""",
                total_batch)

        # ============================================================
        # 5. 写 bill_persons
        # ============================================================
        person_batch = []
        n_skip_worker = 0
        for (pid, bm, name), (ent, r) in person_winners.items():
            wid = worker_cache_per_pid.get(pid, {}).get(name)
            if not wid:
                n_skip_worker += 1
                continue
            extra = r.get('extra_data')
            extra_json = json.dumps(extra, ensure_ascii=False) if extra else None
            person_batch.append((
                ent['enterprise_id'], pid, wid, bm,
                name, r.get('amount'),
                'person_actual', ent['source_file_id'],
                f"{ent['sheet_name']}#R{r.get('row_idx', '?')}",
                int(r.get('is_valid', 1)),
                r.get('invalid_reason'),
                extra_json,
            ))
        if person_batch:
            cur.executemany("""INSERT INTO bill_persons
                (etl_batch_id, ingested_at, enterprise_id, project_id, worker_id,
                 business_month, name_raw, amount,
                 source_type, source_file_id, source_ref,
                 is_valid, invalid_reason, extra_data)
                VALUES (0, NOW(), %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                person_batch)

        return {
            'totals_inserted': len(total_batch),
            'persons_inserted': len(person_batch),
            'totals_dedup_dropped': n_drop_total,
            'persons_dedup_dropped': n_drop_person,
            'skipped_no_worker': n_skip_worker,
        }
