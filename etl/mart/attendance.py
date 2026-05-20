"""mart attendance 持久化（upsert 单一函数）

按 (project_id, worker_id, business_month, shift_date, shift_name) 唯一性 upsert。
对齐 schema v2：含 quantity（计件项目）+ worker_type（工种）。
"""


def upsert_attendance(cur, *, enterprise_id, project_id, worker_id, business_month,
                      shift_date, shift_name, worker_type, floor_or_group,
                      hours, quantity,
                      source_type, source_file_id, source_ref,
                      name_raw, id_card_raw=None,
                      business_period_start=None, business_period_end=None,
                      worker_class=None):
    cur.execute("""SELECT id FROM attendance
                   WHERE project_id=%s AND worker_id=%s
                     AND shift_date=%s AND COALESCE(shift_name,'')=COALESCE(%s,'')
                     AND business_month=%s""",
                (project_id, worker_id, shift_date, shift_name, business_month))
    row = cur.fetchone()
    if row:
        cur.execute("""UPDATE attendance SET
                       business_period_start=%s, business_period_end=%s,
                       worker_type=%s, worker_class=%s, floor_or_group=%s,
                       hours=%s, quantity=%s,
                       source_type=%s, source_file_id=%s, source_ref=%s,
                       name_raw=%s, id_card_raw=%s, ingested_at=NOW()
                       WHERE id=%s AND business_month=%s""",
                    (business_period_start, business_period_end,
                     worker_type, worker_class, floor_or_group, hours, quantity,
                     source_type, source_file_id, source_ref,
                     name_raw, id_card_raw, row[0], business_month))
        return 'update'
    cur.execute("""INSERT INTO attendance
                   (etl_batch_id, ingested_at, enterprise_id, project_id, worker_id,
                    business_month, business_period_start, business_period_end,
                    shift_date, shift_name, worker_type, worker_class, floor_or_group,
                    hours, quantity,
                    source_type, source_file_id, source_ref,
                    name_raw, id_card_raw)
                   VALUES (0, NOW(), %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (enterprise_id, project_id, worker_id, business_month,
                 business_period_start, business_period_end,
                 shift_date, shift_name, worker_type, worker_class, floor_or_group,
                 hours, quantity,
                 source_type, source_file_id, source_ref,
                 name_raw, id_card_raw))
    return 'insert'


def upsert_attendance_summary(cur, *, enterprise_id, project_id, worker_id, business_month,
                               hours, quantity, worker_type, floor_or_group,
                               source_type, source_file_id, source_ref,
                               name_raw, id_card_raw=None,
                               business_period_start=None, business_period_end=None,
                               worker_class=None):
    """月汇总考勤上托：(project_id, worker_id, business_month, source_file_id) 唯一。
    甲方酒店类账单常给"姓名+月总工时"的月汇总表，不带日级数据。
    """
    cur.execute("""SELECT id FROM attendance_summary
                   WHERE project_id=%s AND worker_id=%s
                     AND business_month=%s
                     AND COALESCE(source_file_id,0)=COALESCE(%s,0)""",
                (project_id, worker_id, business_month, source_file_id))
    row = cur.fetchone()
    if row:
        cur.execute("""UPDATE attendance_summary SET
                       business_period_start=%s, business_period_end=%s,
                       hours=%s, quantity=%s,
                       worker_type=%s, worker_class=%s, floor_or_group=%s,
                       source_type=%s, source_ref=%s,
                       name_raw=%s, id_card_raw=%s, ingested_at=NOW()
                       WHERE id=%s AND business_month=%s""",
                    (business_period_start, business_period_end,
                     hours, quantity, worker_type, worker_class, floor_or_group,
                     source_type, source_ref,
                     name_raw, id_card_raw, row[0], business_month))
        return 'update'
    cur.execute("""INSERT INTO attendance_summary
                   (etl_batch_id, ingested_at, enterprise_id, project_id, worker_id,
                    business_month, business_period_start, business_period_end,
                    hours, quantity, worker_type, worker_class, floor_or_group,
                    source_type, source_file_id, source_ref,
                    name_raw, id_card_raw)
                   VALUES (0, NOW(), %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (enterprise_id, project_id, worker_id, business_month,
                 business_period_start, business_period_end,
                 hours, quantity, worker_type, worker_class, floor_or_group,
                 source_type, source_file_id, source_ref,
                 name_raw, id_card_raw))
    return 'insert'
