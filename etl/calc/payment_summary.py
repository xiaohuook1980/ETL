"""出款计算 17 项总览（fish-test 版，对应 web 应用 v2.calc 输出）

只读 mart 层 + 配置表，零接触 raw 层。
未实现项标 None / 0（工资表 #6-11 / 代收 #14 / 授信 #15 / 申请金额 #13）

用法：
    python etl/calc/payment_summary.py --project-id 1986627402054696961 --business-month 2026-04
"""
import sys, argparse, calendar
from datetime import date, datetime
sys.path.insert(0, 'D:/小鱼AI数据')
from scripts._db import connect
from etl.calc.zhifa import calc_zhifa
from etl._utils import get_business_cycle
from etl._kaoqin_filter import build_attendance_where, apply_daily_deduction


def _format_unit_price_label(up_pack):
    """从 fetch_unit_prices 输出生成 header 展示文案。
    rules 空 → '未配置'；单条全通配 → 'price unit'；多条 → 'kw1: p1 / kw2: p2'"""
    rules = up_pack.get('rules') or []
    if not rules:
        return '未配置'
    if len(rules) == 1:
        r = rules[0]
        kws = (r.get('dim1_keywords') or '') + (r.get('dim2_keywords') or '') + (r.get('dim3_keywords') or '')
        if not kws.strip():
            return f"{r['price']} {r.get('unit') or '元/小时'}"
    parts = []
    for r in rules:
        tag = (r.get('dim1_keywords') or r.get('dim2_keywords')
               or r.get('dim3_keywords') or '通配')
        parts.append(f"{tag}: {r['price']}")
    return ' / '.join(parts)


def calc_payment_summary(project_id, business_month, apply_date=None,
                          apply_time=None, apply_amount=0,
                          data_mode='wage_and_payroll', account_balance=0):
    """入口：按项目 finance_mode 分发 normal(17 项) / prepay(12 项)"""
    # 先看 finance_mode
    _conn = connect('fish-test')
    _cur = _conn.cursor()
    _cur.execute("SELECT finance_mode FROM projects WHERE id=%s", (project_id,))
    _row = _cur.fetchone()
    _conn.close()
    if _row and _row[0] == 'prepay':
        from etl.calc.prepay import calc_prepay
        return calc_prepay(project_id, business_month,
                           apply_date=apply_date, apply_time=apply_time,
                           account_balance=account_balance)
    # normal 模式（17 项原逻辑）
    return _calc_normal_17(project_id, business_month, apply_date,
                            apply_time, apply_amount, data_mode)


def _calc_normal_17(project_id, business_month, apply_date=None,
                          apply_time=None, apply_amount=0,
                          data_mode='wage_and_payroll'):
    """完整 17 项出款计算。
    apply_date: 申请日（默认 = apply_time 日期 / 业务月最后一天），用于过滤 loan_records
    apply_time: 精确时刻（datetime 或 'YYYY-MM-DD HH:MM:SS'），用于过滤 pay_time
                对齐人工导出快照（如 '2026-04-30 18:00:00'）
    apply_amount: 本项目出款金额（外部输入，#13）
    data_mode: 数据采用模式（影响 #13 计算口径）
        'wage_and_payroll' — 默认，工资表+发薪流水都参与 → #13=min(账单上限, #5+#11)
        'payroll_only'    — 只用发薪流水 → #13=min(账单上限, #5)（工资表不可信/未装）
        'wage_only'       — 只用工资表 → #13=min(账单上限, #11)（流水不可信/缺）
    """
    conn = connect('fish-test')
    cur = conn.cursor()

    cur.execute("""SELECT enterprise_id, profit_ratio
                   FROM projects WHERE id=%s""", (project_id,))
    proj = cur.fetchone()
    if not proj:
        raise RuntimeError(f'项目 {project_id} 未 seed')
    enterprise_id, profit_ratio = proj
    business_cycle = get_business_cycle(cur, project_id)
    profit = float(profit_ratio)

    # apply_date 默认推断：apply_time 日期 > 业务月最后一天（fallback）
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
    }

    # 取本项目当前生效单价（v2: project_price_config + project_price_rules）
    from etl.calc._validity_filter import fetch_unit_prices
    up_pack = fetch_unit_prices(cur, project_id, ref_date=apply_date)
    out['_unit_prices'] = up_pack.get('rules') or []
    out['_unit_price_label'] = _format_unit_price_label(up_pack)

    # ===== #1 甲方账单金额 =====
    # 优先级：bill_totals（明确账单，可多份累加）→ attendance × unit_prices fallback
    # bill_totals 数据来自账单 xlsx 解析，bill_kind 由 source_type 区分（dept_subtotal / sum_amount_col / 综合表 等）
    cur.execute("""SELECT SUM(amount) FROM bill_totals
                   WHERE project_id=%s AND business_month=%s""",
                (project_id, business_month))
    bill_from_totals = float((cur.fetchone() or [0])[0] or 0)

    if bill_from_totals > 0:
        bill_amount = bill_from_totals
        bill_source = 'bill_totals'
    else:
        # 无明确账单：工时 × 单价 (or 件数/天数 × 元/件/元/天)。
        # 按 unit_prices.unit 决定取 hours 或 quantity:
        #   unit 含'天'/'件'/'单' → SUM(quantity) * price
        #   否则                  → SUM(hours)    * price
        # calc-time validity：取行 + 内存判定 + 聚合
        from etl.calc._validity_filter import (fetch_attendance_rows,
                                                fetch_unit_prices, match_unit_price_full,
                                                row_amount)
        att_where, att_args = build_attendance_where(cur, project_id, table_alias='')
        att_rows = fetch_attendance_rows(
            cur, project_id,
            where_extra=f' AND business_month=%s {att_where}',
            where_args=[business_month] + att_args)
        unit_prices = fetch_unit_prices(cur, project_id, ref_date=apply_date)
        bill_amount = 0
        breakdown = {}  # rule_id → {tag, price, unit, hours, amount}
        for r in att_rows:
            if not r.get('is_valid', 1):
                continue
            price, unit, rule = match_unit_price_full(unit_prices, r)
            if not price:
                continue
            h = row_amount(r, unit)
            amt = h * price
            bill_amount += amt
            key = rule['id'] if rule else None
            if key not in breakdown:
                tag = (rule.get('dim1_keywords') or rule.get('dim2_keywords')
                       or rule.get('dim3_keywords') or '通配') if rule else '通配'
                breakdown[key] = {'tag': tag, 'price': price, 'unit': unit or '',
                                  'hours': 0, 'amount': 0}
            breakdown[key]['hours'] += h
            breakdown[key]['amount'] += amt
        if breakdown:
            parts = [f"{b['tag']} {b['hours']:.1f}h × ¥{b['price']}={b['amount']:,.0f}"
                     for b in breakdown.values()]
            bill_source = '考勤 × 单价（' + ' / '.join(parts) + '）'
        else:
            bill_source = '考勤 × 单价（未匹中任何规则）'
        # 应用项目级天扣除规则（仅 fallback 路径）
        bill_amount, ded_info = apply_daily_deduction(cur, project_id, business_month, bill_amount)
        if ded_info['deduction_amount'] > 0:
            out['#1_天扣除'] = ded_info
            bill_source += f" - 扣 {ded_info['daily_deduction_hours']}h×{ded_info['distinct_days']}天×¥{ded_info['rep_price']}"
    out['#1_甲方账单'] = round(bill_amount, 2)
    out['#1_来源'] = bill_source

    # ===== #2 账单:出款上限 = #1 × 出款比例 =====
    out['#2_账单出款上限'] = round(bill_amount * profit, 2)

    # ===== #3 #4 发薪流水金额 + 已直发（支持 apply_time 精确快照）=====
    yi_faxin, yi_zhifa = calc_zhifa(project_id, business_month, apply_time=apply_time)
    # 项目级配置:多项目混合时,#3 发薪流水合计用已直发代替(无法分项目)
    cur.execute("SELECT use_zhifa_as_faxin FROM projects WHERE id=%s", (project_id,))
    _r = cur.fetchone()
    if _r and _r[0]:
        yi_faxin = yi_zhifa
    out['#3_发薪流水金额'] = round(yi_faxin, 2)
    out['#4_已直发'] = round(yi_zhifa, 2)

    # ===== #5 发薪流水:出款上限 = min(#4÷比例, #3) =====
    out['#5_发薪出款上限'] = round(min(yi_zhifa / profit if profit > 0 else 0, yi_faxin), 2)

    # ===== #6-#11 工资表系列 =====
    # 工资表名单：当月 wage_sheets distinct name_raw + payable_amount
    cur.execute("""SELECT name_raw, SUM(payable_amount)
                   FROM wage_sheets WHERE project_id=%s AND business_month=%s
                   GROUP BY name_raw""",
                (project_id, business_month))
    ws_per_person = {nm: float(s or 0) for nm, s in cur.fetchall()}
    ws_sum = sum(ws_per_person.values())
    out['#6_工资表结算工资'] = round(ws_sum, 2)

    if ws_sum > 0:
        # 本月名单（账单优先 → fallback 考勤）
        cur.execute("""SELECT DISTINCT name_raw FROM bill_persons
                       WHERE project_id=%s AND business_month=%s""",
                    (project_id, business_month))
        kq_this = set(r[0] for r in cur.fetchall())
        if not kq_this:
            from etl.calc._validity_filter import fetch_attendance_rows
            att_where, att_args = build_attendance_where(cur, project_id, table_alias='')
            att_rows = fetch_attendance_rows(
                cur, project_id,
                where_extra=f' AND business_month=%s {att_where}',
                where_args=[business_month] + att_args)
            kq_this = {r['name_raw'] for r in att_rows if r.get('is_valid', 1) and r.get('name_raw')}

        # 上月名单（用于 #10）
        from datetime import date as _date
        y, m = int(business_month[:4]), int(business_month[5:7])
        prev_y, prev_m = (y, m - 1) if m > 1 else (y - 1, 12)
        prev_bm = f'{prev_y:04d}-{prev_m:02d}'
        cur.execute("""SELECT DISTINCT name_raw FROM bill_persons
                       WHERE project_id=%s AND business_month=%s""",
                    (project_id, prev_bm))
        kq_prev = set(r[0] for r in cur.fetchall())
        if not kq_prev:
            from etl.calc._validity_filter import fetch_attendance_rows
            att_where, att_args = build_attendance_where(cur, project_id, table_alias='')
            att_rows_prev = fetch_attendance_rows(
                cur, project_id,
                where_extra=f' AND business_month=%s {att_where}',
                where_args=[prev_bm] + att_args)
            kq_prev = {r['name_raw'] for r in att_rows_prev if r.get('is_valid', 1) and r.get('name_raw')}

        # 各人当月已发金额（用于 #9）— baseline 口径：仅 shift_dated（班次名/备注解析日期∈本月）
        sql = """SELECT name_raw, SUM(work_amount) FROM payrolls
                 WHERE project_id=%s AND business_month=%s
                   AND payroll_kind='shift_dated'"""
        sql_args = [project_id, business_month]
        if apply_time:
            sql += " AND pay_time<=%s"
            sql_args.append(apply_time)
        sql += " GROUP BY name_raw"
        cur.execute(sql, sql_args)
        paid_per_person = {nm: float(s or 0) for nm, s in cur.fetchall()}

        # #8 工资表匹配金额 = 工资表中名字出现在本月名单内的金额合计
        ws_matched_this = sum(amt for nm, amt in ws_per_person.items() if nm in kq_this)
        # #7 本月匹配率 = 匹配金额 / 工资表合计
        out['#7_本月匹配率'] = round(ws_matched_this / ws_sum * 100, 2) if ws_sum > 0 else None
        out['#8_工资表匹配金额'] = round(ws_matched_this, 2)
        # #9 预计直发 = 逐人 max(0, 工资表金额 - 已发) 汇总（仅本月名单内）
        prj_zhifa_ws = sum(max(0, amt - paid_per_person.get(nm, 0))
                           for nm, amt in ws_per_person.items() if nm in kq_this)
        out['#9_工资表预计直发'] = round(prj_zhifa_ws, 2)
        # #10 上月匹配率
        ws_matched_prev = sum(amt for nm, amt in ws_per_person.items() if nm in kq_prev)
        out['#10_上月匹配率'] = round(ws_matched_prev / ws_sum * 100, 2) if ws_sum > 0 else None
        # #11 工资表出款上限 = min(#9÷profit, #6)
        out['#11_工资表出款上限'] = round(min(prj_zhifa_ws / profit if profit > 0 else 0, ws_sum), 2)
    else:
        out['#7_本月匹配率'] = None
        out['#8_工资表匹配金额'] = 0
        out['#9_工资表预计直发'] = 0
        out['#10_上月匹配率'] = None
        out['#11_工资表出款上限'] = 0

    # ===== #12 本业务周期已垫付 =====
    cur.execute("""SELECT COALESCE(SUM(amount - COALESCE(returned_amount, 0)), 0)
                   FROM loan_records
                   WHERE project_id=%s AND abill_month=%s AND mark=1
                     AND DATE(pay_time) < %s
                     AND to_be_return_amount > 0""",
                (project_id, business_month, apply_date))
    out['#12_本周期已垫付'] = round(float((cur.fetchone() or [0])[0] or 0), 2)

    # ===== #13 本项目出款金额 = min(账单上限, 数据组合上限) - 已垫付 → 千位四舍五入 =====
    if data_mode == 'payroll_only':
        combined_cap = out['#5_发薪出款上限']
    elif data_mode == 'wage_only':
        combined_cap = out['#11_工资表出款上限']
    else:  # wage_and_payroll (默认)
        combined_cap = out['#11_工资表出款上限'] + out['#5_发薪出款上限']
    raw_13 = min(out['#2_账单出款上限'], combined_cap) - out['#12_本周期已垫付']
    out['#13_本项目出款金额'] = max(0, round(raw_13 / 1000) * 1000)
    out['_data_mode'] = data_mode

    # ===== #14 代收超额扣减（待实现） =====
    out['#14_代收超额扣减'] = None

    # ===== #15 授信余额 = factoring_limit - 实控人维度在途余额 =====
    # 实控人/授信数据从 fish-prod 直读（按用户拍板：新库不存这部分）
    src = connect('fish-prod')
    sc = src.cursor()
    sc.execute("SELECT realname FROM biz_enterprise WHERE id=%s", (enterprise_id,))
    realname_row = sc.fetchone()
    realname = realname_row[0] if realname_row else None
    if realname:
        sc.execute("""SELECT factoring_limit FROM mini_actual_ctr
                      WHERE user_name=%s AND mark=1
                      ORDER BY id DESC LIMIT 1""", (realname,))
        ctrl_row = sc.fetchone()
        ctrl_limit = float(ctrl_row[0] or 0) if ctrl_row else 0
        # 实控人维度在途余额（所有项目所有周期未回款，截止 apply_date）
        sc.execute("""SELECT COALESCE(SUM(lr.amount - COALESCE(lr.returned_amount, 0)), 0)
                      FROM mini_loan_record lr
                      JOIN mini_project mp ON lr.project_id=mp.id
                      JOIN biz_enterprise be ON mp.sid=be.id
                      WHERE be.realname=%s AND lr.mark=1 AND mp.mark=1
                        AND DATE(lr.pay_time) < %s
                        AND lr.to_be_return_amount > 0""", (realname, apply_date))
        in_flight = float((sc.fetchone() or [0])[0] or 0)
        out['#15_授信余额'] = round(ctrl_limit - in_flight, 2)
        out['_控制人'] = realname
        out['_授信总额'] = ctrl_limit
        out['_实控人在途'] = in_flight
    else:
        out['#15_授信余额'] = None
    src.close()

    # ===== #16 客户申请金额 = 用户传入；未传入则取账单上限 =====
    out['#16_客户申请金额'] = round(float(apply_amount), 2) if apply_amount > 0 else out['#2_账单出款上限']

    # ===== #17 最终出款 = min(#13 - #14, #15 授信余额, #16 客户申请金额) =====
    final_limits = [out['#13_本项目出款金额'], out['#16_客户申请金额']]
    if out['#15_授信余额'] is not None:
        final_limits.append(out['#15_授信余额'])
    out['#17_最终出款'] = round(min(final_limits), 2) if out['#13_本项目出款金额'] > 0 else None

    return out


def _fmt(v):
    if v is None: return '            --'
    return f'{v:>14,.2f}'


def print_summary(out):
    print(f"\n{'='*60}")
    print(f"项目 {out['project_id']} 业务月 {out['business_month']} (申请日 {out['apply_date']})")
    print(f"出款比例: {out['profit_ratio']}")
    print(f"{'='*60}")
    print(f"  #1  甲方账单金额         {_fmt(out['#1_甲方账单'])}  ({out['#1_来源']})")
    print(f"  #2  账单:出款上限        {_fmt(out['#2_账单出款上限'])}")
    print(f"  #3  发薪流水:金额        {_fmt(out['#3_发薪流水金额'])}")
    print(f"  #4  发薪流水:已直发      {_fmt(out['#4_已直发'])}")
    print(f"  #5  发薪流水:出款上限    {_fmt(out['#5_发薪出款上限'])}")
    print(f"  #6  工资表:结算工资      {_fmt(out['#6_工资表结算工资'])}")
    print(f"  #7  工资表:本月匹配率    {_fmt(out['#7_本月匹配率'])}")
    print(f"  #8  工资表:匹配金额      {_fmt(out['#8_工资表匹配金额'])}")
    print(f"  #9  工资表:预计直发      {_fmt(out['#9_工资表预计直发'])}")
    print(f"  #10 工资表:上月匹配率    {_fmt(out['#10_上月匹配率'])}")
    print(f"  #11 工资表:出款上限      {_fmt(out['#11_工资表出款上限'])}")
    print(f"  #12 本业务周期已垫付     {_fmt(out['#12_本周期已垫付'])}")
    print(f"  #13 本项目出款金额       {_fmt(out['#13_本项目出款金额'])}")
    print(f"  #14 代收超额扣减         {_fmt(out['#14_代收超额扣减'])}")
    print(f"  #15 授信余额             {_fmt(out['#15_授信余额'])}")
    print(f"  #16 客户申请金额         {_fmt(out['#16_客户申请金额'])}")
    print(f"  #17 最终出款             {_fmt(out['#17_最终出款'])}")
    print(f"{'='*60}")


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--project-id', type=int, required=True)
    ap.add_argument('--business-month', required=True)
    ap.add_argument('--apply-date', help='YYYY-MM-DD，默认业务月最后一天')
    ap.add_argument('--apply-time', help='YYYY-MM-DD HH:MM:SS（精确到时刻，对齐人工快照）')
    ap.add_argument('--apply-amount', type=float, default=0, help='本项目出款金额（#13）')
    ap.add_argument('--data-mode', default='wage_and_payroll',
                    choices=['wage_and_payroll', 'payroll_only', 'wage_only'],
                    help='数据采用模式（影响 #13）')
    args = ap.parse_args()
    out = calc_payment_summary(args.project_id, args.business_month,
                                apply_date=args.apply_date,
                                apply_time=args.apply_time,
                                apply_amount=args.apply_amount,
                                data_mode=args.data_mode)
    # 按 finance_mode 走对应 print
    if out.get('finance_mode') == 'prepay':
        from etl.calc.prepay import print_summary as print_prepay
        print_prepay(out)
    else:
        print_summary(out)
