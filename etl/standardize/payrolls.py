"""mart payrolls 标准化（DB 流路径）：raw_mini_user_shift_rel JOIN raw_mini_shift → mart payrolls

设计（2026-05-01 拍定，2026-05-03 重构）：
  - 有效性判断：pay_time IS NOT NULL + work_amount > 0 + alipay_status=2 → 写入 mart
  - 业务月归属：
    * 优先按 shift_title 解析的工时日（'3.31白班' / '3-31结算' / '借支' → 3 月业务月）
    * 解析不出来 → fallback 到 pay_time 月
  - worker 匹配：_utils.get_or_create_worker 4 档（fish-prod 有 id_card+mobile，命中 full_id 档）
  - 与 xlsx 路径（parse_payroll_xlsx）共写 mart_payrolls，按 UNIQUE KEY uk_dedup 去重
    DB 流先跑无冲突；xlsx 流装入前 SELECT source_type='fish-prod-mini' 跳过

  - 增量支持：--business-month YYYY-MM 仅刷新该业务月（DELETE 范围限定 + 装入时跳过其他月）
  - 走 etl_batches：每次 run 创建批次，结束写 raw_rows/mart_rows 计数

用法：
  python etl/standardize/payrolls.py --project-id 1986627402054696961
  python etl/standardize/payrolls.py --project-id N --business-month 2026-04
"""
import sys
import argparse
sys.path.insert(0, 'D:/小鱼AI数据')
from scripts._db import connect
from etl._utils import (parse_shift_title, derive_business_month,
                        get_or_create_worker, get_business_cycle)


def standardize(project_id, business_month=None):
    """业务月增量：传入 business_month 时仅装该 bm 的数据，其他月份保留"""
    conn = connect('fish-test')
    cur = conn.cursor()

    cur.execute("SELECT enterprise_id FROM projects WHERE id=%s", (project_id,))
    proj = cur.fetchone()
    if not proj:
        raise RuntimeError(f'项目 {project_id} 未 seed')
    enterprise_id = proj[0]
    business_cycle = get_business_cycle(cur, project_id)
    print(f'项目 {project_id} business_cycle={business_cycle} bm={business_month or "全部"}')

    # ===== 启动批次 =====
    cur.execute("""INSERT INTO etl_batches
                   (started_at, scope_enterprise, scope_project, modules,
                    triggered_by, status)
                   SELECT NOW(), e.short_name, p.title,
                          JSON_ARRAY('standardize_payrolls'), 'cli', 'running'
                   FROM enterprises e, projects p
                   WHERE e.id=%s AND p.id=%s""", (enterprise_id, project_id))
    batch_id = cur.lastrowid
    conn.commit()
    print(f'[batch] started id={batch_id}')

    try:
        # ===== 清空范围 =====
        if business_month:
            cur.execute("""DELETE FROM payrolls
                           WHERE project_id=%s AND business_month=%s
                             AND source_type='fish-prod-mini'""",
                        (project_id, business_month))
        else:
            cur.execute("""DELETE FROM payrolls
                           WHERE project_id=%s AND source_type='fish-prod-mini'""",
                        (project_id,))
        n_deleted = cur.rowcount
        print(f'  清空 payrolls(fish-prod-mini): {n_deleted} 行')

        # ===== JOIN 取数（精简：仅 8 个小鱼系统字段 → mart 标准列）=====
        # 企业名/项目名 不入 mart（用 enterprise_id/project_id 外键）
        # 班次名称 → shift_title；姓名 → name_raw；身份证 → id_card_raw；
        # 手机号 → 仅给 worker 匹配（不入 mart）；金额 → work_amount；到账时间 → pay_time
        # extra_data 留空（业务日期判定走 shift_title + pay_time fallback）
        cur.execute("""SELECT r.user_name, r.id_card, r.mobile, r.work_amount,
                              r.shift_id, r.pay_time, r.alipay_status,
                              s.title AS shift_title
                       FROM raw_mini_user_shift_rel r
                       LEFT JOIN raw_mini_shift s ON r.shift_id = s.id
                       WHERE r.project_id=%s AND r.pay_time IS NOT NULL""",
                    (project_id,))
        rows = cur.fetchall()
        print(f'  待处理流水: {len(rows)} 行')

        # 业务日期判定规则：DB 流也读项目级配置（extra_data 留空，extract 规则只能拿 shift_title）
        from etl.parsers.handlers.payroll_biz_date import load_bd_rules, resolve_business_date
        bd_rules = load_bd_rules(cur, project_id)

        # === 第一遍：build dict rows ===
        intermediate = []
        for row in rows:
            (user_name, id_card, mobile, work_amt,
             shift_id, pay_time, alipay_status, shift_title) = row
            amt = float(work_amt or 0)
            if pay_time is None or amt <= 0 or alipay_status != 2:
                continue
            # extra_data: 仅班次名称（业务日期判定可能配 '班次名称含 X月Y日'）
            extra = {'班次名称': shift_title} if shift_title else {}
            intermediate.append({
                'user_name': user_name,
                'name_raw': user_name,
                'id_card': id_card,
                'mobile': mobile,
                'work_amount': amt,
                'shift_id': shift_id,
                'pay_time': pay_time,
                'alipay_status': alipay_status,
                'shift_title': shift_title,
                'extra_data': extra,
            })

        # validity 已挪到 calc 层（etl/calc/_validity_filter.py）
        # mart INSERT 时 is_valid/count_as_faxin 用默认 1，规则改动不需重 standardize

        # 跨 source 三元组查重：跟 parse_payroll_xlsx.process_sheet 对称
        # DELETE 已清空 fish-prod-mini 自己的行，剩下的主要是 xlsx_payroll 流。
        # 命中 → skip 避免双装（同一笔被 xlsx 流和 DB 流都装入）
        cur.execute("""SELECT name_raw, pay_time, work_amount FROM payrolls
                       WHERE project_id=%s""", (project_id,))
        seen_triples = set()
        for n, pt_v, wa in cur.fetchall():
            try:
                seen_triples.add((n, pt_v, float(wa)))
            except (TypeError, ValueError):
                pass

        n_by_kind = {}
        n_inserted = n_skipped = n_dup_ignored = n_dup_triple = n_off_month = 0
        insert_sql = """INSERT IGNORE INTO payrolls
                        (etl_batch_id, ingested_at, enterprise_id, project_id, worker_id,
                         business_month, pay_time, parsed_shift_date,
                         work_amount, payroll_kind, alipay_status,
                         source_type, source_ref, shift_title, name_raw, id_card_raw,
                         is_valid, invalid_reason, count_as_faxin, extra_data)
                        VALUES (%s, NOW(), %s, %s, %s, %s, %s, %s, %s, %s, %s,
                                'fish-prod-mini', %s, %s, %s, %s, %s, %s, %s, %s)"""

        for r in intermediate:
            user_name = r['user_name']
            shift_title = r['shift_title']
            pay_time = r['pay_time']
            amt = r['work_amount']
            shift_id = r['shift_id']
            id_card = r['id_card']

            # 业务日期判定：优先 web 配的 extract/infer/bill_month 规则；fallback 用 parse_shift_title 内置
            biz_d = None
            if bd_rules.get('extract') or bd_rules.get('infer') or bd_rules.get('bill_month'):
                biz_d = resolve_business_date(pay_time, r.get('extra_data'), bd_rules)
            kind = 'shift_dated'
            if biz_d is None:
                # fallback: 内置 parse_shift_title（"X月Y日"等格式）
                biz_d, kind = parse_shift_title(shift_title, pay_time)
            if biz_d is None:
                n_skipped += 1
                continue
            # 转 datetime → date
            if hasattr(biz_d, 'date'):
                biz_d = biz_d.date()
            n_by_kind[kind] = n_by_kind.get(kind, 0) + 1

            bm = derive_business_month(biz_d, business_cycle)
            if bm is None:
                n_skipped += 1
                continue

            if business_month and bm != business_month:
                n_off_month += 1
                continue

            # 跨 source 三元组查重：跳过 xlsx 流（或本批次）已有的同笔
            triple_key = (user_name, pay_time, float(amt))
            if triple_key in seen_triples:
                n_dup_triple += 1
                continue
            seen_triples.add(triple_key)

            wid = get_or_create_worker(cur, user_name, project_id,
                                        id_card=id_card, mobile=r.get('mobile'))
            if not wid:
                n_skipped += 1
                continue

            extra_json = None
            if r.get('extra_data'):
                import json as _json
                extra_json = _json.dumps(r['extra_data'], ensure_ascii=False, default=str)
            cur.execute(insert_sql,
                        (batch_id, enterprise_id, project_id, wid, bm,
                         pay_time, biz_d, amt, kind, r['alipay_status'],
                         f'shift_id={shift_id}', shift_title, user_name, id_card,
                         int(r.get('is_valid', 1)),
                         r.get('invalid_reason'),
                         int(r.get('count_as_faxin', 1)),
                         extra_json))
            if cur.rowcount == 1:
                n_inserted += 1
            else:
                n_dup_ignored += 1
            if n_inserted > 0 and n_inserted % 500 == 0:
                conn.commit()

        conn.commit()

        # ===== 完成批次 =====
        cur.execute("""UPDATE etl_batches SET status='ok', finished_at=NOW(),
                       raw_rows=JSON_OBJECT('raw_mini_user_shift_rel', %s),
                       mart_rows=JSON_OBJECT('payrolls_inserted', %s,
                                             'payrolls_dup_ignored', %s,
                                             'payrolls_skipped', %s)
                       WHERE id=%s""",
                    (len(rows), n_inserted, n_dup_ignored, n_skipped, batch_id))
        conn.commit()

        print(f'\n=== 解析分布 ===')
        for k, n in sorted(n_by_kind.items(), key=lambda x: -x[1]):
            print(f'  {k:18s}: {n}')
        print(f'\n  写入 payrolls: {n_inserted} 行')
        print(f'  UNIQUE 跳过: {n_dup_ignored} 行（同三元组已存在）')
        print(f'  跨 source 跳过: {n_dup_triple} 行（xlsx 流已装入的同三元组）')
        print(f'  解析跳过: {n_skipped} 行（无 pay_time/金额=0/alipay_status≠2/无 worker）')
        if business_month:
            print(f'  跨月跳过: {n_off_month} 行（增量模式仅装 {business_month}）')
        print(f'[batch] ok id={batch_id}')

    except Exception as e:
        cur.execute("""UPDATE etl_batches SET status='failed', finished_at=NOW(),
                       error_message=%s WHERE id=%s""",
                    (str(e)[:65000], batch_id))
        conn.commit()
        raise
    finally:
        conn.close()
    return batch_id


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--project-id', type=int, required=True)
    ap.add_argument('--business-month', help='YYYY-MM，仅刷该月份；不传则全量')
    args = ap.parse_args()
    standardize(args.project_id, business_month=args.business_month)
