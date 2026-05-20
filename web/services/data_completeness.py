"""数据补全检查：扫描原始数据目录，返回三个维度的数据完整性"""
import os
import openpyxl
from config import RAW_DATA_DIR, BASE_DATA_DIR


def _dir_has_files(path):
    """目录存在且包含文件"""
    if not os.path.isdir(path):
        return False
    for f in os.listdir(path):
        if os.path.isfile(os.path.join(path, f)):
            return True
    return False


def _count_files(path):
    """统计目录下文件数量"""
    if not os.path.isdir(path):
        return 0
    return len([f for f in os.listdir(path) if os.path.isfile(os.path.join(path, f))])


def _list_subdirs(path):
    """列出子目录（排除模版、隐藏目录）"""
    if not os.path.isdir(path):
        return []
    return sorted([d for d in os.listdir(path)
                   if os.path.isdir(os.path.join(path, d))
                   and not d.startswith(('_', '.'))
                   and '模版' not in d and '模板' not in d
                   and '担保人' not in d])


def _load_controller_company_map():
    """从实控人关系.xlsx读取实控人→公司映射"""
    filepath = os.path.join(BASE_DATA_DIR, "实控人关系.xlsx")
    if not os.path.exists(filepath):
        return {}, {}

    wb = openpyxl.load_workbook(filepath, data_only=True)
    ws = wb.worksheets[0]
    ctrl_to_companies = {}
    company_to_ctrl = {}
    for row in ws.iter_rows(min_row=2, max_col=2, values_only=True):
        ctrl, comp = row
        if ctrl and comp:
            ctrl = str(ctrl).strip()
            comp = str(comp).strip()
            ctrl_to_companies.setdefault(ctrl, []).append(comp)
            company_to_ctrl[comp] = ctrl
    wb.close()
    return ctrl_to_companies, company_to_ctrl


def resolve_upload_path(dim, name, category, project=''):
    """根据维度+名称+分类，返回原始数据的目标目录路径"""
    if dim == 'controller':
        # 个人/{name}/{category}
        if name and category:
            return os.path.join(RAW_DATA_DIR, "个人", name, category)
    elif dim == 'company':
        # 企业/{name}/{category}
        if name and category:
            return os.path.join(RAW_DATA_DIR, "企业", name, category)
    elif dim == 'project':
        # 企业/{name}/项目/{project}/{category}
        if name and project and category:
            return os.path.join(RAW_DATA_DIR, "企业", name, "项目", project, category)
    return None


def scan_controllers():
    """扫描实控人维度数据完整性（原始数据/个人/）"""
    ctrl_to_companies, _ = _load_controller_company_map()
    personal_dir = os.path.join(RAW_DATA_DIR, "个人")

    # 个人模版的子目录作为检查项
    check_items = ["征信", "身份证", "资产"]

    results = []
    names = _list_subdirs(personal_dir)

    for name in names:
        person_dir = os.path.join(personal_dir, name)
        companies = ctrl_to_companies.get(name, [])

        data = {}
        for item in check_items:
            data[item] = _dir_has_files(os.path.join(person_dir, item))

        results.append({
            "name": name,
            "companies": companies,
            "data": data,
        })

    # 实控人关系表中有但原始数据不存在的
    for ctrl_name in ctrl_to_companies:
        if ctrl_name not in [r["name"] for r in results]:
            data = {item: False for item in check_items}
            results.append({
                "name": ctrl_name,
                "companies": ctrl_to_companies[ctrl_name],
                "data": data,
                "missing_folder": True,
            })

    return results


def scan_companies():
    """扫描企业维度数据完整性（原始数据/企业/）"""
    _, company_to_ctrl = _load_controller_company_map()
    company_dir = os.path.join(RAW_DATA_DIR, "企业")

    check_items = ["征信", "银行流水", "发票", "中登", "营业执照", "劳务派遣证", "人力资源证"]

    results = []
    companies = _list_subdirs(company_dir)

    for comp in companies:
        comp_dir = os.path.join(company_dir, comp)
        proj_dir = os.path.join(comp_dir, "项目")

        data = {}
        for item in check_items:
            data[item] = _dir_has_files(os.path.join(comp_dir, item))

        project_count = len(_list_subdirs(proj_dir)) if os.path.isdir(proj_dir) else 0

        results.append({
            "name": comp,
            "controller": company_to_ctrl.get(comp, ""),
            "data": data,
            "project_count": project_count,
        })

    return results


def scan_projects():
    """扫描项目维度数据完整性（原始数据/企业/{公司}/项目/）"""
    company_dir = os.path.join(RAW_DATA_DIR, "企业")

    check_items = ["考勤账单", "发薪工资表", "出款管理", "保险", "合同"]

    results = []
    companies = _list_subdirs(company_dir)

    for comp in companies:
        proj_base = os.path.join(company_dir, comp, "项目")
        if not os.path.isdir(proj_base):
            continue

        projects = _list_subdirs(proj_base)

        for proj in projects:
            proj_dir = os.path.join(proj_base, proj)

            data = {}
            for item in check_items:
                data[item] = _dir_has_files(os.path.join(proj_dir, item))

            info_file = os.path.join(proj_dir, "项目信息.xlsx")
            data["项目信息"] = os.path.isfile(info_file)

            results.append({
                "company": comp,
                "project": proj,
                "data": data,
            })

    return results
