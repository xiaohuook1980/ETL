"""mart wage_sheets 持久化（upsert 单一函数）

唯一性 = (project_id, business_month, source_file_id, source_ref)
  → 同一工人在同一月内可有多条（如分上半月/下半月装入），下游用 SUM(payable_amount) 合并。
对齐 schema v2：删除 department / hours / id_card_raw（劳务瞎编无意义）。
"""


def upsert_wage_sheet(cur, *, enterprise_id, project_id, worker_id, business_month,
                      payable_amount,
                      is_substitute=0, substitute_name=None,
                      source_type, source_file_id, source_ref, name_raw,
                      is_valid=1, invalid_reason=None, extra_data=None):
    import json as _json
    extra_json = _json.dumps(extra_data, ensure_ascii=False) if extra_data else None
    cur.execute("""SELECT id FROM wage_sheets
                   WHERE project_id=%s AND business_month=%s
                     AND source_file_id=%s AND source_ref=%s""",
                (project_id, business_month, source_file_id, source_ref))
    row = cur.fetchone()
    if row:
        cur.execute("""UPDATE wage_sheets SET
                       payable_amount=%s,
                       is_substitute=%s, substitute_name=%s,
                       source_type=%s,
                       worker_id=%s, name_raw=%s, ingested_at=NOW(),
                       is_valid=%s, invalid_reason=%s, extra_data=%s
                       WHERE id=%s AND business_month=%s""",
                    (payable_amount, is_substitute, substitute_name,
                     source_type, worker_id, name_raw,
                     int(is_valid), invalid_reason, extra_json, row[0], business_month))
        return 'update'
    cur.execute("""INSERT INTO wage_sheets
                   (etl_batch_id, ingested_at, enterprise_id, project_id, worker_id,
                    business_month, payable_amount,
                    is_substitute, substitute_name,
                    source_type, source_file_id, source_ref, name_raw,
                    is_valid, invalid_reason, extra_data)
                   VALUES (0, NOW(), %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (enterprise_id, project_id, worker_id, business_month, payable_amount,
                 is_substitute, substitute_name,
                 source_type, source_file_id, source_ref, name_raw,
                 int(is_valid), invalid_reason, extra_json))
    return 'insert'
