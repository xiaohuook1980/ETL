"""账单 PDF 解析器（梦寺达-长隆系列"附件3 计费明细表"格式）

目标：
  - mart_bill_persons：每个工人一行 (姓名, 合计金额)
  - mart_bill_totals：整份账单一行（合计行的总合计金额）

PDF 表格结构（梦寺达-长隆 计费明细）：
  R0: '部门：XX部 劳务公司：广州梦寺达商务服务有限公司'
  R1: 表头（含合并表头："序号|兼职卡号|姓名|平日|除夕到初三|...|合计|长期/短期|备注"）
  R2: 子表头（出勤时数/费用标准/服务费）
  R3+: 数据行（每行最后一个数值列 = 合计）
  尾行：'合计 ...'（汇总行）

业务月：从首页文本"YYYY年M月"提取。
"""
import io
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from etl._utils import get_or_create_worker
from etl.mart.bills import upsert_bill_total, upsert_bill_person

import pdfplumber


_BM_RE = re.compile(r'(\d{4})\s*年\s*(\d{1,2})\s*月')


def _safe_float(v):
    if v is None or v == '':
        return None
    s = str(v).strip().replace(',', '').replace(' ', '')
    if s in ('-', '—', '~'):
        return None
    s = re.sub(r'[A-Za-z元小时h]', '', s).strip()
    if not s:
        return None
    try:
        return float(s)
    except (ValueError, TypeError):
        return None


def _detect_business_month(pdf, fallback_bm=None):
    if pdf.pages:
        text = pdf.pages[0].extract_text() or ''
        m = _BM_RE.search(text)
        if m:
            return f'{int(m.group(1)):04d}-{int(m.group(2)):02d}'
    return fallback_bm


def _detect_dept(pdf):
    if pdf.pages:
        text = pdf.pages[0].extract_text() or ''
        m = re.search(r'部门[:：]\s*([^\s劳务]+)', text)
        if m:
            return m.group(1).strip()
    return None


def _find_header_row(table):
    """找含"姓名" + "合计" 的表头行 idx（梦寺达账单）。"""
    for i, row in enumerate(table):
        if row is None:
            continue
        joined = '|'.join(str(c) if c is not None else '' for c in row)
        if '姓名' in joined and '合计' in joined:
            return i, row
    return None, None


def _extract_col_indices(header):
    """关键列定位：name + total（合计）"""
    out = {'name': None, 'total': None}
    for i, c in enumerate(header):
        if c is None:
            continue
        s = str(c).strip()
        if '姓名' in s and out['name'] is None:
            out['name'] = i
        elif s == '合计' and out['total'] is None:
            # 取第一个独立"合计"列（避免子标题"合计"误命中）
            out['total'] = i
    return out


def process_pdf(cur, *, project_id, enterprise_id, business_cycle,
                source_file_id, filename, body, fallback_bm=None):
    """账单 PDF → mart_bill_persons + mart_bill_totals。"""
    from etl._attribution import sheet_passes
    if not sheet_passes(cur, project_id, 'kaoqin_bill', 'enterprise', filename):
        return {'inserted_persons': 0, 'inserted_totals': 0,
                'note': f'文件 {filename!r} 不属于本企业'}
    if not sheet_passes(cur, project_id, 'kaoqin_bill', 'project', filename):
        return {'inserted_persons': 0, 'inserted_totals': 0,
                'note': f'文件 {filename!r} 不属于本项目'}

    pdf = pdfplumber.open(io.BytesIO(body))
    try:
        bm = _detect_business_month(pdf, fallback_bm)
        if bm is None:
            return {'inserted_persons': 0, 'inserted_totals': 0,
                    'note': '未识别 business_month'}
        dept = _detect_dept(pdf)

        n_persons = n_totals = 0
        person_amount_sum = 0.0
        for page_idx, page in enumerate(pdf.pages):
            tables = page.extract_tables()
            for tbl_idx, table in enumerate(tables):
                if not table:
                    continue
                hdr_idx, header = _find_header_row(table)
                if hdr_idx is None:
                    continue
                cols = _extract_col_indices(header)
                if cols['name'] is None or cols['total'] is None:
                    continue

                page_total = None
                for ridx, row in enumerate(table[hdr_idx + 1:], start=hdr_idx + 1):
                    if not row:
                        continue
                    name_v = row[cols['name']] if cols['name'] < len(row) else None
                    if not name_v:
                        continue
                    name_s = str(name_v).strip()
                    if not name_s:
                        continue

                    total = _safe_float(row[cols['total']]
                                        if cols['total'] < len(row) else None)
                    if total is None or total <= 0:
                        continue

                    # 合计行（"合计" 在"姓名"列出现）
                    if name_s in ('合计', '小计', '总计'):
                        page_total = total
                        continue

                    src_ref = f'page{page_idx + 1}#tbl{tbl_idx}#R{ridx}'
                    if dept:
                        src_ref = f'{dept} | {src_ref}'

                    wid = get_or_create_worker(cur, name_s, project_id)
                    if not wid:
                        continue

                    upsert_bill_person(
                        cur,
                        enterprise_id=enterprise_id, project_id=project_id,
                        worker_id=wid, business_month=bm,
                        name_raw=name_s, amount=total,
                        source_type='bill_pdf',
                        source_file_id=source_file_id,
                        source_ref=src_ref,
                    )
                    n_persons += 1
                    person_amount_sum += total

                # 装一份 bill_total（用合计行优先；没有就用人员总和）
                tbl_total = page_total if page_total is not None else round(person_amount_sum, 2)
                if tbl_total > 0:
                    src_ref = f'page{page_idx + 1}#tbl{tbl_idx}'
                    if dept:
                        src_ref = f'{dept} | {src_ref}'
                    upsert_bill_total(
                        cur,
                        enterprise_id=enterprise_id, project_id=project_id,
                        business_month=bm, amount=tbl_total,
                        source_type='bill_pdf',
                        source_file_id=source_file_id,
                        source_ref=src_ref,
                    )
                    n_totals += 1
        return {'inserted_persons': n_persons, 'inserted_totals': n_totals,
                'business_month': bm, 'department': dept,
                'persons_sum': round(person_amount_sum, 2)}
    finally:
        pdf.close()
