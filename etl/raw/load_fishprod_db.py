"""raw 层装入：从 fish-prod 镜像 DB 表到 fish-test.raw_*

完全不依赖本地文件，所有数据从老库流式读+写。

支持模块：
  - mini_a_bill         → raw_mini_a_bill
  - mini_user_shift_rel → raw_mini_user_shift_rel
  - mini_loan_record    → loan_records（直接镜像到权威表，省去 raw 层）
"""
import sys, json
sys.path.insert(0, 'D:/小鱼AI数据')
from datetime import datetime
from scripts._db import connect


def load_mini_a_bill(batch_id, project_id, enterprise_id, src, dst,
                      since_date=None, business_month=None):
    """
    business_month: 'YYYY-MM' 精确匹配 bill_month
    since_date: (开区间下界) 兜底；business_month 优先
    """
    sc = src.cursor()
    dc = dst.cursor()
    sql = """SELECT id, project_id, first_name, sub_project_name, bill_month,
                    bill_interval_start, bill_interval_end, bill_amount, invoice_amount,
                    invoicing_time, cycle_amount, due_refund_time, url, color, status,
                    advance_refund_time, advance_refund_amount, actual_refund_time,
                    actual_refund_amount, bill_valid_amount, verify_type, note,
                    bill_status, create_user, create_time, update_user, update_time, mark
             FROM mini_a_bill WHERE project_id=%s"""
    args = [project_id]
    if business_month:
        # bill_month 列在老库可能是 'YYYY-MM' 或 'YYYY-MM ~ YYYY-MM' 段，用 LIKE 起手匹配
        sql += " AND (bill_month=%s OR bill_month LIKE %s)"
        args += [business_month, f'{business_month}%']
    elif since_date:
        sql += " AND (bill_interval_end >= %s OR bill_month >= %s OR create_time >= %s)"
        sm = str(since_date)[:7]
        args += [since_date, sm, since_date]
    sc.execute(sql, args)
    rows = sc.fetchall()
    n = 0
    insert_sql = """INSERT INTO raw_mini_a_bill
                    (etl_batch_id, ingested_at, source_db,
                     id, project_id, first_name, sub_project_name, bill_month,
                     bill_interval_start, bill_interval_end, bill_amount, invoice_amount,
                     invoicing_time, cycle_amount, due_refund_time, url, color, status,
                     advance_refund_time, advance_refund_amount, actual_refund_time,
                     actual_refund_amount, bill_valid_amount, verify_type, note,
                     bill_status, create_user, create_time, update_user, update_time, mark)
                    VALUES (%s, NOW(), 'fish-prod', """ + ', '.join(['%s']*28) + """)
                    ON DUPLICATE KEY UPDATE
                      etl_batch_id=VALUES(etl_batch_id), ingested_at=NOW(),
                      project_id=VALUES(project_id), first_name=VALUES(first_name),
                      sub_project_name=VALUES(sub_project_name), bill_month=VALUES(bill_month),
                      bill_interval_start=VALUES(bill_interval_start),
                      bill_interval_end=VALUES(bill_interval_end),
                      bill_amount=VALUES(bill_amount), invoice_amount=VALUES(invoice_amount),
                      invoicing_time=VALUES(invoicing_time), cycle_amount=VALUES(cycle_amount),
                      due_refund_time=VALUES(due_refund_time), url=VALUES(url),
                      color=VALUES(color), status=VALUES(status),
                      advance_refund_time=VALUES(advance_refund_time),
                      advance_refund_amount=VALUES(advance_refund_amount),
                      actual_refund_time=VALUES(actual_refund_time),
                      actual_refund_amount=VALUES(actual_refund_amount),
                      bill_valid_amount=VALUES(bill_valid_amount),
                      verify_type=VALUES(verify_type), note=VALUES(note),
                      bill_status=VALUES(bill_status),
                      create_user=VALUES(create_user), create_time=VALUES(create_time),
                      update_user=VALUES(update_user), update_time=VALUES(update_time),
                      mark=VALUES(mark)"""
    for r in rows:
        # bill_month 规范化（去 ' ~ 'YYYY-MM 后缀）
        r_list = list(r)
        if r_list[4]:  # bill_month 在索引 4
            r_list[4] = str(r_list[4]).split(' ')[0]
        dc.execute(insert_sql, (batch_id,) + tuple(r_list))
        n += 1
    print(f'  raw_mini_a_bill: 写入 {n} 行')
    return n


def load_mini_user_shift_rel(batch_id, project_id, enterprise_id, src, dst,
                              since_date=None, until_date=None):
    sc = src.cursor()
    dc = dst.cursor()
    sql = """SELECT r.id, r.user_id, r.openid, r.project_id, r.shift_id, r.shop_id, r.sid, r.uid, r.task_id,
                    r.note, r.work_amount, r.service_charge, r.advance_payment_amount,
                    r.tax_service_charge, r.non_advance_payment_amount, r.work_pic_urls,
                    r.work_status, r.is_source, r.create_user, r.create_time, r.update_user,
                    r.message, r.update_time, r.type, r.mark, r.sign_out_time, r.pay_time,
                    r.alipay_reason, r.alipay_status, r.out_batch_no, r.user_name, r.id_card,
                    r.mobile, r.bank_no, r.json_ext, r.batch_id, r.pdf_url, r.ent_account_id,
                    r.account_type, r.if_sync_bill, r.pay_click_time, r.otheac, r.service_rate,
                    r.base_service_rate, r.bill_status, r.tax_id, r.batch_pdf, r.pay_img_url,
                    r.pay_img_upload_time
             FROM mini_user_shift_rel r WHERE r.project_id=%s AND r.mark=1"""
    args = [project_id]
    if since_date and until_date:
        # sign_out_time / pay_time / create_time 任一落在业务周期 [since, until] 内
        sql += """ AND (
                    (r.sign_out_time IS NOT NULL AND DATE(r.sign_out_time) BETWEEN %s AND %s)
                  OR (r.pay_time IS NOT NULL AND DATE(r.pay_time) BETWEEN %s AND %s)
                  OR (r.create_time IS NOT NULL AND DATE(r.create_time) BETWEEN %s AND %s)
                  )"""
        args += [since_date, until_date, since_date, until_date, since_date, until_date]
    elif since_date:
        sql += " AND (r.pay_time >= %s OR r.create_time >= %s)"
        args += [since_date, since_date]
    sc.execute(sql, args)
    rows = sc.fetchall()
    n = 0
    insert_sql = """INSERT INTO raw_mini_user_shift_rel
                    (etl_batch_id, ingested_at, source_db,
                     id, user_id, openid, project_id, shift_id, shop_id, sid, uid, task_id, note,
                     work_amount, service_charge, advance_payment_amount, tax_service_charge,
                     non_advance_payment_amount, work_pic_urls, work_status, is_source,
                     create_user, create_time, update_user, message, update_time, type, mark,
                     sign_out_time, pay_time, alipay_reason, alipay_status, out_batch_no,
                     user_name, id_card, mobile, bank_no, json_ext, batch_id, pdf_url,
                     ent_account_id, account_type, if_sync_bill, pay_click_time, otheac,
                     service_rate, base_service_rate, bill_status, tax_id, batch_pdf,
                     pay_img_url, pay_img_upload_time)
                    VALUES (%s, NOW(), 'fish-prod', """ + ', '.join(['%s']*49) + """)
                    ON DUPLICATE KEY UPDATE
                      etl_batch_id=VALUES(etl_batch_id), ingested_at=NOW(),
                      user_id=VALUES(user_id), openid=VALUES(openid),
                      project_id=VALUES(project_id), shift_id=VALUES(shift_id),
                      shop_id=VALUES(shop_id), sid=VALUES(sid), uid=VALUES(uid),
                      task_id=VALUES(task_id), note=VALUES(note),
                      work_amount=VALUES(work_amount), service_charge=VALUES(service_charge),
                      advance_payment_amount=VALUES(advance_payment_amount),
                      tax_service_charge=VALUES(tax_service_charge),
                      non_advance_payment_amount=VALUES(non_advance_payment_amount),
                      work_pic_urls=VALUES(work_pic_urls), work_status=VALUES(work_status),
                      is_source=VALUES(is_source),
                      create_user=VALUES(create_user), create_time=VALUES(create_time),
                      update_user=VALUES(update_user), message=VALUES(message),
                      update_time=VALUES(update_time), type=VALUES(type), mark=VALUES(mark),
                      sign_out_time=VALUES(sign_out_time), pay_time=VALUES(pay_time),
                      alipay_reason=VALUES(alipay_reason), alipay_status=VALUES(alipay_status),
                      out_batch_no=VALUES(out_batch_no),
                      user_name=VALUES(user_name), id_card=VALUES(id_card),
                      mobile=VALUES(mobile), bank_no=VALUES(bank_no),
                      json_ext=VALUES(json_ext), batch_id=VALUES(batch_id),
                      pdf_url=VALUES(pdf_url), ent_account_id=VALUES(ent_account_id),
                      account_type=VALUES(account_type), if_sync_bill=VALUES(if_sync_bill),
                      pay_click_time=VALUES(pay_click_time), otheac=VALUES(otheac),
                      service_rate=VALUES(service_rate),
                      base_service_rate=VALUES(base_service_rate),
                      bill_status=VALUES(bill_status), tax_id=VALUES(tax_id),
                      batch_pdf=VALUES(batch_pdf),
                      pay_img_url=VALUES(pay_img_url),
                      pay_img_upload_time=VALUES(pay_img_upload_time)"""
    batch = []
    for r in rows:
        batch.append((batch_id,) + tuple(r))
        if len(batch) >= 500:
            dc.executemany(insert_sql, batch); n += len(batch); batch = []
    if batch:
        dc.executemany(insert_sql, batch); n += len(batch)
    print(f'  raw_mini_user_shift_rel: 写入 {n} 行')
    return n


def load_mini_shift(batch_id, project_id, enterprise_id, src, dst,
                     since_date=None, until_date=None):
    """fish-prod.mini_shift 全字段镜像 → raw_mini_shift。出款计算需要 title。"""
    sc = src.cursor()
    dc = dst.cursor()
    sql = """SELECT id, sid, shop_id, user_id, title, project_id, is_confirm, pay_status, is_source,
                    area_radius, lat, lng, if_ele_fence, if_face_nucleation, area_name, tpl_url,
                    shift_date, shift_start, shift_end, shift_amount, shift_type, card_view,
                    if_insure, insure_plan, address, city, district, province, name,
                    latitude, longitude, create_user, create_time, update_user, update_time,
                    version, mark, task_id
             FROM mini_shift WHERE project_id=%s"""
    args = [project_id]
    if since_date and until_date:
        sql += """ AND (
                    (shift_date IS NOT NULL AND shift_date BETWEEN %s AND %s)
                  OR (create_time IS NOT NULL AND DATE(create_time) BETWEEN %s AND %s)
                  )"""
        args += [since_date, until_date, since_date, until_date]
    elif since_date:
        sql += " AND (shift_date >= %s OR create_time >= %s)"
        args += [since_date, since_date]
    sc.execute(sql, args)
    rows = sc.fetchall()
    insert_sql = """INSERT INTO raw_mini_shift
                    (etl_batch_id, ingested_at, source_db,
                     id, sid, shop_id, user_id, title, project_id, is_confirm, pay_status, is_source,
                     area_radius, lat, lng, if_ele_fence, if_face_nucleation, area_name, tpl_url,
                     shift_date, shift_start, shift_end, shift_amount, shift_type, card_view,
                     if_insure, insure_plan, address, city, district, province, name,
                     latitude, longitude, create_user, create_time, update_user, update_time,
                     version, mark, task_id)
                    VALUES (%s, NOW(), 'fish-prod', """ + ', '.join(['%s']*38) + """)
                    ON DUPLICATE KEY UPDATE
                      title=VALUES(title), shift_date=VALUES(shift_date),
                      shift_amount=VALUES(shift_amount), update_time=VALUES(update_time),
                      mark=VALUES(mark), etl_batch_id=VALUES(etl_batch_id), ingested_at=NOW()"""
    n = 0
    batch = []
    for r in rows:
        batch.append((batch_id,) + tuple(r))
        if len(batch) >= 500:
            dc.executemany(insert_sql, batch); n += len(batch); batch = []
    if batch:
        dc.executemany(insert_sql, batch); n += len(batch)
    print(f'  raw_mini_shift: 写入 {n} 行')
    return n


def load_mini_loan_record(batch_id, project_id, enterprise_id, src, dst,
                           business_month=None):
    """出款记录直接进权威表 loan_records（镜像 + 本地 ID）。
    business_month: 'YYYY-MM' 时按 abill_month 精确过滤。"""
    sc = src.cursor()
    dc = dst.cursor()
    sql = """SELECT id, project_id, abill_month, bill_month, pay_time, amount,
                    predict_time, due_time, to_be_return_amount, status, mark
             FROM mini_loan_record WHERE project_id=%s AND mark=1"""
    args = [project_id]
    if business_month:
        sql += " AND (abill_month=%s OR abill_month LIKE %s)"
        args += [business_month, f'{business_month}%']
    sc.execute(sql, args)
    rows = sc.fetchall()
    n = 0
    for r in rows:
        rid, pid, abm, bm, pt, amt, predict_t, due_t, tbr, status, mark = r
        returned = float(amt) - float(tbr or 0)
        dc.execute("""INSERT INTO loan_records
                      (id, enterprise_id, project_id, abill_month, bill_month, pay_time, amount,
                       predict_time, due_time, to_be_return_amount, returned_amount, status, mark,
                       source_type, last_synced_at)
                      VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'fish-prod', NOW())
                      ON DUPLICATE KEY UPDATE
                        amount=VALUES(amount), to_be_return_amount=VALUES(to_be_return_amount),
                        returned_amount=VALUES(returned_amount), status=VALUES(status),
                        last_synced_at=NOW()""",
                   (rid, enterprise_id, pid, abm, bm, pt, amt, predict_t, due_t, tbr, returned, status, mark))
        n += 1
    print(f'  loan_records: 写入 {n} 行')
    return n


def run(enterprise_id, project_id):
    """按 enterprise_id + project_id 跑全部 DB 镜像"""
    src = connect('fish-prod')
    dst = connect('fish-test')

    # 启动批次
    dc = dst.cursor()
    dc.execute("""INSERT INTO etl_batches (started_at, scope_enterprise, scope_project, modules,
                                            triggered_by, status)
                  SELECT NOW(), e.short_name, p.title, JSON_ARRAY('raw_db_mirror'), 'cli', 'running'
                  FROM enterprises e, projects p
                  WHERE e.id=%s AND p.id=%s""", (enterprise_id, project_id))
    batch_id = dc.lastrowid
    dst.commit()
    print(f'[batch] started id={batch_id} ent={enterprise_id} proj={project_id}')

    try:
        n_bill = load_mini_a_bill(batch_id, project_id, enterprise_id, src, dst)
        n_shift_rel = load_mini_user_shift_rel(batch_id, project_id, enterprise_id, src, dst)
        n_shift = load_mini_shift(batch_id, project_id, enterprise_id, src, dst)
        n_loan = load_mini_loan_record(batch_id, project_id, enterprise_id, src, dst)
        dst.commit()
        dc.execute("""UPDATE etl_batches SET status='ok', finished_at=NOW(),
                                              raw_rows=JSON_OBJECT(
                                                'raw_mini_a_bill', %s,
                                                'raw_mini_user_shift_rel', %s,
                                                'raw_mini_shift', %s,
                                                'loan_records', %s)
                      WHERE id=%s""", (n_bill, n_shift_rel, n_shift, n_loan, batch_id))
        dst.commit()
        print(f'[batch] ok id={batch_id}')
    except Exception as e:
        dc.execute("UPDATE etl_batches SET status='failed', finished_at=NOW(), error_message=%s WHERE id=%s",
                   (str(e)[:65000], batch_id))
        dst.commit()
        raise
    finally:
        src.close(); dst.close()
    return batch_id


if __name__ == '__main__':
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument('--enterprise-id', type=int, required=True)
    ap.add_argument('--project-id', type=int, required=True)
    args = ap.parse_args()
    run(enterprise_id=args.enterprise_id, project_id=args.project_id)
