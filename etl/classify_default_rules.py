"""项目级 sheet 分类规则种子模板。

新项目创建时把这份字典 seed 到 project_classify_rules（每项目一份独立副本）。
内容是 etl/classify.py 30+ 条 if 的拆条翻译，结构对齐 project_classify_rules schema。

字段语义见 etl/sql/migrations/20260509_classify_rules.sql。

handler 命名约定（实际类在 etl/parsers/handlers/<name>.py，step 4/6 落地）：
  standard                       —— 走 column_mapping 解析（横平竖直的明细表）
  monthly_horizontal_kld         —— 康丽达月度横向（R3 序号+姓名+日期 / R4 'X日'）
  monthly_horizontal_xgy         —— 新广益月度横向（含跨月业务周期标题）
  monthly_horizontal_changqi     —— 澳思美等长期工月度横向（'总工时' 列汇总）
  hotel_monthly_horizontal       —— 梦寺达酒店月度模板（R3 部门 / R4 序号+工号+姓名+1..31）
  duty_symbolic                  —— 符号化日级考勤（√/半 等，恒众源/中集）
  shunfeng_clock_pair            —— 顺丰出勤数据（无 hours 列，由打卡时间差算）

priority：同 kind 内尝试顺序（小→大），跨 kind 由引擎按 wage_sheet>bill>attendance>payroll 顺序遍历。

【拆条原则】schema 仅支持 1 组 OR (match_columns_any)。原 classify.py 中含 2/3 组 OR 的规则
按笛卡尔积拆成多条 AND-only 规则；payroll 那条 5×5×3=75 太夸张，只列常见渠道组合 + pending 兜底。
"""

from itertools import product


def _expand(base, *or_groups, priority_start):
    """笛卡尔积展开多组 OR 为多条 AND 规则。

    base: 公共字段（含 target_kind / handler / note 等）
    or_groups: 多组 OR，每组是同义词列表
    priority_start: 第一条 priority，依次 +1
    """
    out = []
    for i, combo in enumerate(product(*or_groups)):
        rule = dict(base)
        rule['match_columns'] = list(base.get('match_columns', [])) + list(combo)
        rule['priority'] = priority_start + i
        rule['note'] = f"{base.get('note','')} [组合: {'+'.join(combo)}]"
        out.append(rule)
    return out


_RULES = []

# ============================================================
# wage_sheet（劳务工资表）
# ============================================================
_RULES.append({
    'target_kind': 'wage_sheet', 'priority': 10,
    'match_columns': ['工号', '部门'],
    'match_columns_any': ['总小时', '上班天数'],
    'match_excludes': None, 'scan_rows': 4,
    'handler': 'standard', 'column_mapping': None,
    'note': '老希锐工资表',
})
_RULES.append({
    'target_kind': 'wage_sheet', 'priority': 20,
    'match_columns': ['应发工资', '姓名'],
    'match_columns_any': ['打卡价', '保险', '个税', '水电', '餐补'],
    'match_excludes': None, 'scan_rows': 4,
    'handler': 'standard', 'column_mapping': None,
    'note': '新格式工资表（应发工资+劳务发薪规则）',
})
_RULES.append({
    'target_kind': 'wage_sheet', 'priority': 30,
    'match_columns': ['应发工资', '姓名', '实发工资'],
    'match_columns_any': ['住宿', '扣费', '预支'],
    'match_excludes': None, 'scan_rows': 4,
    'handler': 'standard', 'column_mapping': None,
    'note': '长期工工资表（梦寺达-长隆）',
})

# ============================================================
# bill（甲方账单）
# ============================================================
_RULES.append({
    'target_kind': 'bill', 'priority': 5,
    'match_columns': ['用工部门', '实发服务费'],
    'match_columns_any': None, 'match_excludes': None, 'scan_rows': 4,
    'handler': 'bill_project_summary', 'column_mapping': None,
    'note': '梦寺达酒店项目综合表（无人员名单，仅 bill_totals）',
})
_RULES.append({
    'target_kind': 'bill', 'priority': 10,
    'match_columns': ['部门', '金额'],
    'match_columns_any': ['综合单价', '工时×单价', '账单金额', '部门小计', '汇总'],
    'match_excludes': None, 'scan_rows': 4,
    'handler': 'standard', 'column_mapping': None,
    'note': '老格式账单（部门+金额+账单关键词）',
})

# bill p20 新格式账单（南斗星汇总）原结构：
#   has(部门,姓名) AND has_any(实发工资,总工时工资) AND has_any(夜班补贴,奖励/补贴,奖励,罚款)
# 拆 2×4=8 条
_RULES += _expand(
    {'target_kind': 'bill', 'match_columns': ['部门', '姓名'],
     'match_columns_any': None, 'match_excludes': None, 'scan_rows': 4,
     'handler': 'standard', 'column_mapping': None,
     'note': '新格式账单（南斗星汇总）'},
    ['实发工资', '总工时工资'],
    ['夜班补贴', '奖励/补贴', '奖励', '罚款'],
    priority_start=20,
)

# ============================================================
# attendance（考勤明细）
# ============================================================

# attendance p10 老格式考勤原结构：
#   has(班次) AND has_any(工时,生产小时) AND has_any(姓名,名字) AND has_any(楼层,班组)
# 拆 2×2×2=8 条
_RULES += _expand(
    {'target_kind': 'attendance', 'match_columns': ['班次'],
     'match_columns_any': None, 'match_excludes': None, 'scan_rows': 4,
     'handler': 'standard', 'column_mapping': None,
     'note': '老格式考勤'},
    ['工时', '生产小时'],
    ['姓名', '名字'],
    ['楼层', '班组'],
    priority_start=10,
)

# 简洁考勤（澳思美）—— priority 紧跟 p10 系列后面
_RULES.append({
    'target_kind': 'attendance', 'priority': 30,
    'match_columns': ['序号', '日期', '班次', '姓名', '工时'],
    'match_columns_any': None, 'match_excludes': None, 'scan_rows': 4,
    'handler': 'standard', 'column_mapping': None,
    'note': '简洁考勤（澳思美等：序号+日期+班次+姓名+工时）',
})
_RULES.append({
    'target_kind': 'attendance', 'priority': 40,
    'match_columns': ['姓名', '日期', '班次', '结算时长'],
    'match_columns_any': ['报名ID', '劳务机构'],
    'match_excludes': None, 'scan_rows': 4,
    'handler': 'standard', 'column_mapping': None,
    'note': '顺丰结算数据',
})
_RULES.append({
    'target_kind': 'attendance', 'priority': 50,
    'match_columns': ['姓名', '日期', '上班打卡时间'],
    'match_columns_any': ['报名ID', '工号'],
    'match_excludes': None, 'scan_rows': 4,
    'handler': 'shunfeng_clock_pair', 'column_mapping': None,
    'note': '顺丰出勤数据（无 hours 列，由上下班打卡时间差算）',
})
_RULES.append({
    'target_kind': 'attendance', 'priority': 60,
    'match_columns': ['考勤', '序号', '姓名'],
    'match_columns_any': ['1日', '2日'],
    'match_excludes': None, 'scan_rows': 4,
    'handler': 'monthly_horizontal_kld', 'column_mapping': None,
    'note': '康丽达月度横向（R3 序号+姓名+日期 / R4 X日）',
})
_RULES.append({
    'target_kind': 'attendance', 'priority': 70,
    'match_columns': ['序号', '姓名', '所在部门', '班别', '统计表'],
    'match_columns_any': None, 'match_excludes': None, 'scan_rows': 4,
    'handler': 'monthly_horizontal_xgy', 'column_mapping': None,
    'note': '新广益月度横向',
})
_RULES.append({
    'target_kind': 'attendance', 'priority': 80,
    'match_columns': ['长期工考勤', '总工时'],
    'match_columns_any': None, 'match_excludes': None, 'scan_rows': 4,
    'handler': 'monthly_horizontal_changqi', 'column_mapping': None,
    'note': '长期工月度横向（澳思美：标题"长期工考勤"+总工时列）',
})
_RULES.append({
    'target_kind': 'attendance', 'priority': 90,
    'match_columns': ['姓名', '总工时'],
    'match_columns_any': None,
    'match_excludes': None,                     # 原 ['日期','班次'] 是 AND-NOT，schema 表达不下；靠 priority 让简洁考勤(p30)先抢
    'scan_rows': 4,
    'handler': 'monthly_horizontal_changqi', 'column_mapping': None,
    'note': '长期工月度横向退化版（无标题/无序号列）；靠简洁考勤 p30 优先短路避免误中',
})
_RULES.append({
    'target_kind': 'attendance', 'priority': 100,
    'match_columns': ['打卡日期', '姓名'],
    'match_columns_any': ['上班小时', '工时', '生产小时', '打卡次数', '打卡时间'],
    'match_excludes': None, 'scan_rows': 4,
    'handler': 'standard', 'column_mapping': None,
    'note': '南斗星格式（打卡日期+姓名+打卡相关）',
})
_RULES.append({
    'target_kind': 'attendance', 'priority': 110,
    'match_columns': ['出勤日期', '姓名'],
    'match_columns_any': ['上班时间', '上班小时', '班次ID', '工时'],
    'match_excludes': None, 'scan_rows': 4,
    'handler': 'standard', 'column_mapping': None,
    'note': '万汇/菜鸟格式（含审核状态/劳务公司列）',
})
_RULES.append({
    'target_kind': 'attendance', 'priority': 120,
    'match_columns': ['考勤表', '部门', '姓名', '序号'],
    'match_columns_any': ['工号', '员工编码', '兼职卡号', '员工号'],
    'match_excludes': None, 'scan_rows': 4,
    'handler': 'hotel_monthly_horizontal', 'column_mapping': None,
    'note': '梦寺达酒店月度横向模板',
})
_RULES.append({
    'target_kind': 'attendance', 'priority': 130,
    'match_columns': ['劳务公司', '工号', '姓名', '出勤天数'],
    'match_columns_any': None, 'match_excludes': None, 'scan_rows': 4,
    'handler': 'duty_symbolic', 'column_mapping': None,
    'note': '月度考勤核对表（恒众源/中集，日级符号化 √/半）',
})

# ============================================================
# payroll（发薪流水 xlsx）
# ============================================================

# payroll p10 标准流水原结构：
#   has_any(付款金额|实发金额|转账金额|到账金额|发放金额)
#   AND has_any(付款时间|转账时间|交易时间|到账时间|发放时间)
#   AND has_any(姓名|户名|收款人)
# 全笛卡尔积 = 5×5×3=75 条；为与旧 classify.py 完全等价（避免遗漏渠道组合），全展开
_RULES += _expand(
    {'target_kind': 'payroll',
     'match_columns_any': None, 'match_excludes': None, 'scan_rows': 4,
     'handler': 'standard', 'column_mapping': None,
     'note': '标准发薪流水'},
    ['姓名', '户名', '收款人'],
    ['付款金额', '实发金额', '转账金额', '到账金额', '发放金额'],
    ['付款时间', '转账时间', '交易时间', '到账时间', '发放时间'],
    priority_start=10,
)

_RULES.append({
    'target_kind': 'payroll', 'priority': 200,
    'match_columns': ['户名', '金额', '账号'],
    'match_columns_any': ['经办日', '经办时间', '期望日'],
    'match_excludes': None, 'scan_rows': 4,
    'handler': 'standard', 'column_mapping': None,
    'note': '银行代发代扣业务明细（建行/农行）',
})
_RULES.append({
    'target_kind': 'payroll', 'priority': 210,
    'match_columns': ['转账金额', '收款人'],
    'match_columns_any': ['商户转账批次明细', '转账批次'],
    'match_excludes': None, 'scan_rows': 10,
    'handler': 'standard', 'column_mapping': None,
    'note': '顺丰商户转账批次明细（R1 标题 + R6 表头分离，要扫前 10 行）',
})


# ============================================================
# standard handler 共享 column_mapping 模板（按 kind）
# 所有 handler='standard' 且 column_mapping=None 的规则自动应用本模板。
# 用户在 UI 上可以单条规则覆盖。
# ============================================================
_STANDARD_MAPPINGS = {
    'attendance': {
        'shift_date':    '日期, 打卡日期, 出勤日期',
        'name_raw':      '姓名, 名字',
        'hours':         '生产小时, 工时, 上班小时, 结算时长, 上班时间',
        'quantity':      '件数, 数量',
        'shift_name':    '班次',
        'floor_or_group': '楼层, 班组, 部门',
        'worker_type':   '工种, 岗位',
        'id_card_raw':   '身份证, 证件号码',
        'extra_data':    '序号, 工号, 报名ID, 中介, 派遣单位, 备注',
    },
    'bill': {
        'name_raw':    '姓名',
        'amount':      '实发工资, 总工时工资, 账单金额, 应发, 金额, 综合金额',
        'id_card_raw': '身份证',
        'extra_data':  '部门, 工时, 单价, 综合单价, 夜班补贴, 奖励, 奖励/补贴, 罚款',
    },
    'wage_sheet': {
        'name_raw':       '姓名',
        'payable_amount': '税前实发工资, 税前实发, 税后实发工资, 税后实发, 税后实际工资, 税后实际, 实发工资, 税前工资, 应发工资, 应发, 合计应付工资',
        'extra_data':     '工号, 部门, 实发工资, 打卡价, 保险, 个税, 水电, 餐补, 总小时, 上班天数',
    },
    'payroll': {
        'name_raw':    '姓名, 户名, 收款人',
        'work_amount': '实际到账金额, 到账金额, 实发金额, 转账金额, 发放金额, 金额, 付款金额',
        'pay_time':    '付款时间, 转账时间, 交易时间, 发放时间, 到账时间, 经办日, 经办时间',
        'id_card_raw': '身份证, 证件号码',
        'extra_data':  '备注, 摘要, 班次名称, 批次名称, 账号, 银行, 工号',
    },
}

# 末尾扫一遍：standard handler 且 mapping 为 None → 注入 kind 模板
for _r in _RULES:
    if _r.get('handler') == 'standard' and _r.get('column_mapping') is None:
        _r['column_mapping'] = _STANDARD_MAPPINGS.get(_r['target_kind'])


DEFAULT_RULES = _RULES


def get_default_rules_for_kind(target_kind):
    """取某 kind 的所有默认规则（按 priority 升序）。"""
    rules = [r for r in DEFAULT_RULES if r['target_kind'] == target_kind]
    return sorted(rules, key=lambda r: r['priority'])


def count_by_kind():
    """统计各 kind 规则数（自检/调试用）。"""
    from collections import Counter
    return dict(Counter(r['target_kind'] for r in DEFAULT_RULES))


if __name__ == '__main__':
    print('DEFAULT_RULES 总条数：', len(DEFAULT_RULES))
    print('分布：', count_by_kind())
    for kind in ('wage_sheet', 'bill', 'attendance', 'payroll'):
        print(f'\n--- {kind} ---')
        for r in get_default_rules_for_kind(kind):
            mc = '+'.join(r['match_columns']) if r['match_columns'] else '-'
            mca = '|'.join(r['match_columns_any']) if r['match_columns_any'] else '-'
            print(f"  p{r['priority']:>3} [{r['handler']}] mc={mc} any={mca}  // {r['note']}")
