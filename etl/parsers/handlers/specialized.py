"""specialized handlers：包装现有 parse_attendance 内特殊格式 parser。

不重写业务逻辑，只做接口适配。每个 handler 的解析行为与旧 _parse 的对应分支等价。

handler 名称对齐 DEFAULT_RULES (etl/classify_default_rules.py) 里的 handler 字段。

handler 接口（统一）：
    fn(ws, *, kind, column_mapping=None, **ctx) -> list[dict]
    column_mapping: specialized 走代码逻辑，通常忽略
    ctx: 可能用到的上下文字段
        - sheet_name:        当前 sheet 名（康丽达月度横向需要）
        - business_month:    'YYYY-MM' 业务月（康丽达/恒众源需要从中推 year/month）
        - fallback_bm:       退而求其次的业务月
        - laowu_keywords:    劳务公司关键词列表（万汇出勤需要用作行级过滤）
"""
import re

from etl.parsers.parse_attendance import (
    parse_yuedu_riziduan,
    parse_yuedu_xinguangyi,
    parse_changqi_hengxiang,
    parse_yuedu_hengxiang,
    parse_yuedu_hekedui,
    parse_shunfeng_jiesuan,
    parse_dakaq,
    parse_wanhui_chuqin,
    parse_rijiegong,
    parse_liying,
)
from etl.parsers.parse_bill import parse_project_summary, parse_dept_subtotal


def _bm_year_month(*candidates):
    """从一组业务月候选字符串里抽 (year, month)，第一个能解析的胜出。"""
    for v in candidates:
        if not v:
            continue
        m = re.match(r'(\d{4})-(\d{1,2})', str(v))
        if m:
            return int(m.group(1)), int(m.group(2))
    return None, None


# ============================================================
# 月度横向系列
# ============================================================
def monthly_horizontal_kld(ws, *, kind, column_mapping=None, **ctx):
    """康丽达月度横向：R3 表头 / R4 'X日'。需要从业务月推 year。"""
    sheet_name = ctx.get('sheet_name')
    year, _ = _bm_year_month(ctx.get('business_month'), ctx.get('fallback_bm'))
    return parse_yuedu_riziduan(ws, sheet_name=sheet_name, year=year)


def monthly_horizontal_xgy(ws, *, kind, column_mapping=None, **ctx):
    """新广益月度横向：含跨月业务周期标题"""
    return parse_yuedu_xinguangyi(ws)


def monthly_horizontal_changqi(ws, *, kind, column_mapping=None, **ctx):
    """澳思美等长期工月度横向（'总工时' 列汇总）"""
    return parse_changqi_hengxiang(ws)


def hotel_monthly_horizontal(ws, *, kind, column_mapping=None, **ctx):
    """梦寺达酒店月度横向模板（R3 部门 / R4 序号+工号+姓名+1..31）"""
    return parse_yuedu_hengxiang(ws)


def duty_symbolic(ws, *, kind, column_mapping=None, **ctx):
    """月度考勤核对表（恒众源/中集，日级符号化 √/半）。需要从业务月推 year+month"""
    year, month = _bm_year_month(ctx.get('business_month'), ctx.get('fallback_bm'))
    return parse_yuedu_hekedui(ws, year=year, month=month)


# ============================================================
# 顺丰系列
# ============================================================
def shunfeng_clock_pair(ws, *, kind, column_mapping=None, **ctx):
    """顺丰结算/出勤（含'结算时长'或'上班打卡时间'）"""
    return parse_shunfeng_jiesuan(ws)


# ============================================================
# bill 特殊模式
# ============================================================
def bill_project_summary(ws, *, kind, column_mapping=None, **ctx):
    """梦寺达酒店项目综合表（用工部门+实发服务费）

    无人员名单，只算 bill_totals。返回单条记录 [{'amount': total, 'is_total_only': True}]
    上层调用方按 is_total_only 写 bill_totals 而非 bill_persons。
    """
    _, total = parse_project_summary(ws)
    if total <= 0:
        return []
    return [{'amount': total, 'is_total_only': True, 'name_raw': None}]


def bill_dept_subtotal(ws, *, kind, column_mapping=None, **ctx):
    """部门小计行（col 0/1=空, col 2=部门名, col 6=金额）

    返回 list of {dept_name, amount}；上层算 totals 用 sum(amount)。
    """
    rows, _total = parse_dept_subtotal(ws)
    # 标准化 row 形态：amount 字段对齐 bill_persons
    return [{'name_raw': r['dept_name'], 'amount': r['amount'],
             'row_idx': r['row_idx'], 'is_dept_subtotal': True} for r in rows]


# ============================================================
# 兜底候选 handler（DEFAULT_RULES 暂未引用，但旧 _parse 用过；留位待 step 6b/c 决定）
# ============================================================
def dakaq(ws, *, kind, column_mapping=None, **ctx):
    """南斗星打卡（'打卡日期'+'上班小时'）— 实际上 standard 也能 cover，留作向后兼容"""
    return parse_dakaq(ws)


def wanhui_chuqin(ws, *, kind, column_mapping=None, **ctx):
    """万汇出勤（'出勤日期'+'结算工时'）"""
    return parse_wanhui_chuqin(ws, laowu_keywords=ctx.get('laowu_keywords') or [])


def rijiegong(ws, *, kind, column_mapping=None, **ctx):
    """日结工合体（'名字'+'中介|派遣'）"""
    return parse_rijiegong(ws)


def liying(ws, *, kind, column_mapping=None, **ctx):
    """丽盈/简洁考勤兜底（旧 _parse 默认分支）"""
    return parse_liying(ws)
