"""考勤 PDF 解析器（梦寺达-长隆系列"附件2"格式）

目标：把 PDF 表格里的日级工时装入 mart_attendance（每行=一人一日）。
另外把"在岗工时"汇总也装一份到 mart_attendance_summary（calc 取名单/总工时用 UNION）。

PDF 表格结构（梦寺达-长隆系列）：
  R0: '部门：XX部 劳务公司：广州梦寺达商务服务有限公司'
  R1: '序号 | 兼职卡号 | 姓名 | 1 | 2 | ... | N | 在岗工时 | 除夕到初三工时 | 年二八-初四到初七 | 长期/短期 | 备注'
  R2: '日 一 二 三 四 五 六 ...' （周几行）
  R3+: 数据行（每个日列单元格 = 该日工时；'F8' 表示法定假 8 小时；空 = 未出勤）
  最后行：'合计 ...'

业务月：从 PDF 首页文本"动物世界YYYY年M月"提取。

接口：与 xlsx parsers 不同（PDF 没有 sheet 概念，传 body bytes）：
    process_pdf(cur, *, project_id, enterprise_id, business_cycle,
                source_file_id, filename, body, fallback_bm=None)
"""
import io
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from etl._utils import get_or_create_worker, bulk_get_or_create_workers, derive_business_period

import pdfplumber


_BM_RE = re.compile(r'(\d{4})\s*年\s*(\d{1,2})\s*月')


def _safe_float(v):
    if v is None or v == '':
        return None
    s = str(v).strip()
    # 去除 'F' 等法定假标记
    s = re.sub(r'[A-Za-z]', '', s).strip()
    if not s:
        return None
    try:
        return float(s)
    except (ValueError, TypeError):
        return None


def _detect_business_month(pdf, fallback_bm=None):
    """从首页文本"YYYY年M月"提取 business_month；提取不到 fallback。"""
    if pdf.pages:
        text = pdf.pages[0].extract_text() or ''
        m = _BM_RE.search(text)
        if m:
            return f'{int(m.group(1)):04d}-{int(m.group(2)):02d}'
    return fallback_bm


def _detect_dept(pdf):
    """从首页文本"部门：XXX部"提取部门。"""
    if pdf.pages:
        text = pdf.pages[0].extract_text() or ''
        m = re.search(r'部门[:：]\s*([^\s劳务]+)', text)
        if m:
            return m.group(1).strip()
    return None


def _find_attendance_header_row(table):
    """在 PDF 提取的 table 中找含"姓名"+"在岗工时"的表头行 idx。"""
    for i, row in enumerate(table):
        if row is None:
            continue
        joined = '|'.join(str(c) if c is not None else '' for c in row)
        if '姓名' in joined and ('在岗' in joined or '工时' in joined):
            return i, row
    return None, None


def _extract_col_indices(header):
    """从 header 行抽取关键列 idx + 日列映射 day_cols={day: col_idx}。"""
    out = {'name': None, 'card': None, 'hours_total': None, 'class': None, 'note': None,
           'day_cols': {}}
    for i, c in enumerate(header):
        if c is None:
            continue
        s = str(c).strip()
        if '姓名' in s and out['name'] is None:
            out['name'] = i
        elif ('兼职卡号' in s or '工号' in s) and out['card'] is None:
            out['card'] = i
        elif '在岗' in s and ('工时' in s or '时' in s) and out['hours_total'] is None:
            out['hours_total'] = i
        elif ('长期' in s or '短期' in s) and out['class'] is None:
            out['class'] = i
        elif '备注' in s and out['note'] is None:
            out['note'] = i
        elif s.isdigit() and 1 <= int(s) <= 31:
            out['day_cols'][int(s)] = i
    return out


def process_pdf(cur, *, project_id, enterprise_id, business_cycle,
                source_file_id, filename, body, fallback_bm=None):
    """考勤 PDF → mart_attendance_summary。"""
    from etl._attribution import sheet_passes
    # PDF 没有 sheet 概念，但可以把"文件名作为 sheet_name"做归属过滤
    if not sheet_passes(cur, project_id, 'kaoqin_bill', 'enterprise', filename):
        return {'inserted': 0, 'skipped_rows': 0, 'parsed_rows': 0,
                'note': f'文件 {filename!r} 不属于本企业（kaoqin_bill/enterprise sheet 规则未命中）'}
    if not sheet_passes(cur, project_id, 'kaoqin_bill', 'project', filename):
        return {'inserted': 0, 'skipped_rows': 0, 'parsed_rows': 0,
                'note': f'文件 {filename!r} 不属于本项目（kaoqin_bill/project sheet 规则未命中）'}

    # 防 force re-parse 重复：先删本文件装入过的 attendance + attendance_summary 行
    cur.execute("DELETE FROM attendance WHERE project_id=%s AND source_file_id=%s",
                (project_id, source_file_id))
    cur.execute("DELETE FROM attendance_summary WHERE project_id=%s AND source_file_id=%s",
                (project_id, source_file_id))

    pdf = pdfplumber.open(io.BytesIO(body))
    try:
        bm = _detect_business_month(pdf, fallback_bm)
        if bm is None:
            return {'inserted': 0, 'skipped_rows': 0, 'parsed_rows': 0,
                    'note': '未识别 business_month'}

        dept = _detect_dept(pdf)
        # 业务周期边界（写到 attendance_summary 用）
        from datetime import date as _date
        y, m = int(bm[:4]), int(bm[5:7])
        ps, pe = derive_business_period(_date(y, m, 15), business_cycle)

        # 第一遍扫所有 distinct 姓名 → 批量建 worker 缓存
        all_names = []
        for page in pdf.pages:
            for table in page.extract_tables() or []:
                if not table: continue
                hdr_idx, header = _find_attendance_header_row(table)
                if hdr_idx is None: continue
                cols = _extract_col_indices(header)
                if cols['name'] is None: continue
                for row in table[hdr_idx + 1:]:
                    if not row: continue
                    nv = row[cols['name']] if cols['name'] < len(row) else None
                    if nv:
                        nm = str(nv).strip()
                        if nm and nm not in ('合计', '小计', '总计', '序号'):
                            all_names.append(nm)
        worker_cache = bulk_get_or_create_workers(cur, all_names, project_id)

        n_ins = n_skip = n_parsed = 0
        att_batch = []
        sum_batch = []
        from datetime import date as _date
        for page_idx, page in enumerate(pdf.pages):
            tables = page.extract_tables()
            for tbl_idx, table in enumerate(tables):
                if not table:
                    continue
                hdr_idx, header = _find_attendance_header_row(table)
                if hdr_idx is None:
                    continue
                cols = _extract_col_indices(header)
                if cols['name'] is None or cols['hours_total'] is None:
                    continue

                for ridx, row in enumerate(table[hdr_idx + 1:], start=hdr_idx + 1):
                    if not row:
                        continue
                    name = row[cols['name']] if cols['name'] < len(row) else None
                    if not name:
                        continue
                    name_s = str(name).strip()
                    if not name_s or name_s in ('合计', '小计', '总计', '序号'):
                        continue
                    hours_total = _safe_float(row[cols['hours_total']]
                                              if cols['hours_total'] < len(row) else None)
                    if hours_total is None or hours_total <= 0:
                        n_skip += 1
                        continue

                    n_parsed += 1
                    worker_class = None
                    if cols['class'] is not None and cols['class'] < len(row):
                        wc = row[cols['class']]
                        if wc:
                            worker_class = str(wc).strip()

                    wid = worker_cache.get(name_s)
                    if not wid:
                        n_skip += 1
                        continue

                    src_ref = f'page{page_idx + 1}#tbl{tbl_idx}#R{ridx}'
                    if dept:
                        src_ref = f'{dept} | {src_ref}'

                    # 收集日级 attendance（每个 day 列一行）
                    for day, col_idx in cols['day_cols'].items():
                        if col_idx >= len(row):
                            continue
                        cell = row[col_idx]
                        h = _safe_float(cell)
                        if h is None or h <= 0:
                            continue
                        try:
                            sd = _date(y, m, day)
                        except ValueError:
                            continue
                        att_batch.append((enterprise_id, project_id, wid, bm, sd,
                                          worker_class, dept, h,
                                          source_file_id, f'{src_ref}#D{day}', name_s))

                    sum_batch.append((enterprise_id, project_id, wid, bm,
                                      hours_total, worker_class, dept,
                                      source_file_id, src_ref, name_s))
                    n_ins += 1

        # attendance：保留原行为（PDF 把"长期/短期"写到 worker_type 列）
        # att_batch 元组顺序：(ent, pid, wid, bm, sd, worker_class, dept, h, fid, sref, name)
        if att_batch:
            cur.executemany("""INSERT INTO attendance
                (etl_batch_id, ingested_at, enterprise_id, project_id, worker_id,
                 business_month, shift_date, shift_name, worker_type, floor_or_group,
                 hours, quantity, source_type, source_file_id, source_ref, name_raw)
                VALUES (0, NOW(), %s, %s, %s, %s, %s, NULL, %s, %s, %s, NULL,
                        'attendance_pdf', %s, %s, %s)""",
                att_batch)
        # sum_batch 元组顺序：(ent, pid, wid, bm, hours_total, worker_class, dept, fid, sref, name)
        if sum_batch:
            cur.executemany("""INSERT INTO attendance_summary
                (etl_batch_id, ingested_at, enterprise_id, project_id, worker_id,
                 business_month, hours, quantity, worker_type, floor_or_group,
                 source_type, source_file_id, source_ref, name_raw)
                VALUES (0, NOW(), %s, %s, %s, %s, %s, NULL, %s, %s,
                        'attendance_pdf', %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    hours=VALUES(hours), worker_type=VALUES(worker_type),
                    floor_or_group=VALUES(floor_or_group), ingested_at=NOW()""",
                sum_batch)

        return {'inserted': n_ins, 'skipped_rows': n_skip, 'parsed_rows': n_parsed,
                'business_month': bm, 'department': dept}
    finally:
        pdf.close()
