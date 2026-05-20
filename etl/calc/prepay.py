"""预付模式出款计算（13 项）— finance_mode='prepay' 项目

公式：
  #1  考勤账单金额 = SUM(bill_totals.amount) 优先 / 否则 attendance.hours × unit_prices.price
  #2  预付考勤估计 = 基准日金额 × prepay_days × profit_ratio
                    基准日由 base_day_mode 决定 (peak/latest/avg/custom)
  #3  合计 = #1 + #2
  #4  考勤出款上限 = #3 × profit_ratio
  #5  已发薪金额 = SUM(payrolls.work_amount) [parsed_shift_date 自然月 ≤ 申请日]
  #6  已直发(验证) = #5 中 ∩ 甲方考勤名单（mart_attendance/summary）
  #7  已垫付 = SUM(loan_records.amount - returned) [abill_month=本月 + pay_time≤申请日 + 待回>0]
  #8  账户结余 = mini_ent_account.loan_surplus_balance (项目子账户)
  #9  项目代收超额 = max(0, (#7 - #8) × profit - min(#1×profit, #5, #6/profit))
  #10 出款金额 = 考勤出款上限 - 已垫付 - 项目代收超额
  #11 代收超额扣减(实控人) — 暂未实现
  #12 授信余额 = factoring_limit - 实控人在途
  #13 最终出款 = min(#12, #10)

用法：
  python etl/calc/prepay.py --project-id N --business-month 2026-04 --apply-date 2026-04-27
"""
import sys, argparse, calendar
from datetime import date, datetime, timedelta
sys.path.insert(0, 'D:/小鱼AI数据')
from scripts._db import connect
from etl._utils import get_business_cycle, derive_business_period


def calc_prepay(project_id, business_month, apply_date=None,
                apply_time=None,
                base_day_mode='peak', prepay_days=7,
                base_day_date=None):
    """预付出款 13 项计算

    base_day_mode:
      'peak'    — 近 7 日按日聚合后取最大日金额
      'latest'  — 申请日前一天（昨天）金额
      'avg'     — 近 7 日平均日金额（仅有金额日）
      'custom'  — 用户指定 base_day_date 那天金额
    prepay_days: 预付天数（默认 7）
    base_day_date: 'YYYY-MM-DD'，仅 base_day_mode='custom' 时用
    """
    conn = connect('fish-test')
    cur = conn.cursor()

    cur.execute("SELECT enterprise_id, profit_ratio FROM projects WHERE id=%s", (project_id,))
    proj = cur.fetchone()
    if not proj:
        raise RuntimeError(f'项目 {project_id} 未 seed')
    enterprise_id, profit_ratio = proj
    profit = float(profit_ratio)
    business_cycle = get_business_cycle(cur, project_id)

    # apply_date 默认从 apply_time 取，或业务月最后一天
    if apply_date is None:
        if apply_time:
            at = apply_time
            if isinstance(at, str):
                at = datetime.strptime(at, '%Y-%m-%d %H:%M:%S')
            apply_date = at.date()
        else:
            y, m = int(business_month[:4]), int(business_month[5:7])
            apply_date = date(y, m, calendar.monthrange(y, m)[1])
    elif isinstance(apply_date, str):
        apply_date = datetime.strptime(apply_date, '%Y-%m-%d').date()

    out = {
        'project_id': project_id, 'business_month': business_month,
        'apply_date': str(apply_date), 'profit_ratio': profit,
        'finance_mode': 'prepay',
    }

    # 单价文案（header 展示）：calc 入口处统一生成
    from etl.calc._validity_filter import fetch_unit_prices
    from etl.calc.payment_summary import _format_unit_price_label
    _up_pack = fetch_unit_prices(cur, project_id, ref_date=apply_date)
    out['_unit_prices'] = _up_pack.get('rules') or []
    out['_unit_price_label'] = _format_unit_price_label(_up_pack)

    apply_dt_end = datetime.combine(apply_date, datetime.max.time())

    # ===== #1 考勤账单金额 =====
    # 优先 bill_totals；fallback attendance × unit_prices
    cur.execute("""SELECT COALESCE(SUM(amount), 0) FROM bill_totals
                   WHERE project_id=%s AND business_month=%s""",
                (project_id, business_month))
    bill_from_totals = float(cur.fetchone()[0] or 0)

    if bill_from_totals > 0:
        v1 = bill_from_totals
        v1_source = 'bill_totals'
    else:
        # attendance × unit_price (calc-time validity：取行+内存判定+聚合)
        from etl._kaoqin_filter import build_attendance_where, apply_daily_deduction
        from etl.calc._validity_filter import (fetch_attendance_rows,
                                                fetch_unit_prices, match_unit_price,
                                                row_amount)
        att_where, att_args = build_attendance_where(cur, project_id, table_alias='')
        att_rows_bm = fetch_attendance_rows(
            cur, project_id,
            where_extra=f' AND business_month=%s AND shift_date<=%s {att_where}',
            where_args=[business_month, apply_date] + att_args)
        valid_att_bm = [r for r in att_rows_bm if r.get('is_valid', 1)]
        unit_prices = fetch_unit_prices(cur, project_id, ref_date=apply_date)

        v1 = 0
        for r in valid_att_bm:
            price, unit = match_unit_price(unit_prices, r)
            if not price:
                continue
            v1 += row_amount(r, unit) * price
        v1_source = 'attendance × unit_prices'
        # 应用项目级天扣除规则
        v1, ded_info = apply_daily_deduction(cur, project_id, business_month, v1)
        if ded_info['deduction_amount'] > 0:
            out['#1_天扣除'] = ded_info
            v1_source += f" - 扣 {ded_info['daily_deduction_hours']}h×{ded_info['distinct_days']}天×¥{ded_info['rep_price']}"
    out['#1_考勤账单金额'] = round(v1, 2)
    out['#1_来源'] = v1_source

    # 总人数 / 总工时（attendance + attendance_summary 合并，已 apply_calc_validity）
    # bill_totals 路径下没 valid_att_bm，需补取一次
    if bill_from_totals > 0:
        from etl._kaoqin_filter import build_attendance_where
        from etl.calc._validity_filter import fetch_attendance_rows
        att_where, att_args = build_attendance_where(cur, project_id, table_alias='')
        att_rows_bm = fetch_attendance_rows(
            cur, project_id,
            where_extra=f' AND business_month=%s AND shift_date<=%s {att_where}',
            where_args=[business_month, apply_date] + att_args)
        valid_att_bm = [r for r in att_rows_bm if r.get('is_valid', 1)]

    a_hours = sum(float(r.get('hours') or 0) for r in valid_att_bm)
    att_names_set = {r['name_raw'] for r in valid_att_bm if r.get('name_raw')}

    cur.execute("""SELECT name_raw, COALESCE(hours, 0)
                   FROM attendance_summary
                   WHERE project_id=%s AND business_month=%s""",
                (project_id, business_month))
    sum_rows = cur.fetchall()
    s_hours = sum(float(r[1] or 0) for r in sum_rows)
    s_names = {r[0] for r in sum_rows if r[0]}

    total_persons = len(att_names_set | s_names)
    out['#1_总人数'] = total_persons
    out['#1_总工时'] = round(a_hours + s_hours, 2)

    # ===== #2 预付考勤估计 =====
    # base_day_mode 决定基准日金额：
    #   peak    — 近 7 日按日聚合后取最大日金额
    #   latest  — 申请日当天金额
    #   avg     — 近 7 日平均日金额（仅有金额日）
    # 然后 base_day_amount × prepay_days × profit 得到 v2
    # 业务周期截断：7 日窗口起点不早于业务周期 start
    from etl._kaoqin_filter import build_attendance_where
    from etl._utils import derive_business_period
    from etl.calc._validity_filter import (fetch_attendance_rows,
                                            fetch_unit_prices, match_unit_price,
                                            row_amount)
    period_start, period_end = derive_business_period(apply_date, business_cycle)
    seven_start = max(apply_date - timedelta(days=6), period_start) if period_start else (apply_date - timedelta(days=6))

    att_where_7d, att_args_7d = build_attendance_where(cur, project_id, table_alias='')
    att_rows_7d = fetch_attendance_rows(
        cur, project_id,
        where_extra=f' AND shift_date BETWEEN %s AND %s {att_where_7d}',
        where_args=[seven_start, apply_date] + att_args_7d)
    valid_att_7d = [r for r in att_rows_7d if r.get('is_valid', 1)]
    unit_prices_7d = fetch_unit_prices(cur, project_id, ref_date=apply_date)

    daily = {}
    for r in valid_att_7d:
        price, unit = match_unit_price(unit_prices_7d, r)
        if not price:
            continue
        amt = row_amount(r, unit) * price
        d = r.get('shift_date')
        daily[d] = daily.get(d, 0) + amt

    if base_day_mode == 'latest':
        # 申请日前一天（昨天）
        from datetime import date as _date2
        yesterday = apply_date - timedelta(days=1)
        base_amt = daily.get(yesterday, 0)
        base_label = f'最近一天（昨日 {yesterday} 金额 ¥{base_amt:.0f}）'
    elif base_day_mode == 'avg':
        nonzero = [v for v in daily.values() if v > 0]
        base_amt = sum(nonzero) / len(nonzero) if nonzero else 0
        base_label = f'近窗口平均（{len(nonzero)} 个有金额日 / 平均 ¥{base_amt:.0f}）'
    elif base_day_mode == 'custom':
        # 解析 base_day_date
        from datetime import datetime as _dt
        try:
            cd = _dt.strptime(str(base_day_date), '%Y-%m-%d').date() if base_day_date else None
        except (ValueError, TypeError):
            cd = None
        # custom 日可能不在 7 日窗口里，单独查一次
        if cd is None:
            base_amt = 0
            base_label = '指定日期（未填，金额=0）'
        else:
            # custom 日可能不在 7 日窗口内，单独取一次
            att_rows_cd = fetch_attendance_rows(
                cur, project_id,
                where_extra=f' AND shift_date=%s {att_where_7d}',
                where_args=[cd] + att_args_7d)
            base_amt = 0
            for r in att_rows_cd:
                if not r.get('is_valid', 1):
                    continue
                price, unit = match_unit_price(unit_prices_7d, r)
                if not price:
                    continue
                base_amt += row_amount(r, unit) * price
            base_label = f'指定日期（{cd} 金额 ¥{base_amt:.0f}）'
    else:  # peak
        if daily:
            peak_d, peak_v = max(daily.items(), key=lambda x: x[1])
            base_amt = peak_v
            base_label = f'峰值（{peak_d} ¥{base_amt:.0f}）'
        else:
            base_amt = 0
            base_label = '峰值（无数据）'

    v2 = base_amt * int(prepay_days) * profit
    out['#2_预付考勤估计'] = round(v2, 2)
    out['_近窗口范围'] = f'{seven_start} ~ {apply_date}'
    out['_基准日模式'] = base_day_mode
    out['_基准日金额'] = round(base_amt, 2)
    out['_预付天数'] = int(prepay_days)
    out['_基准日描述'] = base_label

    # ===== #3 合计 / #4 考勤出款上限 =====
    out['#3_合计'] = round(v1 + v2, 2)
    out['#4_考勤出款上限'] = round((v1 + v2) * profit, 2)

    # ===== #5 已发薪金额 =====
    # 按 business_cycle 决定 parsed_shift_date 范围:
    #   natural_month  → 该自然月范围
    #   上月26-本月25   → 上月26~本月25
    # 上限取 min(周期末, apply_date)
    import calendar
    y_bm, m_bm = int(business_month[:4]), int(business_month[5:7])
    if business_cycle and '26' in str(business_cycle) and '25' in str(business_cycle):
        # 上月26-本月25
        if m_bm == 1:
            nat_start = date(y_bm - 1, 12, 26)
        else:
            nat_start = date(y_bm, m_bm - 1, 26)
        nat_last = date(y_bm, m_bm, 25)
    else:
        # natural_month
        nat_start = date(y_bm, m_bm, 1)
        nat_last = date(y_bm, m_bm, calendar.monthrange(y_bm, m_bm)[1])
    nat_end = min(nat_last, apply_date)

    # calc-time validity：取行 + 跑规则 + 按 count_as_faxin=1 内存过滤
    from etl.calc._validity_filter import apply_calc_validity, parse_extra_data
    cur.execute("""SELECT name_raw, work_amount, payroll_kind, pay_time, extra_data, source_type
                   FROM payrolls
                   WHERE project_id=%s
                     AND parsed_shift_date BETWEEN %s AND %s""",
                (project_id, nat_start, nat_end))
    p_rows = [{
        'name_raw': r[0], 'work_amount': r[1], 'payroll_kind': r[2],
        'pay_time': r[3], 'extra_data': parse_extra_data(r[4]), 'source_type': r[5],
    } for r in cur.fetchall()]
    p_rows = apply_calc_validity(cur, project_id, 'payroll', p_rows)
    v5 = sum(float(r['work_amount'] or 0) for r in p_rows if r.get('count_as_faxin', 1))
    n5 = sum(1 for r in p_rows if r.get('count_as_faxin', 1))
    out['#5_已发薪金额'] = round(v5, 2)
    out['_已发薪笔数'] = n5
    out['_发薪范围'] = f'{nat_start} ~ {nat_end}（按班次解析日期自然月）'

    # ===== #6 已直发(验证) = 甲方考勤名单 ∩ payrolls =====
    # calc-time validity：复用 #1 阶段 valid_att_bm（已 apply attendance kind validity）
    att_names = {r['name_raw'] for r in valid_att_bm if r.get('name_raw')}
    cur.execute("""SELECT DISTINCT name_raw FROM attendance_summary
                   WHERE project_id=%s AND business_month=%s""",
                (project_id, business_month))
    att_names.update(r[0] for r in cur.fetchall() if r[0])
    if att_names:
        # 复用 #5 阶段的 p_rows（已 apply validity），按 is_valid=1 ∩ count_as_faxin=1 ∩ att_names 累加
        # is_valid=0（如转账备注非空=接力代付）的行仍算已发薪（count_as_faxin=1）但不计直发
        v6 = sum(float(r['work_amount'] or 0) for r in p_rows
                 if r.get('is_valid', 1) and r.get('count_as_faxin', 1)
                 and r['name_raw'] in att_names)
    else:
        v6 = 0
    out['#6_已直发'] = round(v6, 2)
    out['_attendance名单'] = len(att_names)

    # ===== #7 已垫付 =====
    # 含申请日当天（baseline 口径，<= apply_date）
    cur.execute("""SELECT COALESCE(SUM(amount - COALESCE(returned_amount, 0)), 0)
                   FROM loan_records WHERE project_id=%s AND abill_month=%s AND mark=1
                     AND DATE(pay_time)<=%s AND to_be_return_amount>0""",
                (project_id, business_month, apply_date))
    v7 = float(cur.fetchone()[0] or 0)
    out['#7_已垫付'] = round(v7, 2)

    # ===== #8 账户结余 = fish-prod.mini_ent_account.loan_surplus_balance（项目子账户）=====
    src8 = connect('fish-prod')
    sc8 = src8.cursor()
    sc8.execute("""SELECT loan_surplus_balance FROM mini_ent_account
                   WHERE project_id=%s AND mark=1
                   ORDER BY id DESC LIMIT 1""", (project_id,))
    r8 = sc8.fetchone()
    src8.close()
    v8 = float(r8[0] or 0) if r8 else 0
    out['#8_账户结余'] = round(v8, 2)

    # ===== #9 项目代收超额 =====
    # max(0, (#7 - #8) * profit - min(#1*profit, #5, #6/profit))
    if profit > 0:
        threshold = min(v1 * profit, v5, v6 / profit)
    else:
        threshold = min(v5, v6)
    v9 = max(0, (v7 - v8) * profit - threshold)
    out['#9_项目代收超额'] = round(v9, 2)

    # ===== #10 出款金额 = 考勤出款上限 - 已垫付 - 项目代收超额 =====
    v10 = out['#4_考勤出款上限'] - v7 - v9
    out['#10_出款金额'] = round(v10, 2)

    # ===== #11 代收超额扣减(实控人) — 暂未实现 =====
    out['#11_代收超额扣减'] = None

    # ===== #12 授信余额 = factoring_limit - 实控人在途 =====
    src = connect('fish-prod')
    sc = src.cursor()
    sc.execute("SELECT realname FROM biz_enterprise WHERE id=%s", (enterprise_id,))
    realname_row = sc.fetchone()
    realname = realname_row[0] if realname_row else None
    v12 = None
    if realname:
        sc.execute("""SELECT factoring_limit FROM mini_actual_ctr
                      WHERE user_name=%s AND mark=1
                      ORDER BY id DESC LIMIT 1""", (realname,))
        ctrl_row = sc.fetchone()
        if ctrl_row:
            ctrl_limit = float(ctrl_row[0] or 0)
            sc.execute("""SELECT COALESCE(SUM(lr.amount - COALESCE(lr.returned_amount, 0)), 0)
                          FROM mini_loan_record lr
                          JOIN mini_project mp ON lr.project_id=mp.id
                          JOIN biz_enterprise be ON mp.sid=be.id
                          WHERE be.realname=%s AND lr.mark=1 AND mp.mark=1
                            AND DATE(lr.pay_time)<%s AND lr.to_be_return_amount>0""",
                       (realname, apply_date))
            in_flight = float(sc.fetchone()[0] or 0)
            v12 = ctrl_limit - in_flight
            out['_控制人'] = realname
            out['_授信总额'] = ctrl_limit
            out['_实控人在途'] = in_flight
    src.close()
    out['#12_授信余额'] = round(v12, 2) if v12 is not None else None

    # ===== #13 最终出款 = min(#12, #10) =====
    if v12 is not None:
        out['#13_最终出款'] = max(0, min(v12, v10))
    else:
        out['#13_最终出款'] = v10

    conn.close()
    return out


def _fmt(v):
    if v is None: return '            --'
    if isinstance(v, str): return f'{v:>14}'
    return f'{v:>14,.2f}'


def print_summary(out):
    print(f"\n{'='*60}")
    print(f"项目 {out['project_id']} 业务月 {out['business_month']} (申请日 {out['apply_date']})")
    print(f"finance_mode: {out['finance_mode']}  出款比例: {out['profit_ratio']}")
    print(f"近 7 日窗口: {out.get('_近7日窗口', '')}")
    print(f"{'='*60}")
    print(f"  #1  考勤账单金额          {_fmt(out['#1_考勤账单金额'])}  ({out['#1_来源']})")
    print(f"  #2  预付考勤估计          {_fmt(out['#2_预付考勤估计'])}")
    print(f"  #3  合计                  {_fmt(out['#3_合计'])}")
    print(f"  #4  考勤出款上限          {_fmt(out['#4_考勤出款上限'])}")
    print(f"  #5  已发薪金额            {_fmt(out['#5_已发薪金额'])}  ({out['_已发薪笔数']} 笔)")
    print(f"  #6  已直发(验证)          {_fmt(out['#6_已直发'])}  (名单 {out['_attendance名单']} 人)")
    print(f"  #7  已垫付                {_fmt(out['#7_已垫付'])}")
    print(f"  #8  账户结余              {_fmt(out['#8_账户结余'])}")
    print(f"  #9  项目代收超额          {_fmt(out['#9_项目代收超额'])}")
    print(f"  #10 出款金额              {_fmt(out['#10_出款金额'])}")
    print(f"  #11 代收超额扣减(实控人)  {_fmt(out['#11_代收超额扣减'])}")
    print(f"  #12 授信余额              {_fmt(out['#12_授信余额'])}")
    print(f"  #13 最终出款              {_fmt(out['#13_最终出款'])}")
    print(f"{'='*60}")


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--project-id', type=int, required=True)
    ap.add_argument('--business-month', required=True)
    ap.add_argument('--apply-date', help='YYYY-MM-DD，默认从 apply-time 取或业务月最后一天')
    ap.add_argument('--apply-time', help='YYYY-MM-DD HH:MM:SS')
    args = ap.parse_args()
    out = calc_prepay(args.project_id, args.business_month,
                      apply_date=args.apply_date,
                      apply_time=args.apply_time)
    print_summary(out)
