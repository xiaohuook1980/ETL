"""拉取最新出款数据：fish-prod → raw → COS → 解压 → parsers → mart

5 步链路（详见 reference_pull_data_5steps.md）。
支持异步：传 task_id → 每步完成时 update fish-test.pull_tasks 表
"""
import sys
import io
import json
import contextlib
from datetime import datetime, date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts._db import connect
from etl.raw import load_fishprod_db, sync_cos, extract_archives, wipe_for_pull
from etl import dispatcher
from etl.standardize import payrolls as std_payrolls
from etl._utils import get_business_cycle, derive_business_period


def _capture_stdout(fn, *args, **kwargs):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        result = fn(*args, **kwargs)
    return result, buf.getvalue()


def _bump_progress(task_id, step, msg, progress_dict, status='running'):
    """更新 pull_tasks 表的进度。task_id=None 时静默跳过（同步调用）"""
    if task_id is None:
        return
    try:
        c = connect('fish-test')
        cur = c.cursor()
        cur.execute("""UPDATE pull_tasks SET
                       status=%s, current_step=%s, step_msg=%s,
                       progress=%s
                       WHERE id=%s""",
                    (status, step, msg[:255],
                     json.dumps(progress_dict, ensure_ascii=False, default=str),
                     task_id))
        c.commit()
        c.close()
    except Exception:
        pass


def pull_data(project_id, since_date=None, task_id=None, business_month=None):
    """
    business_month: 'YYYY-MM'，只拉该业务月数据。优先级高于 since_date。
                    业务月会按项目 business_cycle 推算 [period_start, period_end] 区间。
    since_date:     'YYYY-MM-DD'，兜底（business_month 不传时用）。

    发薪流水来源策略：
      - 项目存在至少 1 个 active 的 xiaoyu_payroll format → 跑 DB 流（fish-prod 小鱼系统）
      - 否则 → 不跑 DB 流；只走 xlsx 上传发薪（dispatcher）
    （xiaoyu_payroll format 的 classify 规则不参与 xlsx 解析，已在 classify_v2 过滤）
    """
    project_id = int(project_id)

    src = connect('fish-prod')
    dst = connect('fish-test')

    cur = dst.cursor()
    cur.execute("SELECT enterprise_id FROM projects WHERE id=%s", (project_id,))
    row = cur.fetchone()
    if not row:
        src.close(); dst.close()
        raise ValueError(f'项目 {project_id} 不存在')
    enterprise_id = row[0]

    period_start = period_end = None
    if business_month:
        cycle = get_business_cycle(cur, project_id)
        y, m = int(business_month[:4]), int(business_month[5:7])
        # 用业务月任一天推业务周期
        period_start, period_end = derive_business_period(date(y, m, 15), cycle)
        since_d = period_start
    else:
        if since_date is None:
            since_date = '2026-02-01'
        since_d = (datetime.strptime(since_date, '%Y-%m-%d').date()
                   if isinstance(since_date, str) else since_date)

    cur.execute("""INSERT INTO etl_batches (started_at, scope_enterprise, scope_project,
                    modules, triggered_by, status)
                   SELECT NOW(), e.short_name, p.title,
                          JSON_ARRAY('pull_data_full'), 'web', 'running'
                   FROM enterprises e, projects p
                   WHERE e.id=%s AND p.id=%s""", (enterprise_id, project_id))
    batch_id = cur.lastrowid
    dst.commit()

    out = {
        'project_id': str(project_id),
        'business_month': business_month,
        'since_date': since_d.isoformat() if since_d else None,
        'period_start': period_start.isoformat() if period_start else None,
        'period_end': period_end.isoformat() if period_end else None,
        'step1_db_mirror': {},
        'step2_cos': {'sync_log_tail': ''},
        'step3_extract': {'extract_log_tail': ''},
        'step4_parse': {'parse_log_tail': ''},
        'step5_standardize': {},
        'errors': [],
    }
    if task_id is not None:
        c = connect('fish-test')
        cu = c.cursor()
        cu.execute("UPDATE pull_tasks SET status='running', started_at=NOW() WHERE id=%s", (task_id,))
        c.commit(); c.close()

    try:
        _bump_progress(task_id, 1, 'fish-prod 镜像中…', out)
        out['step1_db_mirror']['mini_a_bill'] = load_fishprod_db.load_mini_a_bill(
            batch_id, project_id, enterprise_id, src, dst,
            since_date=since_d, business_month=business_month)
        out['step1_db_mirror']['mini_user_shift_rel'] = load_fishprod_db.load_mini_user_shift_rel(
            batch_id, project_id, enterprise_id, src, dst,
            since_date=period_start or since_d, until_date=period_end)
        out['step1_db_mirror']['mini_shift'] = load_fishprod_db.load_mini_shift(
            batch_id, project_id, enterprise_id, src, dst,
            since_date=period_start or since_d, until_date=period_end)
        out['step1_db_mirror']['loan_records'] = load_fishprod_db.load_mini_loan_record(
            batch_id, project_id, enterprise_id, src, dst,
            business_month=business_month)
        dst.commit()
        src.close(); dst.close()

        # ===== step 1.5: 按业务月清空（mart 6 表 + raw_files），让后续步骤完全按上游重建 =====
        if business_month:
            _bump_progress(task_id, 1, '清空业务月旧数据…', out)
            try:
                out['step1_db_mirror']['wipe'] = wipe_for_pull.wipe_business_month(
                    project_id, business_month, dry_run=False)
            except Exception as e:
                out['errors'].append(f'wipe: {type(e).__name__}: {e}')

        _bump_progress(task_id, 2, 'COS 同步中…', out)
        try:
            _, log = _capture_stdout(sync_cos.sync_project, project_id,
                                     business_month=business_month)
            out['step2_cos']['sync_log_tail'] = '\n'.join(log.strip().splitlines()[-8:])
        except Exception as e:
            out['errors'].append(f'COS 同步: {type(e).__name__}: {e}')

        _bump_progress(task_id, 3, '解压压缩包中…', out)
        try:
            _, log = _capture_stdout(extract_archives.main)
            out['step3_extract']['extract_log_tail'] = '\n'.join(log.strip().splitlines()[-10:])
        except Exception as e:
            out['errors'].append(f'解压: {type(e).__name__}: {e}')

        _bump_progress(task_id, 4, '解析 xlsx/PDF 文件中…', out)
        try:
            _, log = _capture_stdout(dispatcher.main,
                                     project_id=project_id, force=True,
                                     business_month=business_month)
            out['step4_parse']['parse_log_tail'] = '\n'.join(log.strip().splitlines()[-15:])
        except Exception as e:
            out['errors'].append(f'解析: {type(e).__name__}: {e}')

        _bump_progress(task_id, 5, 'DB 流发薪标准化中…', out)
        # attendance 不再 standardize DB 流（劳务伪造数据），只接受 xlsx/PDF
        # DB 流跑的条件：项目存在至少 1 个 active 的 xiaoyu_payroll format
        dst4 = connect('fish-test')
        cu4 = dst4.cursor()
        cu4.execute("""SELECT COUNT(*) FROM project_formats
                       WHERE project_id=%s AND is_xiaoyu_payroll=1
                         AND status='active'""", (project_id,))
        xy_active = int((cu4.fetchone() or [0])[0])
        dst4.close()
        if xy_active == 0:
            out['step5_standardize']['payrolls'] = 'skipped (no active xiaoyu_payroll format)'
        else:
            try:
                _capture_stdout(std_payrolls.standardize, project_id)
                out['step5_standardize']['payrolls'] = '已重建（DB 流）'
            except Exception as e:
                out['step5_standardize']['payrolls'] = 'fail'
                out['errors'].append(f'payrolls: {type(e).__name__}: {e}')

        # 收尾：mart 各表统计行数
        dst2 = connect('fish-test')
        cur2 = dst2.cursor()
        for t in ('attendance', 'attendance_summary', 'bill_totals', 'bill_persons',
                  'payrolls', 'wage_sheets'):
            cur2.execute(f'SELECT COUNT(*) FROM {t} WHERE project_id=%s', (project_id,))
            out.setdefault('mart_counts', {})[t] = cur2.fetchone()[0]
        cur2.execute("UPDATE etl_batches SET status='ok', finished_at=NOW() WHERE id=%s", (batch_id,))
        dst2.commit()
        dst2.close()

        # 收尾：写 task ok
        if task_id is not None:
            c = connect('fish-test')
            cu = c.cursor()
            cu.execute("""UPDATE pull_tasks SET
                          status='ok', current_step=6, step_msg='完成',
                          progress=%s, finished_at=NOW()
                          WHERE id=%s""",
                       (json.dumps(out, ensure_ascii=False, default=str), task_id))
            c.commit(); c.close()
    except Exception as e:
        try:
            dst3 = connect('fish-test')
            c = dst3.cursor()
            c.execute("UPDATE etl_batches SET status='failed', finished_at=NOW(), error_message=%s WHERE id=%s",
                      (str(e)[:65000], batch_id))
            dst3.commit()
            dst3.close()
        except Exception:
            pass
        if task_id is not None:
            try:
                c = connect('fish-test')
                cu = c.cursor()
                cu.execute("""UPDATE pull_tasks SET
                              status='failed', step_msg=%s, error_message=%s,
                              progress=%s, finished_at=NOW()
                              WHERE id=%s""",
                           (f'{type(e).__name__}', str(e)[:65000],
                            json.dumps(out, ensure_ascii=False, default=str), task_id))
                c.commit(); c.close()
            except Exception:
                pass
        raise

    return out


_MART_TABLES = ('attendance', 'attendance_summary', 'bill_totals', 'bill_persons',
                'payrolls', 'wage_sheets')


def delete_business_month_data(project_id, business_month):
    """删除指定项目 + 业务月 的 mart 数据（6 张表）。

    raw_files 不动；下次 pull_data 跑 dispatcher 时 force=True 会重跑已 parsed 的 raw，
    重新装入 mart。
    返回 {'deleted': {table: rows}}。
    """
    project_id = int(project_id)
    if not business_month:
        raise ValueError('business_month 必填')
    deleted = {}
    conn = connect('fish-test')
    try:
        cur = conn.cursor()
        for t in _MART_TABLES:
            cur.execute(f'DELETE FROM {t} WHERE project_id=%s AND business_month=%s',
                        (project_id, business_month))
            deleted[t] = cur.rowcount
        conn.commit()
    finally:
        conn.close()
    return {'project_id': str(project_id), 'business_month': business_month,
            'deleted': deleted}


if __name__ == '__main__':
    import argparse, json as _j
    ap = argparse.ArgumentParser()
    ap.add_argument('--project-id', type=int, required=True)
    ap.add_argument('--since', default=None,
                    help='YYYY-MM-DD；business-month 不传时使用')
    ap.add_argument('--business-month', default=None,
                    help='YYYY-MM；优先级高于 since，按业务周期过滤')
    args = ap.parse_args()
    print(_j.dumps(pull_data(args.project_id,
                              since_date=args.since,
                              business_month=args.business_month),
                   ensure_ascii=False, indent=2, default=str))
