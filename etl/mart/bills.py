"""mart bill_totals + bill_persons 持久化（schema v2 两表拆分）

bill_totals：每份账单一行（一个 source_file_id 对应一份）
bill_persons：账单人员金额（一份账单 N 行，按人）

按 feedback_mart_bills_scope：只存"明确账单"（业务方系统账单 / 部门小计累加 / 综合表 sum_amount_col）。
xlsx 派生值（hours×price）一律实时算不预存。
"""


def upsert_bill_total(cur, *, enterprise_id, project_id, business_month, amount,
                      source_type, source_file_id, source_ref):
    """每份账单一行
    - 同 (source_file_id + source_ref) 重复 parse 走 update
    - 跨 source 去重：同 (project_id, business_month, amount) 任意来源已存在 → skip
      避免同一份账单被多份上传（如 fid=270 Sheet1 + fid=271 原件）重复装入"""
    # 1. 同 source 更新
    cur.execute("""SELECT id FROM bill_totals
                   WHERE project_id=%s AND business_month=%s
                     AND source_file_id=%s AND COALESCE(source_ref,'')=COALESCE(%s,'')""",
                (project_id, business_month, source_file_id, source_ref))
    row = cur.fetchone()
    if row:
        cur.execute("""UPDATE bill_totals SET
                       amount=%s, source_type=%s, ingested_at=NOW()
                       WHERE id=%s""",
                    (amount, source_type, row[0]))
        return 'update'
    # 2. 跨 source 去重：同 bm + 同金额已存在 → skip（同一笔账单被重复上传）
    cur.execute("""SELECT id FROM bill_totals
                   WHERE project_id=%s AND business_month=%s
                     AND ABS(amount - %s) < 0.01""",
                (project_id, business_month, float(amount)))
    if cur.fetchone():
        return 'skip_dup_cross_source'
    cur.execute("""INSERT INTO bill_totals
                   (etl_batch_id, ingested_at, enterprise_id, project_id, business_month,
                    amount, source_type, source_file_id, source_ref)
                   VALUES (0, NOW(), %s, %s, %s, %s, %s, %s, %s)""",
                (enterprise_id, project_id, business_month,
                 amount, source_type, source_file_id, source_ref))
    return 'insert'


def upsert_bill_person(cur, *, enterprise_id, project_id, worker_id, business_month,
                       name_raw, amount,
                       source_type, source_file_id, source_ref,
                       is_valid=1, invalid_reason=None, extra_data=None):
    """账单人员金额：一行=一人在一份账单里。
    - 同 (source_file_id + source_ref + name_raw) 重复 parse 走 update
    - 跨 source 去重：同 (project_id, bm, name_raw, amount) 任意来源已存在 → skip
      避免同人同月同金额账单被多份上传重复装入"""
    import json as _json
    extra_json = _json.dumps(extra_data, ensure_ascii=False) if extra_data else None
    # 1. 同 source 更新
    cur.execute("""SELECT id FROM bill_persons
                   WHERE project_id=%s AND business_month=%s
                     AND name_raw=%s
                     AND source_file_id=%s AND COALESCE(source_ref,'')=COALESCE(%s,'')""",
                (project_id, business_month, name_raw, source_file_id, source_ref))
    row = cur.fetchone()
    if not row:
        # 2. 跨 source 去重：同人同月同金额已存在 → skip
        cur.execute("""SELECT id FROM bill_persons
                       WHERE project_id=%s AND business_month=%s
                         AND name_raw=%s AND ABS(amount - %s) < 0.01""",
                    (project_id, business_month, name_raw, float(amount or 0)))
        if cur.fetchone():
            return 'skip_dup_cross_source'
    if row:
        cur.execute("""UPDATE bill_persons SET
                       amount=%s, worker_id=%s,
                       source_type=%s, ingested_at=NOW(),
                       is_valid=%s, invalid_reason=%s, extra_data=%s
                       WHERE id=%s""",
                    (amount, worker_id, source_type,
                     int(is_valid), invalid_reason, extra_json, row[0]))
        return 'update'
    cur.execute("""INSERT INTO bill_persons
                   (etl_batch_id, ingested_at, enterprise_id, project_id, worker_id,
                    business_month, name_raw, amount,
                    source_type, source_file_id, source_ref,
                    is_valid, invalid_reason, extra_data)
                   VALUES (0, NOW(), %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (enterprise_id, project_id, worker_id, business_month,
                 name_raw, amount,
                 source_type, source_file_id, source_ref,
                 int(is_valid), invalid_reason, extra_json))
    return 'insert'
