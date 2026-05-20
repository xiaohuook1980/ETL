import os
import re
import sys
from datetime import date, datetime
from pathlib import Path

# 让 etl/* 模块可被 import（web/ 在 D:/小鱼AI数据/web/，etl/ 在同级）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from flask import Flask, render_template, jsonify, request, send_file
from config import HOST, PORT
from services.data_loader import list_companies, list_projects, list_months, load_project_info, load_data_status, clear_cache
from services.precondition_rules import (
    load_controller_info, load_project_info_full, load_total_in_loan,
    run_precondition_checks,
)
from services.payment_calculator import calculate_payment
from services.calc_adapter import run_calc as run_xy_calc
from services.data_completeness import scan_controllers, scan_companies, scan_projects, resolve_upload_path

# etl 接口（项目注册 + 归属规则 + 考勤设置）
from etl.views import projects as projects_view
from etl.views import attribution as attribution_view
from etl.views import kaoqin_settings as kaoqin_view
from etl.views import classify_settings as classify_view
from etl.actions import projects as projects_action
from etl.actions import sync_projects as sync_action
from etl.actions import attribution as attribution_action
from etl.actions import kaoqin_settings as kaoqin_action
from etl.actions import classify_settings as classify_action
from etl.actions import project_formats as formats_action
from etl.views import project_formats as formats_view

app = Flask(__name__)
app.config['JSON_AS_ASCII'] = False


def _cleanup_zombie_tasks_on_startup():
    """Flask 启动时清掉所有 'running'/'pending' 任务（旧 thread 已死）+ running etl_batches。
    避免重启后僵尸 task 挡住"防并发"逻辑。"""
    try:
        from scripts._db import connect
        c = connect('fish-test')
        cu = c.cursor()
        cu.execute("""UPDATE pull_tasks SET status='failed',
                      error_message='zombie cleanup on web startup',
                      finished_at=NOW()
                      WHERE status IN ('pending', 'running')""")
        n_t = cu.rowcount
        cu.execute("""UPDATE etl_batches SET status='failed',
                      error_message='zombie cleanup on web startup',
                      finished_at=NOW()
                      WHERE status='running'""")
        n_b = cu.rowcount
        c.commit()
        c.close()
        if n_t or n_b:
            print(f'[startup] zombie cleanup: {n_t} tasks + {n_b} batches')
    except Exception as e:
        print(f'[startup] zombie cleanup 失败（忽略）: {type(e).__name__}: {e}')


# Flask debug 模式 reloader 会启动两次（监听进程 + 真正 worker），
# 只在 worker 进程跑 cleanup（避免 reloader 进程也跑一次）
if not os.environ.get('WERKZEUG_RUN_MAIN') == 'false':
    _cleanup_zombie_tasks_on_startup()


# ============ 页面路由 ============

@app.route('/')
def index():
    return render_template('index.html')


@app.route('/payment/new')
def payment_new():
    return render_template('payment_form.html')


@app.route('/payment/report')
def payment_report():
    """旧入口，兼容。建议直接用 /payment/report/xy 或 /payment/report/bw"""
    return render_template('payment_report.html')


@app.route('/payment/report/xy')
def payment_report_xy():
    """小鱼风控：出款计算明细（普通 17 项 / 预付 13 项）"""
    return render_template('payment_report_xy.html')


# ============ API路由 ============

@app.route('/api/companies')
def api_companies():
    """返回公司列表（仅已注册项目所属的公司）"""
    regs = projects_view.list_projects(status='registered')
    companies = sorted({p['enterprise_short'] for p in regs if p['enterprise_short']})
    return jsonify({"companies": companies})


@app.route('/api/projects')
def api_projects():
    """返回指定公司的已注册项目（按业务月有数据/最新使用排序，title 作为下拉显示值）"""
    company = request.args.get('company', '')
    regs = projects_view.list_projects(status='registered', company=company)
    return jsonify({
        "company": company,
        "projects": [p['project_title'] for p in regs],
        "_full": [{"project_id": p['project_id'], "title": p['project_title'],
                   "short": p['project_short_name'],
                   "daishou_threshold": p['daishou_threshold'],
                   "profit_ratio": p['profit_ratio'],
                   "business_cycle": p['business_cycle']} for p in regs],
    })


@app.route('/api/months')
def api_months():
    """返回可选月份 + 项目预览信息。给了 company+project 时按业务周期返回带区间 label"""
    company = request.args.get('company', '')
    project = request.args.get('project', '')
    months = list_months(company=company or None, project=project or None)
    preview = load_project_info(company, project)
    return jsonify({"company": company, "project": project, "months": months, "preview": preview})


@app.route('/api/payment/pull-data', methods=['POST'])
def api_payment_pull_data():
    """异步触发拉取：建 pull_tasks 任务行 + 启线程跑 pull_data；立即返回 task_id。
    若该项目已有 running/pending 任务 → 直接返回该 task_id（避免并发重复跑）。"""
    import threading
    from scripts._db import connect
    from etl.actions.pull_data import pull_data
    data = request.get_json() or {}
    project_id = data.get('project_id')
    if not project_id:
        return jsonify({'error': 'project_id 必填'}), 400
    business_month = data.get('business_month') or None
    since_date = data.get('since_date') or None
    if not business_month and not since_date:
        return jsonify({'error': 'business_month 或 since_date 必填一个'}), 400

    conn = connect('fish-test')
    cur = conn.cursor()

    # 防并发：已有 running/pending → 接现有任务
    cur.execute("""SELECT id FROM pull_tasks
                   WHERE project_id=%s AND status IN ('pending', 'running')
                   ORDER BY id DESC LIMIT 1""", (int(project_id),))
    row = cur.fetchone()
    if row:
        conn.close()
        return jsonify({'task_id': row[0], 'status': 'running', 'reused': True})

    cur.execute("""INSERT INTO pull_tasks (project_id, since_date, business_month, status, step_msg)
                   VALUES (%s, %s, %s, 'pending', '排队中')""",
                (int(project_id), since_date, business_month))
    task_id = cur.lastrowid
    conn.commit()
    conn.close()

    def _run():
        try:
            pull_data(int(project_id), since_date=since_date,
                      business_month=business_month, task_id=task_id)
        except Exception:
            pass

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return jsonify({'task_id': task_id, 'status': 'pending', 'reused': False})


@app.route('/api/payment/delete-data', methods=['POST'])
def api_payment_delete_data():
    """删除项目+业务月的 mart 数据（6 张表）。raw_files 保留，下次拉会重装。
    防并发：项目当前有 running/pending 任务时拒绝（避免删-INSERT 互相打架）。"""
    from etl.actions.pull_data import delete_business_month_data
    from scripts._db import connect
    data = request.get_json() or {}
    project_id = data.get('project_id')
    business_month = data.get('business_month')
    if not project_id:
        return jsonify({'error': 'project_id 必填'}), 400
    if not business_month:
        return jsonify({'error': 'business_month 必填'}), 400

    # 防并发：有拉取任务在跑就拒绝
    conn = connect('fish-test')
    try:
        cur = conn.cursor()
        cur.execute("""SELECT id, business_month FROM pull_tasks
                       WHERE project_id=%s AND status IN ('pending','running')
                       ORDER BY id DESC LIMIT 1""", (int(project_id),))
        row = cur.fetchone()
    finally:
        conn.close()
    if row:
        return jsonify({'error': f'拉取任务 #{row[0]} ({row[1]}) 正在跑，请等其完成或停止后再删'}), 409

    try:
        return jsonify(delete_business_month_data(project_id, business_month))
    except ValueError as e:
        return jsonify({'error': str(e)}), 400


@app.route('/api/payment/pull-data/active')
def api_payment_pull_data_active():
    """查项目当前有无 running/pending 任务（页面打开时接续用）。"""
    from scripts._db import connect
    project_id = request.args.get('project_id')
    if not project_id:
        return jsonify({'error': 'project_id 必填'}), 400
    conn = connect('fish-test')
    cur = conn.cursor()
    cur.execute("""SELECT id, status, current_step, step_msg
                   FROM pull_tasks
                   WHERE project_id=%s AND status IN ('pending', 'running')
                   ORDER BY id DESC LIMIT 1""", (int(project_id),))
    row = cur.fetchone()
    conn.close()
    if not row:
        return jsonify({'task_id': None})
    return jsonify({'task_id': row[0], 'status': row[1],
                    'current_step': row[2], 'step_msg': row[3]})


@app.route('/api/payment/pull-data/status')
def api_payment_pull_data_status():
    """轮询 pull_task 进度 + 实时 mart 行数（让用户看到数据持续装入）"""
    import json as _j
    from scripts._db import connect
    task_id = request.args.get('task_id')
    if not task_id:
        return jsonify({'error': 'task_id 必填'}), 400
    conn = connect('fish-test')
    cur = conn.cursor()
    cur.execute("""SELECT id, project_id, status, current_step, step_msg,
                          progress, error_message, started_at, finished_at
                   FROM pull_tasks WHERE id=%s""", (int(task_id),))
    r = cur.fetchone()
    if not r:
        conn.close()
        return jsonify({'error': f'task {task_id} 不存在'}), 404
    progress = r[5]
    if isinstance(progress, str):
        try: progress = _j.loads(progress)
        except: progress = None

    # 实时 mart 行数（活体计数器，让用户看到数据在跳）
    pid = r[1]
    live_counts = {}
    for t in ('attendance', 'attendance_summary', 'bill_totals', 'bill_persons',
              'payrolls', 'wage_sheets'):
        try:
            cur.execute(f'SELECT COUNT(*) FROM {t} WHERE project_id=%s', (pid,))
            live_counts[t] = cur.fetchone()[0]
        except Exception:
            live_counts[t] = None
    # raw_files 状态分布
    cur.execute("""SELECT parse_status, COUNT(*) FROM raw_files
                   WHERE JSON_CONTAINS(source_project_ids, %s) GROUP BY parse_status""",
                (str(pid),))
    raw_status = {row[0]: row[1] for row in cur.fetchall()}
    conn.close()

    return jsonify({
        'task_id': r[0], 'project_id': str(r[1]),
        'status': r[2], 'current_step': r[3], 'step_msg': r[4],
        'progress': progress,
        'error_message': r[6],
        'started_at': r[7].isoformat(sep=' ') if r[7] else None,
        'finished_at': r[8].isoformat(sep=' ') if r[8] else None,
        'live_counts': live_counts,
        'raw_status': raw_status,
    })


@app.route('/api/data-status')
def api_data_status():
    """4 类数据卡片：mode='normal' 返回 4 卡片；'prepay' 返回考勤+发薪 2 卡片。"""
    from etl.views import data_status as ds
    project_id = request.args.get('project_id') or None
    if not project_id:
        company = request.args.get('company', '')
        project = request.args.get('project', '')
        project_id = projects_view.resolve_project_id(company, project)
    if not project_id:
        return jsonify({'error': '项目未找到'}), 404
    business_month = request.args.get('month', '')
    apply_date = request.args.get('date', '')
    mode = request.args.get('mode') or 'normal'
    if not business_month or not apply_date:
        return jsonify({'error': 'month + date 必填'}), 400
    return jsonify(ds.get_data_status(project_id, business_month, apply_date, mode))


@app.route('/api/payment/analyze', methods=['POST'])
def api_analyze():
    """执行出款分析。八维走原有逻辑，小鱼风控直接调 v2 工具（calc_adapter）。"""
    data = request.get_json()
    company = data.get('company', '')
    project = data.get('project', '')
    month_str = data.get('month', '')
    apply_date = data.get('date', '')
    engine = data.get('engine', '小鱼风控分析')

    # 解析月份
    m = re.match(r'(\d{4})年(\d{1,2})月', month_str)
    if not m:
        return jsonify({"error": f"无法解析月份: {month_str}"})
    year, month = int(m.group(1)), int(m.group(2))
    service_month = f"{year:04d}-{month:02d}"

    # 解析申请日期
    try:
        today = datetime.strptime(apply_date, "%Y-%m-%d").date() if apply_date else date.today()
    except ValueError:
        today = date.today()
    apply_date_str = today.strftime('%Y-%m-%d')

    # ====== 小鱼风控：调 etl/calc（替代 v2）======
    mode = data.get('mode') or 'normal'
    customer_amount = data.get('customer_amount')
    calc_formula = data.get('calc_formula') or ('prepay1' if mode == 'prepay' else 'normal1')

    prepay = data.get('prepay') or {}
    base_day_mode = (prepay.get('base_day_mode') or 'peak') if mode == 'prepay' else 'peak'
    if base_day_mode == 'max':
        base_day_mode = 'peak'
    prepay_days = int(prepay.get('prepay_days') or 7) if mode == 'prepay' else 7
    base_day_date = prepay.get('base_day_date') if mode == 'prepay' else None

    # 解析 project_id：前端传了直接用；否则按 (company, project name) 解析
    project_id = data.get('project_id')
    if not project_id:
        project_id = projects_view.resolve_project_id(company, project)
    if not project_id:
        return jsonify({'error': f'未找到项目: {company} / {project}'}), 404

    out = run_xy_calc(
        project_id=int(project_id),
        business_month=service_month,
        apply_date=apply_date_str,
        mode=mode,
        calc_formula=calc_formula,
        customer_amount=customer_amount,
        base_day_mode=base_day_mode,
        prepay_days=prepay_days,
        base_day_date=base_day_date,
    )
    out['engine'] = '小鱼风控'
    return jsonify(out)


@app.route('/api/payment/export-detail')
def api_payment_export_detail():
    """导出出款计算明细 xlsx：6 sheet。
    GET 参数：project_id + month (YYYY-MM)"""
    from services.export_detail import export_detail
    project_id = request.args.get('project_id')
    month = request.args.get('month')
    if not project_id or not month:
        # 兼容前端按 company/project name 解析
        company = request.args.get('company')
        project = request.args.get('project')
        if not project_id and company and project:
            project_id = projects_view.resolve_project_id(company, project)
        if not project_id or not month:
            return jsonify({'error': 'project_id 与 month 必填'}), 400
    # month 兼容 'YYYY年M月'
    m = re.match(r'(\d{4})年(\d{1,2})月', month)
    if m:
        month = f'{int(m.group(1)):04d}-{int(m.group(2)):02d}'
    apply_date = request.args.get('date') or None
    try:
        buf, filename = export_detail(int(project_id), month, apply_date=apply_date)
    except Exception as e:
        return jsonify({'error': f'{type(e).__name__}: {e}'}), 500
    return send_file(
        buf, as_attachment=True, download_name=filename,
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')


@app.route('/api/upload-manual', methods=['POST'])
def api_upload_manual():
    """解析上传的人工数据Excel"""
    if 'file' not in request.files:
        return jsonify({"error": "未上传文件"}), 400

    file = request.files['file']
    if not file.filename.endswith(('.xlsx', '.xls')):
        return jsonify({"error": "仅支持xlsx/xls格式"}), 400

    try:
        import openpyxl, io
        wb = openpyxl.load_workbook(io.BytesIO(file.read()), data_only=True)
        ws = wb.worksheets[0]
        data = {}
        for row in ws.iter_rows(min_row=1, max_col=2, values_only=True):
            if row[0] and row[1] is not None:
                key = str(row[0]).strip()
                try:
                    data[key] = float(row[1])
                except (TypeError, ValueError):
                    data[key] = str(row[1]).strip()
        wb.close()
        return jsonify({"data": data})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ============ 数据补全 ============

@app.route('/data/complete')
def data_complete():
    return render_template('data_complete.html')


@app.route('/api/data/completeness')
def api_data_completeness():
    """返回三维度数据完整性"""
    return jsonify({
        "controllers": scan_controllers(),
        "companies": scan_companies(),
        "projects": scan_projects(),
    })


@app.route('/api/data/upload', methods=['POST'])
def api_data_upload():
    """上传文件到原始数据对应目录"""
    dim = request.form.get('dim', '')       # controller / company / project
    name = request.form.get('name', '')     # 实控人名 或 企业名
    category = request.form.get('category', '')  # 征信、合同等
    project = request.form.get('project', '')    # 项目名（仅项目维度）

    if 'file' not in request.files:
        return jsonify({"error": "未选择文件"}), 400

    target_dir = resolve_upload_path(dim, name, category, project)
    if not target_dir:
        return jsonify({"error": "无法确定目标目录"}), 400

    os.makedirs(target_dir, exist_ok=True)

    uploaded = []
    files = request.files.getlist('file')
    for f in files:
        if f.filename:
            safe_name = f.filename
            filepath = os.path.join(target_dir, safe_name)
            f.save(filepath)
            uploaded.append(safe_name)

    return jsonify({"ok": True, "uploaded": uploaded, "dir": target_dir})


@app.route('/api/payment/recalculate', methods=['POST'])
def api_recalculate():
    """修改参数后重新计算"""
    data = request.get_json()
    # Phase 5 实现
    return jsonify({"status": "todo", "message": "重算功能开发中"})


@app.route('/api/reload', methods=['POST'])
def api_reload():
    """刷新数据缓存"""
    clear_cache()
    return jsonify({"status": "ok", "message": "缓存已刷新"})


# ============ 项目注册（同步项目） ============

@app.route('/projects')
def page_projects():
    return render_template('projects.html')


@app.route('/api/registry/list')
def api_registry_list():
    """项目列表（含筛选）。"""
    status = request.args.get('status') or None
    company = request.args.get('company') or None
    keyword = request.args.get('keyword') or None
    return jsonify({'projects': projects_view.list_projects(status, company, keyword)})


@app.route('/api/registry/sync-status')
def api_registry_sync_status():
    """顶部统计 + 最近同步时间。"""
    return jsonify(projects_view.get_sync_status())


@app.route('/api/registry/companies')
def api_registry_companies():
    """筛选条的劳务公司下拉。"""
    return jsonify({'companies': projects_view.list_companies()})


@app.route('/api/registry/sync-incremental', methods=['POST'])
def api_registry_sync_incremental():
    data = request.get_json() or {}
    since_date = data.get('since_date')
    if not since_date:
        return jsonify({'error': 'since_date 必填，格式 YYYY-MM-DD'}), 400
    try:
        result = sync_action.sync_incremental(since_date)
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    return jsonify(result)


@app.route('/api/registry/sync-one', methods=['POST'])
def api_registry_sync_one():
    """按 劳务公司 + 项目名 关键字拉单个/多个项目。"""
    data = request.get_json() or {}
    try:
        result = sync_action.sync_one(
            enterprise_keyword=data.get('enterprise_keyword'),
            project_keyword=data.get('project_keyword'),
        )
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    return jsonify(result)


@app.route('/api/registry/register', methods=['POST'])
def api_registry_register():
    data = request.get_json() or {}
    try:
        result = projects_action.register_project(
            project_id=int(data['project_id']),
            cycle_key=data['cycle_key'],
            daishou_threshold=int(data['daishou_threshold']),
            profit_ratio=float(data['profit_ratio']),
            custom_start_day=data.get('custom_start_day'),
        )
    except (KeyError, ValueError, TypeError) as e:
        return jsonify({'error': f'参数错误: {e}'}), 400
    return jsonify(result)


@app.route('/api/registry/update', methods=['POST'])
def api_registry_update():
    data = request.get_json() or {}
    try:
        result = projects_action.update_project(
            project_id=int(data['project_id']),
            cycle_key=data.get('cycle_key'),
            daishou_threshold=data.get('daishou_threshold'),
            profit_ratio=data.get('profit_ratio'),
            custom_start_day=data.get('custom_start_day'),
        )
    except (KeyError, ValueError, TypeError) as e:
        return jsonify({'error': f'参数错误: {e}'}), 400
    return jsonify(result)


@app.route('/api/registry/disable', methods=['POST'])
def api_registry_disable():
    data = request.get_json() or {}
    try:
        return jsonify(projects_action.disable_project(int(data['project_id'])))
    except (KeyError, ValueError, TypeError) as e:
        return jsonify({'error': f'参数错误: {e}'}), 400


@app.route('/api/registry/enable', methods=['POST'])
def api_registry_enable():
    data = request.get_json() or {}
    try:
        return jsonify(projects_action.enable_project(int(data['project_id'])))
    except (KeyError, ValueError, TypeError) as e:
        return jsonify({'error': f'参数错误: {e}'}), 400


# ============ 项目设置（归属规则） ============

@app.route('/projects/<project_id>/settings')
def page_project_settings(project_id):
    return render_template('project_settings.html', project_id=project_id)


@app.route('/projects/<project_id>/classify')
def page_classify_redirect(project_id):
    """老 classify 顶层入口已停用，统一跳转到 format 管理页"""
    from flask import redirect
    return redirect(f'/projects/{project_id}/formats', code=302)


@app.route('/projects/<project_id>/classify/<kind>')
def page_classify_kind(project_id, kind):
    """format 详情进入的规则配置页（需 ?format_id=... 参数）。
    无 format_id 时回退跳 format 管理页（避免进入孤儿页面）"""
    from flask import redirect
    if not request.args.get('format_id'):
        return redirect(f'/projects/{project_id}/formats', code=302)
    tmpl_map = {
        'attendance': 'classify_attendance.html',
        'bill':       'classify_bill.html',
        'wage_sheet': 'classify_wage_sheet.html',
        'payroll':    'classify_payroll.html',
    }
    tmpl = tmpl_map.get(kind, 'classify_attendance.html')
    return render_template(tmpl, project_id=project_id, kind=kind)


@app.route('/api/projects/<project_id>/classify/overview')
def api_classify_overview(project_id):
    return jsonify(classify_view.get_overview(project_id))


@app.route('/api/projects/<project_id>/classify/pending')
def api_classify_pending(project_id):
    return jsonify(classify_view.list_pending_sheets(project_id))


@app.route('/api/projects/<project_id>/classify/rules/<kind>')
def api_classify_list(project_id, kind):
    fid = request.args.get('format_id')
    return jsonify(classify_view.list_classify_rules(project_id, kind, format_id=fid))


@app.route('/api/projects/<project_id>/classify/seed', methods=['POST'])
def api_classify_seed(project_id):
    data = request.get_json(silent=True) or {}
    kind = data.get('kind') or None
    replace = bool(data.get('replace'))
    return jsonify(classify_action.seed_default_rules(project_id, kind=kind, replace=replace))


@app.route('/api/projects/<project_id>/classify/rules/<kind>', methods=['POST'])
def api_classify_upsert(project_id, kind):
    data = request.get_json(silent=True) or {}
    return jsonify(classify_action.upsert_classify_rule(project_id, kind, data))


@app.route('/api/projects/<project_id>/classify/rules/<int:rule_id>', methods=['DELETE'])
def api_classify_delete(project_id, rule_id):
    return jsonify(classify_action.delete_classify_rule(project_id, rule_id))


# ============ format 模式 mock 页面 ============
@app.route('/projects/<project_id>/classify-v2/<kind>')
def page_classify_v2_mock(project_id, kind):
    if kind not in ('attendance', 'bill', 'wage_sheet', 'payroll'):
        kind = 'attendance'
    kind_labels = {'attendance':'考勤','bill':'账单','wage_sheet':'工资表','payroll':'发薪流水'}
    return render_template('classify_v2_mock.html',
                           project_id=project_id, kind=kind,
                           kind_label=kind_labels.get(kind, kind))


# ============ format CRUD ============
@app.route('/projects/<project_id>/formats')
def page_project_formats(project_id):
    return render_template('project_formats.html', project_id=project_id)


@app.route('/api/projects/<project_id>/formats')
def api_list_formats(project_id):
    kind = request.args.get('kind')
    return jsonify(formats_view.list_formats(project_id, kind=kind))


@app.route('/api/projects/<project_id>/formats/summary')
def api_formats_summary(project_id):
    return jsonify(formats_view.list_formats_summary(project_id))


@app.route('/api/projects/<project_id>/formats', methods=['POST'])
def api_upsert_format(project_id):
    data = request.get_json(silent=True) or {}
    return jsonify(formats_action.upsert_format(project_id, data))


@app.route('/api/projects/<project_id>/formats/<int:format_id>', methods=['DELETE'])
def api_delete_format(project_id, format_id):
    return jsonify(formats_action.delete_format(project_id, format_id))


@app.route('/api/projects/<project_id>/formats/<int:format_id>/toggle', methods=['POST'])
def api_toggle_format(project_id, format_id):
    data = request.get_json(silent=True) or {}
    enabled = bool(data.get('enabled', True))
    return jsonify(formats_action.toggle_format(project_id, format_id, enabled))


@app.route('/api/projects/<project_id>/validity/rules/<kind>')
def api_validity_list(project_id, kind):
    fid = request.args.get('format_id')
    return jsonify(classify_view.list_validity_rules(project_id, kind, format_id=fid))


@app.route('/api/projects/<project_id>/validity/rules/<kind>', methods=['POST'])
def api_validity_upsert(project_id, kind):
    data = request.get_json(silent=True) or {}
    return jsonify(classify_action.upsert_validity_rule(project_id, kind, data))


@app.route('/api/projects/<project_id>/validity/rules/<int:rule_id>', methods=['DELETE'])
def api_validity_delete(project_id, rule_id):
    return jsonify(classify_action.delete_validity_rule(project_id, rule_id))


@app.route('/api/projects/<project_id>/payroll-biz-date/rules')
def api_pbd_list(project_id):
    fid = request.args.get('format_id')
    return jsonify(classify_view.list_payroll_biz_date_rules(project_id, format_id=fid))


@app.route('/api/projects/<project_id>/payroll-biz-date/rules', methods=['POST'])
def api_pbd_upsert(project_id):
    data = request.get_json(silent=True) or {}
    return jsonify(classify_action.upsert_payroll_biz_date_rule(project_id, data))


@app.route('/api/projects/<project_id>/payroll-biz-date/rules/<int:rule_id>', methods=['DELETE'])
def api_pbd_delete(project_id, rule_id):
    return jsonify(classify_action.delete_payroll_biz_date_rule(project_id, rule_id))


@app.route('/api/projects/<project_id>/enterprise/rules/<kind>')
def api_enterprise_list(project_id, kind):
    fid = request.args.get('format_id')
    return jsonify(classify_view.list_enterprise_rules(project_id, kind, format_id=fid))


@app.route('/api/projects/<project_id>/enterprise/rules/<kind>', methods=['POST'])
def api_enterprise_upsert(project_id, kind):
    data = request.get_json(silent=True) or {}
    return jsonify(classify_action.upsert_enterprise_rule(project_id, kind, data))


@app.route('/api/projects/<project_id>/enterprise/rules/<int:rule_id>', methods=['DELETE'])
def api_enterprise_delete(project_id, rule_id):
    return jsonify(classify_action.delete_enterprise_rule(project_id, rule_id))


# 单价配置 v2：入口页 + 项目详情页
@app.route('/unit-prices')
def page_unit_prices():
    return render_template('unit_prices.html')


@app.route('/unit-prices/<int:project_id>')
def page_unit_prices_detail(project_id):
    return render_template('unit_prices_detail.html', project_id=project_id)


@app.route('/api/unit-prices')
def api_unit_prices_list():
    from etl.views.project_price_rules import list_projects_with_price_summary
    return jsonify(list_projects_with_price_summary())


@app.route('/api/unit-prices/<int:project_id>')
def api_unit_prices_detail(project_id):
    from etl.views.project_price_rules import get_project_price_config
    data = get_project_price_config(project_id)
    if data is None:
        return jsonify({'error': '项目不存在'}), 404
    return jsonify(data)


@app.route('/api/unit-prices/<int:project_id>/config', methods=['POST'])
def api_unit_prices_config_save(project_id):
    from etl.actions.project_price_rules import upsert_project_config
    data = request.get_json(silent=True) or {}
    try:
        return jsonify(upsert_project_config(project_id, data))
    except ValueError as e:
        return jsonify({'error': str(e)}), 400


@app.route('/api/unit-prices/<int:project_id>/rules', methods=['POST'])
def api_unit_prices_rule_upsert(project_id):
    from etl.actions.project_price_rules import upsert_price_rule
    data = request.get_json(silent=True) or {}
    data['project_id'] = project_id
    try:
        return jsonify(upsert_price_rule(data))
    except ValueError as e:
        return jsonify({'error': str(e)}), 400


@app.route('/api/unit-prices/<int:project_id>/rules/<int:rid>', methods=['DELETE'])
def api_unit_prices_rule_delete(project_id, rid):
    from etl.actions.project_price_rules import delete_price_rule
    return jsonify(delete_price_rule(rid))


@app.route('/api/projects/<project_id>/pivot-template/<kind>')
def api_pivot_template_get(project_id, kind):
    from etl.views.pivot_template import get_pivot_template
    fid = request.args.get('format_id', type=int)
    return jsonify(get_pivot_template(project_id, kind, format_id=fid))


@app.route('/api/projects/<project_id>/pivot-template/<kind>', methods=['POST'])
def api_pivot_template_save(project_id, kind):
    from etl.actions.pivot_template import upsert_pivot_template
    data = request.get_json(silent=True) or {}
    fid = request.args.get('format_id', type=int)
    try:
        return jsonify(upsert_pivot_template(project_id, kind, data, format_id=fid))
    except ValueError as e:
        return jsonify({'error': str(e)}), 400


@app.route('/api/projects/<project_id>/attribution')
def api_project_attribution(project_id):
    fid = request.args.get('format_id')
    return jsonify(attribution_view.get_rules(project_id, format_id=fid))


@app.route('/api/projects/<project_id>/attribution', methods=['POST'])
def api_project_attribution_save(project_id):
    data = request.get_json() or {}
    fid = data.get('format_id')
    try:
        results = attribution_action.save_all_rules(
            project_id, data.get('rules') or {}, format_id=fid)
        return jsonify({'saved': len(results), 'results': results})
    except ValueError as e:
        return jsonify({'error': str(e)}), 400


# ============ 聚合标签账单规则 ============

@app.route('/api/projects/<project_id>/aggregate-labels')
def api_aggregate_labels_list(project_id):
    from etl.views.aggregate_label_rules import list_rules
    fid = request.args.get('format_id')
    return jsonify(list_rules(project_id, format_id=fid))


@app.route('/api/projects/<project_id>/aggregate-labels', methods=['POST'])
def api_aggregate_labels_save(project_id):
    """批量保存：覆盖 (project_id, format_id) 下的所有规则。
    body: {format_id?, rules: [{sheet_pattern, label, col_name, enabled?, priority?, note?}, ...]}
    """
    from etl.actions.aggregate_label_rules import save_all_rules
    data = request.get_json() or {}
    fid = data.get('format_id')
    rules = data.get('rules') or []
    try:
        return jsonify(save_all_rules(project_id, fid, rules))
    except ValueError as e:
        return jsonify({'error': str(e)}), 400


@app.route('/api/projects/<project_id>/aggregate-labels/<int:rule_id>', methods=['DELETE'])
def api_aggregate_labels_delete(project_id, rule_id):
    from etl.actions.aggregate_label_rules import delete_rule
    return jsonify(delete_rule(project_id, rule_id))


# ============ 项目基本信息 ============

@app.route('/api/projects/<project_id>/basic')
def api_project_basic_get(project_id):
    """读 projects 表的基本字段（cycle/threshold/ratio/offset_n/offset_unit）"""
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from scripts._db import connect
    conn = connect('fish-test')
    try:
        cur = conn.cursor()
        cur.execute("""SELECT business_cycle, daishou_threshold, profit_ratio,
                              payroll_offset_n, payroll_offset_unit, use_zhifa_as_faxin
                       FROM projects WHERE id=%s""", (int(project_id),))
        r = cur.fetchone()
        if not r:
            return jsonify({'error': 'project not found'}), 404
        cycle = r[0] or '自然月'
        if cycle == '自然月':
            cycle_key, custom_start_day = 'natural', None
        elif cycle in ('上月26-本月25', '26-25'):
            cycle_key, custom_start_day = '26_25', None
        else:
            cycle_key = 'custom'
            import re as _re
            m = _re.search(r'(\d+)', cycle or '')
            custom_start_day = int(m.group(1)) if m else None
        return jsonify({
            'cycle_key': cycle_key,
            'custom_start_day': custom_start_day,
            'business_cycle_raw': cycle,
            'daishou_threshold': int(r[1] or 2000),
            'profit_ratio': float(r[2] or 0.8),
            'payroll_offset_n': int(r[3] or 0),
            'payroll_offset_unit': r[4] or 'day',
            'use_zhifa_as_faxin': bool(r[5]),
        })
    finally:
        conn.close()


@app.route('/api/projects/<project_id>/basic', methods=['POST'])
def api_project_basic_save(project_id):
    """保存基本信息（cycle/threshold/ratio/offset_n/offset_unit）"""
    data = request.get_json() or {}
    try:
        result = projects_action.update_project(
            project_id=int(project_id),
            cycle_key=data.get('cycle_key'),
            daishou_threshold=data.get('daishou_threshold'),
            profit_ratio=data.get('profit_ratio'),
            custom_start_day=data.get('custom_start_day'),
            payroll_offset_n=data.get('payroll_offset_n'),
            payroll_offset_unit=data.get('payroll_offset_unit'),
            use_zhifa_as_faxin=data.get('use_zhifa_as_faxin'),
        )
        return jsonify(result)
    except (KeyError, ValueError, TypeError) as e:
        return jsonify({'error': f'参数错误: {e}'}), 400


# ============ 发薪业务日期（payroll_bm 指定规则 + 偏移） ============

@app.route('/api/projects/<project_id>/payroll-date')
def api_project_payroll_date_get(project_id):
    """读发薪业务日期配置：① 指定规则（mode=extract）+ ② 推断规则（mode=infer，含 offset）"""
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from scripts._db import connect
    conn = connect('fish-test')
    try:
        cur = conn.cursor()
        cur.execute("""SELECT file_columns, column_names, mode, offset_n, offset_unit,
                              enabled, match_count, last_matched_at
                       FROM project_attribution_rules
                       WHERE project_id=%s AND category='payroll_bm'
                         AND scope='project' AND rule_type='column'
                       ORDER BY id""", (int(project_id),))
        extract_rules, infer_rules = [], []
        for r in cur.fetchall():
            base = {
                'file_columns': r[0] or '',
                'column_names': r[1] or '',
                'enabled': bool(r[5]),
                'match_count': int(r[6] or 0),
                'last_matched_at': r[7].isoformat(sep=' ') if r[7] else None,
            }
            if r[2] == 'infer':
                base['offset_n'] = int(r[3] or 0)
                base['offset_unit'] = r[4] or 'day'
                infer_rules.append(base)
            else:
                extract_rules.append(base)
        return jsonify({
            'extract_rules': extract_rules,
            'infer_rules': infer_rules,
        })
    finally:
        conn.close()


@app.route('/api/projects/<project_id>/payroll-date', methods=['POST'])
def api_project_payroll_date_save(project_id):
    """保存发薪业务日期：extract_rules + infer_rules"""
    data = request.get_json() or {}
    try:
        projects_action.save_payroll_bm_rules(
            project_id=int(project_id),
            extract_rules=data.get('extract_rules') or [],
            infer_rules=data.get('infer_rules') or [],
        )
        return jsonify({'ok': True})
    except (KeyError, ValueError, TypeError) as e:
        return jsonify({'error': f'参数错误: {e}'}), 400


# ============ 考勤设置（单价 + 排除规则） ============

@app.route('/api/projects/<project_id>/kaoqin-settings')
def api_project_kaoqin_get(project_id):
    return jsonify(kaoqin_view.get_settings(project_id))


@app.route('/api/projects/<project_id>/kaoqin-settings', methods=['POST'])
def api_project_kaoqin_save(project_id):
    data = request.get_json() or {}
    try:
        return jsonify(kaoqin_action.save_all(
            project_id,
            unit_prices=data.get('unit_prices'),
            filters=data.get('filters'),
            daily_deduction_hours=data.get('daily_deduction_hours'),
        ))
    except ValueError as e:
        return jsonify({'error': str(e)}), 400


# ============ 启动 ============

if __name__ == '__main__':
    print(f"小鱼风控系统启动: http://localhost:{PORT}")
    app.run(host=HOST, port=PORT, debug=True)
