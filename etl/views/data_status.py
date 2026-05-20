"""4 类数据卡片视图（出款申请右侧用）

输入：project_id, business_month (YYYY-MM), apply_date (YYYY-MM-DD), mode ('normal'|'prepay')
输出：dict，按 mode 返回对应卡片：
    normal: {kaoqin, bill, payroll, wage}
    prepay: {kaoqin, payroll}（无账单/无工资表）

业务定义（"什么算最新""怎么拼月覆盖文案"）都在这里，web 不参与。
"""
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts._db import connect
from etl._utils import get_business_cycle, derive_business_period


def _to_date(v):
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    if isinstance(v, str):
        return datetime.strptime(v.split(' ')[0], '%Y-%m-%d').date()
    return None


def _fmt_date(d):
    return d.strftime('%Y-%m-%d') if d else None


def _fmt_md_range(d1, d2, with_days=False):
    if not d1 or not d2:
        return None
    s = f'{d1.strftime("%m-%d")} ~ {d2.strftime("%m-%d")}'
    if with_days:
        days = (d2 - d1).days + 1
        s += f'（{days} 天）'
    return s


def _kaoqin_normal(cur, project_id, business_month):
    """优先看日级 attendance；没有日级数据时 fallback 到 attendance_summary。
    主指标按 hours/quantity 哪个有值决定（计件项目 hours=NULL，显示总单量）。
    calc-time validity：取行 + apply_validity + 内存聚合"""
    from etl.calc._validity_filter import fetch_attendance_rows
    att_rows = fetch_attendance_rows(
        cur, project_id,
        where_extra=' AND business_month=%s',
        where_args=[business_month],
        extra_select_cols=['worker_id'])
    valid_rows = [r for r in att_rows if r.get('is_valid', 1)]
    rows = len(valid_rows)
    workers = len({r['worker_id'] for r in valid_rows if r.get('worker_id') is not None})
    hours_a = sum(float(r.get('hours') or 0) for r in valid_rows)
    qty_a = sum(float(r.get('quantity') or 0) for r in valid_rows)
    dates = [r['shift_date'] for r in valid_rows if r.get('shift_date')]
    dmin = min(dates) if dates else None
    dmax = max(dates) if dates else None
    latest = dmax

    cur.execute("""
        SELECT COUNT(DISTINCT worker_id), SUM(hours)
        FROM attendance_summary
        WHERE project_id=%s AND business_month=%s
    """, (project_id, business_month))
    s_workers, hours_s = cur.fetchone()

    has_daily = rows > 0
    has_summary = bool(s_workers and s_workers > 0)

    def _primary(hours, qty):
        h = float(hours or 0)
        q = float(qty or 0)
        if h <= 0 and q > 0:
            return round(q, 2), 'quantity'
        return round(h, 2), 'hours'

    if has_daily:
        val, unit = _primary(hours_a, qty_a)
        return {
            'has_data': True,
            'latest_date': _fmt_date(latest),
            'rows': int(rows),
            'workers': int(workers or 0),
            'month_range_str': _fmt_md_range(dmin, dmax, with_days=True),
            'total_hours': val if unit == 'hours' else 0.0,
            'total_quantity': val if unit == 'quantity' else 0.0,
            'primary_unit': unit,
            'source_note': '日级（含节假日工时）' if unit == 'hours' else '日级（计件项目）',
        }
    if has_summary:
        return {
            'has_data': True,
            'latest_date': '月汇总（无日级）',
            'rows': int(s_workers),
            'workers': int(s_workers),
            'month_range_str': '整月汇总',
            'total_hours': round(float(hours_s or 0), 2),
            'total_quantity': 0.0,
            'primary_unit': 'hours',
            'source_note': '月汇总（无日级）',
        }
    return {
        'has_data': False,
        'latest_date': None, 'rows': 0, 'workers': 0,
        'month_range_str': None, 'total_hours': 0.0,
        'total_quantity': 0.0, 'primary_unit': 'hours',
        'source_note': None,
    }


def _kaoqin_prepay(cur, project_id, business_month, apply_date):
    """预付模式：按申请日近 7 日 + 当月范围两块数据。calc-time validity

    近 7 日金额聚合：attendance × unit_prices（与 prepay.py 的 #2 基准日逻辑一致），
    作为前端预付预览的"基准日金额"数据源。
    """
    from etl.calc._validity_filter import (fetch_attendance_rows,
                                            fetch_unit_prices, match_unit_price,
                                            row_amount)
    d_from = apply_date - timedelta(days=6)

    att_7d = fetch_attendance_rows(
        cur, project_id,
        where_extra=' AND shift_date BETWEEN %s AND %s',
        where_args=[d_from, apply_date])
    valid_7d = [r for r in att_7d if r.get('is_valid', 1)]
    rows_7d = len(valid_7d)
    hours_7d = sum(float(r.get('hours') or 0) for r in valid_7d)

    unit_prices = fetch_unit_prices(cur, project_id, ref_date=apply_date)
    daily_amt = {}
    for r in valid_7d:
        price, unit = match_unit_price(unit_prices, r)
        if not price:
            continue
        d = r.get('shift_date')
        if not d:
            continue
        daily_amt[d] = daily_amt.get(d, 0) + row_amount(r, unit) * price

    peak_amt = max(daily_amt.values()) if daily_amt else 0
    nonzero = [v for v in daily_amt.values() if v > 0]
    avg_amt = sum(nonzero) / len(nonzero) if nonzero else 0
    latest_amt = daily_amt.get(apply_date - timedelta(days=1), 0)

    att_bm = fetch_attendance_rows(
        cur, project_id,
        where_extra=' AND business_month=%s',
        where_args=[business_month])
    valid_bm = [r for r in att_bm if r.get('is_valid', 1)]
    dates = [r['shift_date'] for r in valid_bm if r.get('shift_date')]
    dmin = min(dates) if dates else None
    dmax = max(dates) if dates else None
    latest = dmax

    has_data = rows_7d > 0 or latest is not None
    return {
        'has_data': has_data,
        'latest_date': _fmt_date(latest),
        'rows_7d': rows_7d,
        'hours_7d': round(hours_7d, 2),
        'month_range_str': _fmt_md_range(dmin, dmax, with_days=False),
        'last7d_peak': round(peak_amt, 2),
        'last7d_avg': round(avg_amt, 2),
        'last7d_latest': round(latest_amt, 2),
    }


def _bill(cur, project_id, business_month, business_cycle_str):
    cur.execute("""
        SELECT MAX(ingested_at), SUM(amount)
        FROM bill_totals
        WHERE project_id=%s AND business_month=%s
    """, (project_id, business_month))
    received, amount = cur.fetchone()
    cur.execute("""
        SELECT COUNT(DISTINCT worker_id)
        FROM bill_persons
        WHERE project_id=%s AND business_month=%s
    """, (project_id, business_month))
    person_count = cur.fetchone()[0]

    has_data = (amount is not None) or (person_count and person_count > 0)
    return {
        'has_data': bool(has_data),
        'business_period_str': f'{business_month}（{business_cycle_str}）',
        'received_date': _fmt_date(_to_date(received)),
        'amount': round(float(amount), 2) if amount else 0.0,
        'person_count': int(person_count or 0),
    }


def _payroll_normal(cur, project_id, business_month):
    # calc-time validity：取行 + 实时跑规则 + count_as_faxin=1 内存过滤
    from etl.calc._validity_filter import apply_calc_validity, parse_extra_data
    cur.execute("""
        SELECT name_raw, work_amount, payroll_kind, pay_time, extra_data, worker_id, source_type
        FROM payrolls
        WHERE project_id=%s AND business_month=%s
    """, (project_id, business_month))
    raw = cur.fetchall()
    dict_rows = [{
        'name_raw': r[0], 'work_amount': r[1], 'payroll_kind': r[2],
        'pay_time': r[3], 'extra_data': parse_extra_data(r[4]), '_worker_id': r[5],
        'source_type': r[6],
    } for r in raw]
    dict_rows = apply_calc_validity(cur, project_id, 'payroll', dict_rows)
    valid_rows = [r for r in dict_rows if r.get('count_as_faxin', 1)]

    paid_amount = sum(float(r['work_amount'] or 0) for r in valid_rows)
    paid_count = len({r['_worker_id'] for r in valid_rows if r['_worker_id'] is not None})
    pts = [r['pay_time'] for r in valid_rows if r['pay_time']]
    latest = max(pts) if pts else None
    dmin = min(pts) if pts else None
    dmax = latest

    cur.execute("""
        SELECT COUNT(DISTINCT worker_id)
        FROM bill_persons
        WHERE project_id=%s AND business_month=%s
    """, (project_id, business_month))
    bill_count = cur.fetchone()[0] or 0

    paid_amount = round(paid_amount, 2)
    unmatched = max(bill_count - paid_count, 0)
    coverage_pct = round(paid_count / bill_count * 100, 1) if bill_count else None

    return {
        'has_data': paid_count > 0 or paid_amount > 0,
        'latest_date': _fmt_date(_to_date(latest)),
        'month_range_str': _fmt_md_range(_to_date(dmin), _to_date(dmax), with_days=False),
        'paid_amount': paid_amount,
        'paid_count': paid_count,
        'bill_count': bill_count,
        'unmatched_count': unmatched,
        'coverage_pct': coverage_pct,
    }


def _payroll_prepay(cur, project_id, business_month, apply_date):
    """预付模式：当月发薪 + 近 7 日明细 + 峰值/平均/最近
    calc-time validity：取行 + 实时跑规则 + count_as_faxin=1 内存过滤"""
    from etl.calc._validity_filter import apply_calc_validity, parse_extra_data
    cur.execute("""
        SELECT name_raw, work_amount, payroll_kind, pay_time, extra_data, source_type
        FROM payrolls
        WHERE project_id=%s AND business_month=%s
    """, (project_id, business_month))
    raw = cur.fetchall()
    dict_rows = [{
        'name_raw': r[0], 'work_amount': r[1], 'payroll_kind': r[2],
        'pay_time': r[3], 'extra_data': parse_extra_data(r[4]), 'source_type': r[5],
    } for r in raw]
    dict_rows = apply_calc_validity(cur, project_id, 'payroll', dict_rows)
    valid_rows = [r for r in dict_rows if r.get('count_as_faxin', 1)]

    pts = [r['pay_time'] for r in valid_rows if r['pay_time']]
    latest = max(pts) if pts else None
    month_amount = sum(float(r['work_amount'] or 0) for r in valid_rows)
    month_rows = len(valid_rows)

    d_from = apply_date - timedelta(days=6)
    by_day = {}
    for r in valid_rows:
        pt = r['pay_time']
        if pt is None:
            continue
        d = pt.date() if hasattr(pt, 'date') else pt
        if d < d_from or d > apply_date:
            continue
        amt, cnt = by_day.get(d, (0.0, 0))
        by_day[d] = (amt + float(r['work_amount'] or 0), cnt + 1)

    detail = []
    peak_amt, peak_d = -1, None
    for i in range(7):
        d = d_from + timedelta(days=i)
        amt, cnt = by_day.get(d, (0.0, 0))
        detail.append({'date': d.strftime('%m-%d'), 'iso_date': d.isoformat(),
                       'amount': round(amt, 2), 'count': cnt, 'marker': None})
        if amt > peak_amt:
            peak_amt, peak_d = amt, d

    if peak_d is not None and peak_amt > 0:
        for it in detail:
            if it['iso_date'] == peak_d.isoformat():
                it['marker'] = 'peak'
                break
    if detail:
        detail[-1]['marker'] = 'latest' if detail[-1]['marker'] != 'peak' else 'peak+latest'

    nonzero = [it['amount'] for it in detail if it['amount'] > 0]
    avg_amt = round(sum(nonzero) / len(nonzero), 2) if nonzero else 0.0
    latest_amt = detail[-1]['amount'] if detail else 0.0

    return {
        'has_data': bool(latest) or any(nonzero),
        'latest_date': _fmt_date(_to_date(latest)),
        'month_paid_amount': round(float(month_amount or 0), 2),
        'month_rows': int(month_rows or 0),
        'last7d_detail': detail,
        'last7d_peak': round(peak_amt if peak_amt > 0 else 0, 2),
        'last7d_avg': avg_amt,
        'last7d_latest': latest_amt,
    }


def _wage(cur, project_id, business_month):
    cur.execute("""
        SELECT MAX(ingested_at), SUM(payable_amount), COUNT(DISTINCT worker_id)
        FROM wage_sheets
        WHERE project_id=%s AND business_month=%s
    """, (project_id, business_month))
    received, payable, person_count = cur.fetchone()

    return {
        'has_data': bool(payable) or bool(person_count),
        'business_month': business_month,
        'received_date': _fmt_date(_to_date(received)),
        'payable_total': round(float(payable or 0), 2),
        'person_count': int(person_count or 0),
    }


def get_data_status(project_id, business_month, apply_date, mode='normal'):
    """4 类数据卡片：mode='normal' 返回 4 卡片；'prepay' 返回考勤+发薪 2 卡片。"""
    apply_date = _to_date(apply_date)
    project_id = int(project_id)

    conn = connect('fish-test')
    try:
        cur = conn.cursor()
        cycle_str = get_business_cycle(cur, project_id, ref_date=apply_date)

        if mode == 'prepay':
            return {
                'mode': 'prepay',
                'project_id': str(project_id),
                'business_month': business_month,
                'apply_date': apply_date.isoformat() if apply_date else None,
                'business_cycle': cycle_str,
                'kaoqin': _kaoqin_prepay(cur, project_id, business_month, apply_date),
                'payroll': _payroll_prepay(cur, project_id, business_month, apply_date),
            }

        return {
            'mode': 'normal',
            'project_id': str(project_id),
            'business_month': business_month,
            'apply_date': apply_date.isoformat() if apply_date else None,
            'business_cycle': cycle_str,
            'kaoqin': _kaoqin_normal(cur, project_id, business_month),
            'bill': _bill(cur, project_id, business_month, cycle_str),
            'payroll': _payroll_normal(cur, project_id, business_month),
            'wage': _wage(cur, project_id, business_month),
        }
    finally:
        conn.close()


if __name__ == '__main__':
    import json
    proj = sys.argv[1] if len(sys.argv) > 1 else None
    if not proj:
        from etl.views.projects import list_projects
        regs = [p for p in list_projects(status='registered')]
        if not regs:
            print('没有 registered 项目；先在 web 注册一个再测')
            sys.exit(0)
        proj = regs[0]['project_id']
        print(f'用第一个已注册项目 {proj}')

    print('--- normal mode ---')
    print(json.dumps(
        get_data_status(proj, '2026-04', '2026-05-05', 'normal'),
        ensure_ascii=False, indent=2, default=str))
    print('--- prepay mode ---')
    print(json.dumps(
        get_data_status(proj, '2026-05', '2026-05-05', 'prepay'),
        ensure_ascii=False, indent=2, default=str))
