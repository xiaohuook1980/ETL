"""数据识别层：文件级（魔术字节）+ sheet 级（表头关键词）

识别完全按内容，不依赖文件名。
"""


# ============================================================
# 1. 文件级识别（魔术字节）
# ============================================================
def detect_mime(b):
    """根据文件前几字节识别类型"""
    if b[:2] == b'PK':                           return 'xlsx_or_zip'
    if b[:8] == b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1': return 'xls'  # OLE2 (旧 Excel/Word)
    if b[:4] == b'Rar!':                         return 'rar'
    if b[:4] == b'%PDF':                         return 'pdf'
    if b[:8] == b'\x89PNG\r\n\x1a\n':            return 'png'
    if b[:3] == b'\xff\xd8\xff':                 return 'jpeg'
    return 'unknown'


# ============================================================
# 2. sheet 级识别（按表头关键词）
# ============================================================
def collect_header_text(ws, max_rows=4):
    """前 max_rows 行所有非空单元格连接成串。
    单元格内的换行/Tab/连续空白统一压成单个空格,避免 '序\\n号' 这种破坏关键词匹配。
    """
    import re as _re
    parts = []
    for row in ws.iter_rows(max_row=max_rows, values_only=True):
        for v in row:
            if v is not None:
                s = _re.sub(r'\s+', '', str(v).strip())
                parts.append(s)
    return '|'.join(parts)


def classify_sheet(ws):
    """sheet kind ∈ {wage_sheet, attendance, bill, payroll, empty, unknown}

    特征匹配规则（按业务字段区分核心 4 类）：

      wage_sheet  — 劳务工资表（劳务文员录入，给工人发的工资清单）
                    特征字段：'应发工资' + 劳务发薪规则字段（打卡价/保险/个税/水电）
                    或老希锐格式：'工号' + '部门' + ('总小时'|'上班天数')

      bill        — 甲方账单（甲方给劳务的费用结算单，含管理费）
                    特征字段：'实发工资' / '总工时工资' + 甲方计费规则字段（夜班补贴/奖励补贴/罚款）
                    或老格式：'部门' + '金额' + ('综合单价'|'账单金额'|'部门小计')

      attendance  — 考勤明细（甲方系统/小鱼系统导出工人打卡记录）
                    新格式（南斗星离职/在职）：'打卡日期' + ('上班小时'|'打卡次数'|'打卡时间')
                    旧格式：'班次' + '工时' + '姓名' + ('楼层'|'班组')

      payroll     — 发薪流水 xlsx（银行/支付通道导出转账记录）
                    特征：('付款金额'|'实发金额'|'转账金额') + ('付款时间'|'转账时间')
                          + ('姓名'|'户名'|'收款人')

    优先级：wage_sheet → bill → attendance → payroll → unknown
    （应发工资和实发工资是 wage_sheet/bill 的强区分特征，先识别）
    """
    head = collect_header_text(ws)
    if not head:
        # 前 4 行全空：表头可能在更深位置（如银行代发代扣 R6 起表头）
        head = collect_header_text(ws, max_rows=10)
        if not head:
            return 'empty'

    has = lambda *kw: all(k in head for k in kw)
    has_any = lambda *kw: any(k in head for k in kw)

    # ===== wage_sheet =====
    # 老希锐：工号+部门+月统计列
    if has('工号', '部门') and has_any('总小时', '上班天数'):
        return 'wage_sheet'
    # 新格式：应发工资 + 劳务发薪规则
    if has('应发工资', '姓名') and has_any('打卡价', '保险', '个税', '水电', '餐补'):
        return 'wage_sheet'
    # 长期工格式（梦寺达-长隆）：应发工资 + 实发工资 + (住宿|扣费|预支)
    if has('应发工资', '姓名', '实发工资') and has_any('住宿', '扣费', '预支'):
        return 'wage_sheet'

    # ===== bill =====
    # 老格式：部门+金额+账单关键词
    if (has('部门', '金额')
            and has_any('综合单价', '工时×单价', '账单金额', '部门小计', '汇总')):
        return 'bill'
    # 新格式（南斗星汇总）：部门+姓名+实发工资 + 甲方计费规则
    if (has('部门', '姓名')
            and has_any('实发工资', '总工时工资')
            and has_any('夜班补贴', '奖励/补贴', '奖励', '罚款')):
        return 'bill'

    # ===== attendance =====
    # 老格式：班次+工时+姓名+场地
    if (has('班次') and has_any('工时', '生产小时')
            and has_any('姓名', '名字')
            and has_any('楼层', '班组')):
        return 'attendance'
    # 简洁考勤（澳思美等）：序号+日期+班次+姓名+工时（无楼层）
    if has('序号', '日期', '班次', '姓名', '工时'):
        return 'attendance'
    # 顺丰结算数据：报名ID + 姓名 + 日期 + 班次 + 结算时长
    if has('姓名', '日期', '班次', '结算时长') and has_any('报名ID', '劳务机构'):
        return 'attendance'
    # 顺丰出勤数据：报名ID + 姓名 + 日期 + 上班打卡时间（无 hours 列，由打卡时间差计算）
    if has('姓名', '日期', '上班打卡时间') and has_any('报名ID', '工号'):
        return 'attendance'
    # 康丽达月度横向（R3 序号+姓名+日期 / R4 'X日'）
    if has('考勤', '序号', '姓名') and ('1日' in head or '2日' in head):
        return 'attendance'
    # 新广益月度横向（R2 含"YYYY年（M.D-M.D）...统计表" / R3 序号+姓名+所在部门+班别 / R4 数字日期跨月）
    if has('序号', '姓名', '所在部门', '班别') and '统计表' in head:
        return 'attendance'
    # 长期工月度横向（澳思美）：'长期工考勤' + '总工时'
    if has('长期工考勤', '总工时'):
        return 'attendance'
    # 长期工月度横向退化版（无标题/无序号列）：'姓名' + '总工时' + 不是简洁考勤
    if has('姓名', '总工时') and not has('日期', '班次'):
        return 'attendance'
    # 南斗星格式：打卡日期+姓名+打卡相关
    if (has('打卡日期', '姓名')
            and has_any('上班小时', '工时', '生产小时', '打卡次数', '打卡时间')):
        return 'attendance'
    # 万汇/菜鸟格式：出勤日期 + 上班时间 + 工号 + 姓名 (含审核状态/劳务公司列)
    if (has('出勤日期', '姓名')
            and has_any('上班时间', '上班小时', '班次ID', '工时')):
        return 'attendance'
    # 梦寺达酒店月度横向模板：标题"...考勤表" + R3 "部门：" + R4 "序号 [工号|员工编码|兼职卡号] 姓名 1 2 3..."
    if (has('考勤表', '部门', '姓名', '序号')
            and has_any('工号', '员工编码', '兼职卡号', '员工号')):
        return 'attendance'
    # 月度考勤核对表（恒众源/中集等）：劳务公司+工号+姓名+出勤天数 + 日级符号化(√/半)
    if has('劳务公司', '工号', '姓名', '出勤天数'):
        return 'attendance'

    # ===== payroll xlsx =====
    if (has_any('付款金额', '实发金额', '转账金额', '到账金额', '发放金额')
            and has_any('付款时间', '转账时间', '交易时间', '到账时间', '发放时间')
            and has_any('姓名', '户名', '收款人')):
        return 'payroll'
    # 银行代发代扣业务明细格式（建行/农行等）：户名+金额+经办日+账号
    if (has('户名', '金额', '账号')
            and has_any('经办日', '经办时间', '期望日')):
        return 'payroll'
    # 顺丰商户转账批次明细（R1 标题 + R6 表头分离）：扩展扫前 10 行
    head_ext = collect_header_text(ws, max_rows=10)
    if ('商户转账批次明细' in head_ext or '转账批次' in head_ext) \
            and '转账金额' in head_ext and '收款人' in head_ext:
        return 'payroll'

    return 'unknown'
