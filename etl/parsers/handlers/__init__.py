"""handler 注册表：sheet 解析的实际实现按 handler 名注册到这里。

handler 名来源 = project_classify_rules.handler 字段 / DEFAULT_RULES。

调用约定：
    fn(ws, *, kind, column_mapping=None, **ctx) -> list[dict]
    ctx 可能包含 sheet_name / business_month / fallback_bm / laowu_keywords 等

step 4: 'standard' handler 实现（按 column_mapping 解析横平竖直明细表）
step 6a: specialized handlers 包装现有 parse_attendance 内特殊格式 parser（无逻辑改写）
step 6b/c/d: standard handler 扩 bill / wage_sheet / payroll
"""
from etl.parsers.handlers.standard import parse as standard_parse
from etl.parsers.handlers import specialized as _sp


HANDLERS = {
    'standard':                  standard_parse,
    'monthly_horizontal_kld':    _sp.monthly_horizontal_kld,
    'monthly_horizontal_xgy':    _sp.monthly_horizontal_xgy,
    'monthly_horizontal_changqi': _sp.monthly_horizontal_changqi,
    'hotel_monthly_horizontal':  _sp.hotel_monthly_horizontal,
    'duty_symbolic':             _sp.duty_symbolic,
    'shunfeng_clock_pair':       _sp.shunfeng_clock_pair,
    # bill 特殊模式
    'bill_project_summary':      _sp.bill_project_summary,
    'bill_dept_subtotal':        _sp.bill_dept_subtotal,
    # 兜底候选（DEFAULT_RULES 暂未引用，留位）
    'dakaq':                     _sp.dakaq,
    'wanhui_chuqin':             _sp.wanhui_chuqin,
    'rijiegong':                 _sp.rijiegong,
    'liying':                    _sp.liying,
}


def get_handler(name):
    """取 handler；未注册返回 None（dispatcher 决定怎么处理：报错 / fallback / 写 pending）"""
    return HANDLERS.get(name)

