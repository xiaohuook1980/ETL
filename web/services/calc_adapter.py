"""出款计算薄适配器：调 etl.calc.* + 拼说明文案给报告页

不再加载 scripts/payment_calc_tool_v2.py（已退役）。
所有业务逻辑都在 etl.calc.payment_summary / prepay。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from etl.calc.payment_summary import _calc_normal_17
from etl.calc.prepay import calc_prepay


def run_calc(project_id, business_month, apply_date=None, mode='normal',
             apply_time=None, apply_amount=0,
             data_mode='wage_and_payroll',
             customer_amount=None,
             calc_formula=None,
             base_day_mode='peak', prepay_days=7, base_day_date=None):
    """统一入口：按 calc_formula 选公式。
    - 'normal1' (默认 normal): etl.calc.payment_summary 17 项
    - 'prepay1' (默认 prepay): etl.calc.prepay 13 项
    - 'prepay2': 待告知，未实现
    返回 {'detail': dict, 'online': None, 'log': '', 'error': str|None, 'raw': dict}
    """
    if calc_formula is None:
        calc_formula = 'prepay1' if mode == 'prepay' else 'normal1'

    try:
        if calc_formula == 'prepay1':
            raw = calc_prepay(project_id, business_month,
                              apply_date=apply_date, apply_time=apply_time,
                              base_day_mode=base_day_mode,
                              prepay_days=prepay_days,
                              base_day_date=base_day_date)
            detail = _build_prepay_detail(raw, customer_amount)
        elif calc_formula == 'prepay2':
            return {'detail': {}, 'online': None, 'log': '',
                    'error': '计算逻辑预付2 待用户告知具体逻辑，尚未实现',
                    'raw': {}, 'calc_formula': calc_formula}
        elif calc_formula == 'normal1':
            raw = _calc_normal_17(project_id, business_month, apply_date,
                                  apply_time, apply_amount or 0, data_mode)
            detail = _build_normal_detail(raw, customer_amount)
        else:
            return {'detail': {}, 'online': None, 'log': '',
                    'error': f'未知计算公式: {calc_formula}',
                    'raw': {}, 'calc_formula': calc_formula}
    except Exception as e:
        return {'detail': {}, 'online': None, 'log': '',
                'error': f'{type(e).__name__}: {e}', 'raw': {},
                'calc_formula': calc_formula}

    return {'detail': detail, 'online': None, 'log': '',
            'error': None, 'raw': _serializable(raw),
            'calc_formula': calc_formula}


def _build_normal_detail(r, customer_amount):
    """普通 17 项：把 etl/calc 输出的 #N 字段铺平到 detail，附 _说明"""
    profit = r.get('profit_ratio', 0.8)
    bill = r.get('#1_甲方账单')
    bill_src = r.get('#1_来源', '')
    bill_limit = r.get('#2_账单出款上限')
    yi_faxin = r.get('#3_发薪流水金额')
    yi_zhifa = r.get('#4_已直发')
    payroll_limit = r.get('#5_发薪出款上限')
    wage = r.get('#6_工资表结算工资')
    match_curr = r.get('#7_本月匹配率')
    wage_match = r.get('#8_工资表匹配金额')
    wage_pred = r.get('#9_工资表预计直发')
    match_prev = r.get('#10_上月匹配率')
    wage_limit = r.get('#11_工资表出款上限')
    advanced = r.get('#12_本周期已垫付', 0) or 0
    proj_payout = r.get('#13_本项目出款金额')
    deduct = r.get('#14_代收超额扣减')
    credit_remain = r.get('#15_授信余额')
    apply_amt = r.get('#16_客户申请金额')
    final_calc = r.get('#17_最终出款')

    final = _apply_customer(final_calc, customer_amount)

    # 单价文案（用于报告页 header 展示）；calc 层已生成 → 直接用
    up_label = r.get('_unit_price_label') or '未配置'
    unit_prices = r.get('_unit_prices') or []

    return {
        '#1_来源': bill_src or '',
        '_unit_price_label': up_label,
        '_unit_prices': unit_prices,
        '#1_甲方账单': bill,
        '#1_甲方账单_说明': f'账单来源：{bill_src or "—"}',
        '#2_账单出款上限': bill_limit,
        '#2_账单出款上限_说明': f'账单 {_n(bill)} × 出款比例 {profit} = {_n(bill_limit)}',
        '#3_发薪流水金额': yi_faxin,
        '#3_发薪流水金额_说明': '本项目本业务周期发薪合计',
        '#4_已直发': yi_zhifa,
        '#4_已直发_说明': '发薪流水 ∩ 账单/考勤名单（代收人按考勤封顶）',
        '#5_发薪出款上限': payroll_limit,
        '#5_发薪出款上限_说明': f'min(已直发 {_n(yi_zhifa)} ÷ 出款比例 {profit}, 发薪流水金额 {_n(yi_faxin)})',
        '#6_工资表结算工资': wage,
        '#6_工资表结算工资_说明': '本月工资表合计（应发）',
        '#7_本月匹配率': f'{match_curr:.1f}%' if match_curr is not None else None,
        '#7_本月匹配率_说明': '工资表 ∩ 本月账单人员 / 工资表',
        '#8_工资表匹配金额': wage_match,
        '#8_工资表匹配金额_说明': '工资表中姓名出现在账单/考勤名单内的金额合计',
        '#9_工资表预计直发': wage_pred,
        '#9_工资表预计直发_说明': '逐人 max(0, 工资表 − 已发) 汇总（仅在账单/考勤名单内）',
        '#10_上月匹配率': f'{match_prev:.1f}%' if match_prev is not None else None,
        '#10_上月匹配率_说明': '本月工资表 ∩ 上月账单人员 / 工资表（反欺诈信号）',
        '#11_工资表出款上限': wage_limit,
        '#11_工资表出款上限_说明': f'min(预计直发 {_n(wage_pred)} ÷ 比例 {profit}, 工资表 {_n(wage)})',
        '#12_本周期已垫付': advanced,
        '#12_本周期已垫付_说明': '出款管理表同业务周期未回款累计',
        '#13_本项目出款金额': proj_payout,
        '#13_本项目出款金额_说明': f'min(账单上限, 工资表上限+发薪上限) − 已垫付 → 千位取整 = {_n(proj_payout)}',
        '#14_代收超额扣减': deduct,
        '#14_代收超额扣减_说明': '实控人净超额（未运行检查则为无）',
        '#15_授信余额': credit_remain,
        '#15_授信余额_说明': f'实控人 {r.get("_控制人", "—")}，授信 {_n(r.get("_授信总额"))} − 在途 {_n(r.get("_实控人在途"))}',
        '#16_客户申请金额': customer_amount if customer_amount else apply_amt,
        '#16_客户申请金额_说明': '用户传入；未传入则取账单上限' if not customer_amount else '用户填写',
        '#17_最终出款': final,
        '#17_最终出款_说明': f'min(本项目出款金额, 授信余额, 客户申请) = {_n(final)}',
    }


def _build_prepay_detail(r, customer_amount):
    """预付 12+1 项"""
    profit = r.get('profit_ratio', 0.8)
    v1 = r.get('#1_考勤账单金额')
    v1_src = r.get('#1_来源', '')
    v2 = r.get('#2_预付考勤估计')
    win = r.get('_近窗口范围', r.get('_近7日窗口', ''))
    base_label = r.get('_基准日描述', '—')
    pdays = r.get('_预付天数', '?')
    v3 = r.get('#3_合计')
    v4 = r.get('#4_考勤出款上限')
    v5 = r.get('#5_已发薪金额')
    v6 = r.get('#6_已直发')
    v7 = r.get('#7_已垫付')
    v8 = r.get('#8_账户结余')
    v9 = r.get('#9_项目代收超额')
    v10 = r.get('#10_出款金额')
    v11 = r.get('#11_代收超额扣减')
    v12 = r.get('#12_授信余额')
    v13 = r.get('#13_最终出款')

    final = _apply_customer(v13, customer_amount)

    return {
        '#1_考勤账单金额': v1,
        '#1_考勤账单金额_说明': f'来源：{v1_src or "—"}；总人数 {r.get("#1_总人数", "—")}，总工时 {r.get("#1_总工时", "—")}',
        '#1_总人数': r.get('#1_总人数'),
        '#1_总工时': r.get('#1_总工时'),
        '#2_预付考勤估计': v2,
        '#2_预付考勤估计_说明': f'{base_label} × {pdays} 天 × 出款比例（窗口 {win}，已截到业务周期内）',
        '#3_合计': v3,
        '#3_合计_说明': f'考勤账单 {_n(v1)} + 预付估计 {_n(v2)} = {_n(v3)}',
        '#4_考勤出款上限': v4,
        '#4_考勤出款上限_说明': f'合计 {_n(v3)} × 出款比例 {profit}',
        '#5_已发薪金额': v5,
        '#5_已发薪金额_说明': '班次名解析日期∈业务周期；押金/代发等已排除',
        '#6_已直发': v6,
        '#6_已直发_说明': '已发薪 ∩ 考勤名单（同名异人按工时金额封顶）',
        '#7_已垫付': v7,
        '#7_已垫付_说明': '出款管理表同业务周期未回款累计',
        '#8_账户结余': v8,
        '#8_账户结余_说明': '从 mini_ent_account.loan_surplus_balance 自动读（项目子账户）',
        '#9_项目代收超额': v9,
        '#9_项目代收超额_说明': (
            f'max(0, (已垫付 {_n(v7)} − 账户结余 {_n(v8)}) × {profit} '
            f'− min(考勤账单金额×{profit} {_n((v1 or 0)*profit)}, 已发薪 {_n(v5)}, '
            f'已直发÷{profit} {_n((v6 or 0)/profit) if profit else "—"})) = {_n(v9)}'
        ),
        '#10_出款金额': v10,
        '#10_出款金额_说明': (
            f'考勤出款上限 {_n(v4)} − 已垫付 {_n(v7)} − 项目代收超额 {_n(v9)} = {_n(v10)}'
        ),
        '#11_代收超额扣减': v11,
        '#11_代收超额扣减_说明': '实控人净超额（暂未实现）',
        '#12_授信余额': v12,
        '#12_授信余额_说明': f'实控人 {r.get("_控制人", "—")}，授信 {_n(r.get("_授信总额"))} − 在途',
        '#13_最终出款': final,
        '#13_最终出款_说明': f'min(授信余额, 出款金额)' + (f'，客户申请封顶 {_n(customer_amount)}' if customer_amount else ''),
        '_unit_price_label': r.get('_unit_price_label') or '未配置',
        '_unit_prices': r.get('_unit_prices') or [],
    }


def _apply_customer(final, customer_amount):
    if customer_amount is None or final is None:
        return final
    try:
        return min(final, float(customer_amount))
    except (TypeError, ValueError):
        return final


def _serializable(d):
    if d is None:
        return None
    out = {}
    for k, v in d.items():
        if v is None or isinstance(v, (int, float, str, bool, list)):
            out[k] = v
        elif isinstance(v, dict):
            out[k] = _serializable(v)
        else:
            out[k] = str(v)
    return out


def _n(v):
    if v is None:
        return '--'
    try:
        return f'{float(v):,.0f}'
    except (TypeError, ValueError):
        return str(v)
