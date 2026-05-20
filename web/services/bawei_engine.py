"""八维数据分析引擎 — 准入门槛 + 量化评分"""
import os
import openpyxl
from datetime import date
from config import BASE_DATA_DIR
from services.precondition_rules import (
    load_controller_info, load_project_info_full,
    load_enterprise_credit, load_personal_credit, parse_date,
)
from services.payment_calculator import load_attendance_data, load_payroll_data


# ============ 第一层：准入门槛（12条硬关卡） ============

GATE_CHECKS = [
    {"id": 1, "name": "法人/实控人非失信被执行人", "source": "enterprise_credit"},
    {"id": 2, "name": "征信无90天以上逾期记录", "source": "personal_credit"},
    {"id": 3, "name": "征信无当前大额逾期", "source": "personal_credit"},
    {"id": 4, "name": "消费贷同时在借不超过8笔", "source": "personal_credit"},
    {"id": 5, "name": "与甲方合作满3个月以上", "source": "project_info"},
    {"id": 6, "name": "月均在岗人数≥50人", "source": "attendance"},
    {"id": 7, "name": "企业工商状态正常", "source": "manual"},
    {"id": 8, "name": "企业当天不是税务非正常户", "source": "manual"},
    {"id": 9, "name": "企业无当前欠税公告", "source": "manual"},
    {"id": 10, "name": "无涉及刑事案件或重大诉讼", "source": "manual"},
    {"id": 11, "name": "未提供虚假资料", "source": "manual"},
    {"id": 12, "name": "有行业不诚信讯息", "source": "manual"},
]


def _check_overdue_90(personal_loans):
    """检查是否有90天以上逾期"""
    # 从征信数据中检查逾期记录，目前简化处理
    # 实际需要读取征信报告中的逾期天数字段
    return None  # None = 无数据


def _check_current_overdue(personal_loans):
    """检查是否有当前大额逾期"""
    return None


def _count_consumer_loans(personal_loans):
    """统计消费贷在借笔数"""
    if not personal_loans:
        return None
    # 简化：返回关联负债数量作为参考
    return len(personal_loans)


def run_gate_checks(company, project, year, month, controllers, all_companies, proj_info):
    """执行12条准入门槛检查"""
    checks = []
    blocked = []

    # 加载数据
    att_data = load_attendance_data(company, project, year, month)
    _, ent_loans = load_enterprise_credit(company)
    _, per_loans = load_personal_credit(controllers)

    # 合作开始日期
    start_date = parse_date(proj_info.get("合作开始日期") or proj_info.get("甲方合同开始日期"))

    for gate in GATE_CHECKS:
        gid = gate["id"]
        name = gate["name"]
        source = gate["source"]

        if source == "manual":
            # 需人工填写的项，标记为无数据
            checks.append({"id": gid, "name": name, "value": None, "result": "无数据", "auto": False})
            continue

        if gid == 1:
            # 失信被执行人 — 需要企查查数据，暂无自动化
            checks.append({"id": gid, "name": name, "value": None, "result": "无数据", "auto": False})

        elif gid == 2:
            val = _check_overdue_90(per_loans)
            if val is None:
                checks.append({"id": gid, "name": name, "value": None, "result": "无数据", "auto": False})
            elif val:
                checks.append({"id": gid, "name": name, "value": "有逾期", "result": "否决"})
                blocked.append(name)
            else:
                checks.append({"id": gid, "name": name, "value": "无逾期", "result": "通过"})

        elif gid == 3:
            val = _check_current_overdue(per_loans)
            if val is None:
                checks.append({"id": gid, "name": name, "value": None, "result": "无数据", "auto": False})
            elif val:
                checks.append({"id": gid, "name": name, "value": "有逾期", "result": "否决"})
                blocked.append(name)
            else:
                checks.append({"id": gid, "name": name, "value": "无逾期", "result": "通过"})

        elif gid == 4:
            count = _count_consumer_loans(per_loans)
            if count is None:
                checks.append({"id": gid, "name": name, "value": None, "result": "无数据", "auto": False})
            elif count > 8:
                checks.append({"id": gid, "name": name, "value": f"{count}笔", "result": "否决"})
                blocked.append(f"{name}（{count}笔）")
            else:
                checks.append({"id": gid, "name": name, "value": f"{count}笔", "result": "通过"})

        elif gid == 5:
            if start_date:
                months_diff = (date(year, month, 1).year - start_date.year) * 12 + (date(year, month, 1).month - start_date.month)
                if months_diff >= 3:
                    checks.append({"id": gid, "name": name, "value": f"{months_diff}个月", "result": "通过"})
                else:
                    checks.append({"id": gid, "name": name, "value": f"{months_diff}个月", "result": "否决"})
                    blocked.append(f"{name}（仅{months_diff}个月）")
            else:
                checks.append({"id": gid, "name": name, "value": None, "result": "无数据", "auto": False})

        elif gid == 6:
            person_count = att_data.get("人数", 0)
            if person_count > 0:
                if person_count >= 50:
                    checks.append({"id": gid, "name": name, "value": f"{person_count}人", "result": "通过"})
                else:
                    checks.append({"id": gid, "name": name, "value": f"{person_count}人", "result": "否决"})
                    blocked.append(f"{name}（仅{person_count}人）")
            else:
                checks.append({"id": gid, "name": name, "value": None, "result": "无数据", "auto": False})

    gate_pass = len(blocked) == 0
    return checks, gate_pass, blocked


# ============ 第二层：六维量化评分 ============

# 评分标准定义
DIMENSIONS = [
    {
        "key": "A", "name": "考勤稳定性", "weight": 0.20,
        "indicators": [
            {"key": "A1", "name": "在岗人数波动率", "sub_weight": 0.4,
             "bands": [("< 10%", 100), ("10-20%", 70), ("20-30%", 40), ("> 30%", 10)]},
            {"key": "A2", "name": "出勤率", "sub_weight": 0.3,
             "bands": [("≥ 95%", 100), ("90-94%", 70), ("85-89%", 40), ("< 85%", 10)]},
            {"key": "A3", "name": "流失率", "sub_weight": 0.3,
             "bands": [("< 10%", 100), ("10-20%", 70), ("20-30%", 40), ("> 30%", 10)]},
        ]
    },
    {
        "key": "B", "name": "财务健康度", "weight": 0.25,
        "indicators": [
            {"key": "B1", "name": "银行流水匹配度", "sub_weight": 0.25,
             "bands": [("偏差< 5%", 100), ("5-15%", 70), ("15-30%", 40), ("> 30%", 10)]},
            {"key": "B2", "name": "负债率", "sub_weight": 0.25,
             "bands": [("< 30%", 100), ("30-50%", 70), ("50-70%", 40), ("> 70%", 10)]},
            {"key": "B3", "name": "消费贷笔数", "sub_weight": 0.25,
             "bands": [("≤ 2笔", 100), ("3-5笔", 50), ("6-7笔", 20), ("≥ 8笔", 0)]},
            {"key": "B4", "name": "信用卡使用率", "sub_weight": 0.25,
             "bands": [("< 50%", 100), ("50-70%", 70), ("70-90%", 30), ("> 90%", 10)]},
        ]
    },
    {
        "key": "C", "name": "合作深度", "weight": 0.15,
        "indicators": [
            {"key": "C1", "name": "与甲方合作时长", "sub_weight": 0.33,
             "bands": [("≥ 2年", 100), ("1-2年", 70), ("6月-1年", 40), ("3-6月", 20)]},
            {"key": "C2", "name": "甲方回款占比", "sub_weight": 0.33,
             "bands": [("40-70%", 100), ("70-80%", 70), ("30-40%", 50), ("> 80%", 30), ("< 30%", 20)]},
            {"key": "C3", "name": "甲方评价/续约情况", "sub_weight": 0.34,
             "bands": [("核心供应商", 100), ("稳定", 70), ("普通", 40), ("即将替换", 0)]},
        ]
    },
    {
        "key": "D", "name": "征信质量", "weight": 0.15,
        "indicators": [
            {"key": "D1", "name": "逾期次数(近5年)", "sub_weight": 0.33,
             "bands": [("0次", 100), ("1次", 70), ("2次", 40), ("≥ 3次", 10)]},
            {"key": "D2", "name": "贷款审批查询频率", "sub_weight": 0.33,
             "bands": [("≤ 3次", 100), ("4-6次", 60), ("7-10次", 30), ("> 10次", 10)]},
            {"key": "D3", "name": "对外担保情况", "sub_weight": 0.34,
             "bands": [("无", 100), ("有小额", 60), ("有大额", 20)]},
        ]
    },
    {
        "key": "E", "name": "经营规范度", "weight": 0.15,
        "indicators": [
            {"key": "E1", "name": "开票连续性", "sub_weight": 0.25,
             "bands": [("连续12月", 100), ("1-2月中断", 70), ("多次中断", 30)]},
            {"key": "E2", "name": "发票合规性", "sub_weight": 0.25,
             "bands": [("全部合规", 100), ("基本合规", 70), ("有问题", 20)]},
            {"key": "E3", "name": "税务正常度", "sub_weight": 0.25,
             "bands": [("正常", 100), ("欠缴已补", 60), ("当前欠缴", 10)]},
            {"key": "E4", "name": "保险参保率", "sub_weight": 0.25,
             "bands": [("100%", 100), ("≥ 90%", 70), ("70-89%", 40), ("< 70%", 10)]},
        ]
    },
    {
        "key": "F", "name": "还款配合度", "weight": 0.10,
        "indicators": [
            {"key": "F1", "name": "还款准时率", "sub_weight": 0.33,
             "bands": [("100%", 100), ("1次延迟", 60), ("2次+", 20)]},
            {"key": "F2", "name": "资料配合速度", "sub_weight": 0.33,
             "bands": [("当天", 100), ("3天内", 70), ("反复催", 30), ("拒绝", 0)]},
            {"key": "F3", "name": "面谈一致性", "sub_weight": 0.34,
             "bands": [("一致", 100), ("基本一致", 70), ("有矛盾", 30), ("撒谎", 0)]},
        ]
    },
]

# 风险等级划分
RISK_LEVELS = [
    (85, "低风险", 0.80, "bg-success"),
    (70, "中风险", 0.60, "bg-warning"),
    (55, "高风险", 0.40, "bg-danger"),
    (0,  "拒绝",   0.00, "bg-dark"),
]


def _auto_score_attendance(company, project, year, month):
    """自动计算考勤稳定性相关指标"""
    att = load_attendance_data(company, project, year, month)
    person_count = att.get("人数", 0)
    scores = {}

    # A1: 在岗人数波动率 — 需要多月数据，单月无法计算
    scores["A1"] = None

    # A2: 出勤率 — 需要应出勤数据，暂无
    scores["A2"] = None

    # A3: 流失率 — 需要上月数据对比，暂无
    scores["A3"] = None

    return scores


def _auto_score_credit(controllers):
    """自动计算征信质量相关指标"""
    _, per_loans = load_personal_credit(controllers)
    scores = {}

    # D1: 逾期次数 — 需要读取征信报告逾期字段
    scores["D1"] = None

    # D2: 查询频率 — 需要读取征信报告查询记录
    scores["D2"] = None

    # D3: 对外担保 — 可从关联负债中部分判断
    if per_loans is not None:
        has_guarantee = any(l.get("类型") == "担保" for l in per_loans)
        if not has_guarantee:
            scores["D3"] = {"value": "无", "score": 100}
    else:
        scores["D3"] = None

    return scores


def _auto_score_cooperation(proj_info, year, month):
    """自动计算合作深度指标"""
    scores = {}

    start_date = parse_date(proj_info.get("合作开始日期") or proj_info.get("甲方合同开始日期"))
    if start_date:
        months = (date(year, month, 1).year - start_date.year) * 12 + (date(year, month, 1).month - start_date.month)
        if months >= 24:
            scores["C1"] = {"value": f"{months}个月", "score": 100}
        elif months >= 12:
            scores["C1"] = {"value": f"{months}个月", "score": 70}
        elif months >= 6:
            scores["C1"] = {"value": f"{months}个月", "score": 40}
        elif months >= 3:
            scores["C1"] = {"value": f"{months}个月", "score": 20}
        else:
            scores["C1"] = {"value": f"{months}个月", "score": 0}
    else:
        scores["C1"] = None

    scores["C2"] = None
    scores["C3"] = None
    return scores


def run_scoring(company, project, year, month, controllers, proj_info):
    """执行六维量化评分，返回各维度得分"""
    # 自动计算可用的指标
    att_scores = _auto_score_attendance(company, project, year, month)
    credit_scores = _auto_score_credit(controllers)
    coop_scores = _auto_score_cooperation(proj_info, year, month)

    auto_scores = {}
    auto_scores.update(att_scores)
    auto_scores.update(credit_scores)
    auto_scores.update(coop_scores)

    # 组装维度结果
    dimensions = []
    total_score = 0
    total_weight_with_data = 0

    for dim in DIMENSIONS:
        indicators = []
        dim_score = 0
        dim_has_data = False

        for ind in dim["indicators"]:
            auto = auto_scores.get(ind["key"])
            if auto and isinstance(auto, dict):
                indicators.append({
                    "key": ind["key"],
                    "name": ind["name"],
                    "sub_weight": ind["sub_weight"],
                    "bands": ind["bands"],
                    "value": auto["value"],
                    "score": auto["score"],
                    "auto": True,
                })
                dim_score += auto["score"] * ind["sub_weight"]
                dim_has_data = True
            else:
                indicators.append({
                    "key": ind["key"],
                    "name": ind["name"],
                    "sub_weight": ind["sub_weight"],
                    "bands": ind["bands"],
                    "value": None,
                    "score": None,
                    "auto": False,
                })

        dim_result = {
            "key": dim["key"],
            "name": dim["name"],
            "weight": dim["weight"],
            "indicators": indicators,
            "score": round(dim_score, 1) if dim_has_data else None,
            "has_data": dim_has_data,
            "data_count": sum(1 for i in indicators if i["score"] is not None),
            "total_count": len(indicators),
        }
        dimensions.append(dim_result)

        if dim_has_data:
            total_score += dim_score * dim["weight"]
            total_weight_with_data += dim["weight"]

    # 如果有数据的维度不足，按比例折算
    if total_weight_with_data > 0:
        final_score = round(total_score / total_weight_with_data, 1)
    else:
        final_score = None

    # 风险等级
    risk_level = None
    risk_color = "bg-secondary"
    finance_ratio = None
    if final_score is not None:
        for threshold, level, ratio, color in RISK_LEVELS:
            if final_score >= threshold:
                risk_level = level
                risk_color = color
                finance_ratio = ratio
                break

    return {
        "dimensions": dimensions,
        "final_score": final_score,
        "risk_level": risk_level,
        "risk_color": risk_color,
        "finance_ratio": finance_ratio,
    }


# ============ 主入口 ============

def run_bawei_analysis(company, project, year, month, today):
    """执行八维数据分析，返回完整结果"""
    # 加载基础数据
    controllers, all_companies, credit_limit, temp_occupy = load_controller_info(company)
    proj_info = load_project_info_full(company, project)
    proj_info['_project'] = project

    if not controllers:
        return {
            "engine": "八维分析",
            "gate_pass": False,
            "gate_checks": [],
            "gate_blocked": [f"未找到{company}的实控人信息"],
            "scoring": None,
        }

    # 第一层：准入门槛
    gate_checks, gate_pass, gate_blocked = run_gate_checks(
        company, project, year, month, controllers, all_companies, proj_info
    )

    # 第二层：量化评分（即使准入不通过也计算，供参考）
    scoring = run_scoring(company, project, year, month, controllers, proj_info)

    return {
        "engine": "八维分析",
        "gate_pass": gate_pass,
        "gate_checks": gate_checks,
        "gate_blocked": gate_blocked,
        "scoring": scoring,
    }
