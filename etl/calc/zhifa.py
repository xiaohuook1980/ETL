"""规则1：已直发金额计算（fish-test 版，只读 mart 层）

参考 .skill-docs/数据分析/规则1-直发金额.md + memory feedback_bill_over_attendance：
  - 已发薪 = 业务周期内全部有效发薪流水（payroll_kind ∈ {normal, loan}）
  - 已直发 = 已发薪 ∩ "考勤名单"（按 name_raw 匹配）
  - **名单优先级（账单 > 考勤）**：
      bill_persons (该业务月有数据) → 用 bill_persons 名单 + bill_persons.amount 做代收封顶
      否则 fallback → attendance 名单（无 kq 金额，跳过封顶判定）
  - 代收人封顶：若 paid - kq > daishou_threshold（默认 2000）→ 该人按 kq 封顶
  - 同名异人封顶：传入 ambig_names 集合时，paid > kq 的同名异人按 kq 封顶
  - 可选 apply_time 参数：过滤 pay_time <= apply_time（对齐人工导出快照时点）

数据源（零接触 raw 层）：
  fish-test.payrolls       # 已标准化的发薪事实
  fish-test.bill_persons   # 账单人员金额（优先名单 + kq）
  fish-test.attendance     # 考勤事实（fallback 名单）
  fish-test.projects       # 项目配置

用法：
  python etl/calc/zhifa.py --project-id 1986627402054696961 --business-month 2026-04
  python etl/calc/zhifa.py ... --apply-time '2026-04-30 18:00:00'   # 精确到时刻
"""
import sys, argparse
from datetime import datetime
sys.path.insert(0, 'D:/小鱼AI数据')
from scripts._db import connect
from etl._utils import get_business_cycle


def calc_zhifa(project_id, business_month, apply_time=None,
               daishou_threshold=2000, ambig_names=None):
    """apply_time: datetime 或 'YYYY-MM-DD HH:MM:SS' 字符串；指定后过滤 pay_time <= apply_time
    daishou_threshold: 代收阈值（默认 2000）
    ambig_names: 可选，同名异人姓名集合
    """
    conn = connect('fish-test')
    cur = conn.cursor()

    cur.execute("SELECT enterprise_id FROM projects WHERE id=%s", (project_id,))
    proj = cur.fetchone()
    if not proj:
        raise RuntimeError(f'项目 {project_id} 未 seed')
    enterprise_id = proj[0]
    business_cycle = get_business_cycle(cur, project_id)

    if apply_time and isinstance(apply_time, str):
        apply_time = datetime.strptime(apply_time, '%Y-%m-%d %H:%M:%S')

    print(f'\n=== 项目 {project_id} 业务月 {business_month} ===')
    print(f'  business_cycle: {business_cycle}')
    if apply_time:
        print(f'  apply_time: {apply_time} (过滤 pay_time <=)')

    # ===== 名单 + 每人金额（账单优先，考勤 fallback，无名单时上月发薪 fallback）=====
    cur.execute("""SELECT name_raw, SUM(amount) FROM bill_persons
                   WHERE project_id=%s AND business_month=%s
                   GROUP BY name_raw""", (project_id, business_month))
    bill_rows = cur.fetchall()
    if bill_rows:
        kq_names = set(r[0] for r in bill_rows)
        bill_person_amt = {r[0]: float(r[1]) for r in bill_rows}
        kq_source = 'bill_persons'
    else:
        # attendance 名单 = 日级 attendance ∪ 月汇总 attendance_summary
        # 过滤按 attendance_filters（见 etl/_kaoqin_filter）
        from etl._kaoqin_filter import build_attendance_where
        wh1, args1 = build_attendance_where(cur, project_id, table_alias='')
        sql_att = f"""SELECT DISTINCT name_raw FROM attendance
                      WHERE project_id=%s AND business_month=%s AND is_valid=1 {wh1}"""
        sql_sum = f"""SELECT DISTINCT name_raw FROM attendance_summary
                      WHERE project_id=%s AND business_month=%s AND is_valid=1 {wh1}"""
        cur.execute(sql_att + ' UNION ' + sql_sum,
                    [project_id, business_month] + args1 +
                    [project_id, business_month] + args1)
        att_names = set(r[0] for r in cur.fetchall())
        if att_names:
            kq_names = att_names
            bill_person_amt = None
            kq_source = 'attendance (fallback)'
        else:
            # 计件等无人员名单项目：用"上月发薪人员"作为名单
            # 业务规则：直发 = 本月发薪 ∩ 上月发薪 在本月的金额（"老员工"过滤新人/代收）
            y, m = int(business_month[:4]), int(business_month[5:7])
            prev_y, prev_m = (y, m - 1) if m > 1 else (y - 1, 12)
            prev_bm = f'{prev_y:04d}-{prev_m:02d}'
            cur.execute("""SELECT DISTINCT name_raw FROM payrolls
                           WHERE project_id=%s AND business_month=%s""",
                        (project_id, prev_bm))
            kq_names = set(r[0] for r in cur.fetchall())
            bill_person_amt = None
            kq_source = f'last_month_payrolls ({prev_bm})' if kq_names \
                       else f'last_month_payrolls EMPTY ({prev_bm} 无数据)'
    print(f'  名单来源: {kq_source} | 人数: {len(kq_names)}')

    # ===== payrolls 取数（calc-time validity：取 raw row + extra_data，内存跑规则）=====
    # mart 里的 is_valid/count_as_faxin 字段不再可信（validity 已挪到 calc 层），
    # 每次跑 calc 时按当前 project_validity_rules 实时判定 → 改规则即时生效，不用重 parse。
    sql = """SELECT name_raw, work_amount, payroll_kind, pay_time, extra_data, source_type
             FROM payrolls
             WHERE project_id=%s AND business_month=%s"""
    args = [project_id, business_month]
    if apply_time:
        sql += " AND pay_time <= %s"
        args.append(apply_time)
    cur.execute(sql, args)
    raw_rows = cur.fetchall()

    # 转 dict + 跑 validity（apply_validity 输入 row 用 dict 格式）
    from etl.calc._validity_filter import apply_calc_validity, parse_extra_data
    dict_rows = [{
        'name_raw': r[0], 'work_amount': r[1], 'payroll_kind': r[2],
        'pay_time': r[3], 'extra_data': parse_extra_data(r[4]), 'source_type': r[5],
    } for r in raw_rows]
    dict_rows = apply_calc_validity(cur, project_id, 'payroll', dict_rows)
    # 装回 tuple 形态（下面循环原本就按 5 字段解包）
    rows = [(r['name_raw'], r['work_amount'], r['payroll_kind'],
             r.get('is_valid', 1), r.get('count_as_faxin', 1)) for r in dict_rows]

    # 按 name + kind 累加每人 paid（baseline 已直发=班次名解析日期∈月内∩名单）
    # 已直发口径：shift_dated（日级精确）+ bill_month_only（月级精确，达达类无时间字段项目）
    # 排除：pay_time_based 等（fallback 路径）
    DATED_KINDS = ('shift_dated', 'bill_month_only')
    paid_shift_per_person = {}     # DATED_KINDS 且 is_valid=1（用于已直发计算）
    paid_total_per_person = {}     # is_valid=1 全部 kind 之和（用于代收/同名异人封顶判定）
    yi_faxin_shift = yi_faxin_paytime = 0
    yi_non_salary = 0
    n_in = n_out = 0
    for name, work, kind, is_valid, count_as_faxin in rows:
        amt = float(work or 0)
        # count_as_faxin=0：非工资条目（押金/补贴/借支等，按 validity 规则配的"不计入已发薪"标识）
        if not count_as_faxin:
            yi_non_salary += amt
            continue
        # 已发薪：含所有工资行（含 is_valid=0 代收）
        if kind in DATED_KINDS:
            yi_faxin_shift += amt
        else:
            yi_faxin_paytime += amt
        if not is_valid:
            continue
        if name in kq_names:
            paid_total_per_person[name] = paid_total_per_person.get(name, 0) + amt
            if kind in DATED_KINDS:
                paid_shift_per_person[name] = paid_shift_per_person.get(name, 0) + amt
            n_in += 1
        else:
            n_out += 1
    yi_faxin = yi_faxin_shift + yi_faxin_paytime

    # ===== 代收人/同名异人封顶（仅 bill_persons 路径有 kq 金额可比）=====
    # baseline 已直发口径：班次名解析日期∈月内 ∩ 名单内（按 shift_dated kind 过滤）
    daishou_records = []
    if bill_person_amt is not None:
        ambig_set = ambig_names or set()
        yi_zhifa = 0
        for nm, paid_shift in paid_shift_per_person.items():
            paid_total = paid_total_per_person.get(nm, 0)
            kq = bill_person_amt.get(nm, 0)
            # 封顶判定按 paid_total（全 kind 之和）；累加按 paid_shift（仅 shift_dated）
            if paid_total - kq > daishou_threshold:
                yi_zhifa += min(paid_shift, kq)
                daishou_records.append({'姓名': nm, '发薪': paid_total, '考勤金额': kq, '差额': paid_total - kq, '类型': '代收人'})
            elif nm in ambig_set and paid_total > kq:
                yi_zhifa += min(paid_shift, kq)
                daishou_records.append({'姓名': nm, '发薪': paid_total, '考勤金额': kq, '差额': paid_total - kq, '类型': '同名异人'})
            else:
                yi_zhifa += paid_shift
    else:
        # attendance 名单 fallback：没有 kq 金额，按 shift_dated 累加（全 kind 算"已发薪"）
        yi_zhifa = sum(paid_shift_per_person.values())

    print(f'\n=== 业务周期 {business_month} 内（payrolls 表）===')
    print(f'  shift_dated (按工时日):     {yi_faxin_shift:>12,.2f}')
    print(f'  pay_time_based (fallback): {yi_faxin_paytime:>12,.2f}')
    if yi_non_salary > 0:
        print(f'  非工资条目 (count_as_faxin=0):{yi_non_salary:>12,.2f}  (不算入已发薪)')
    print(f'  ---------------------------------')
    print(f'  已发薪合计:                 {yi_faxin:>12,.2f}')
    print(f'  ---------------------------------')
    print(f'  名单内 {n_in:>4d} 人次  -> 已直发  {yi_zhifa:>12,.2f}')
    print(f'  名单外 {n_out:>4d} 人次  -> 不计入')
    if daishou_records:
        print(f'  代收/同名异人封顶: {len(daishou_records)} 人, 封顶差额合计 {sum(r["差额"] for r in daishou_records):,.2f}')
    conn.close()
    return yi_faxin, yi_zhifa


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--project-id', type=int, required=True)
    ap.add_argument('--business-month', required=True)
    ap.add_argument('--apply-time', help='YYYY-MM-DD HH:MM:SS 精确到时刻')
    args = ap.parse_args()
    calc_zhifa(args.project_id, args.business_month, apply_time=args.apply_time)
