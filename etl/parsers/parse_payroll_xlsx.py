"""发薪流水 xlsx sheet 解析 → mart payrolls

用于不接小鱼系统的项目（劳务公司用银行 APP 打款），与 standardize/payrolls.py（DB 流）共写 mart_payrolls。

冲突规则（fish-prod 优先）：
  装入每条前 SELECT 同三元组 (name_raw, pay_time, work_amount) 是否已有 source_type='fish-prod-mini'，存在跳过。

支持表头字段（task #6 P1 扩展）：
  姓名/户名/收款人 + 付款金额/实发金额/转账金额/发放金额/到账金额
                + 付款时间/转账时间/交易时间/发放时间/到账时间
  可选：身份证/证件号码 / 手机/电话
  可选：班次名称 / 备注/摘要 (用于业务月推断 + 项目关键词过滤)

业务月推断（按优先级）：
  1. 班次名称按 _utils.parse_shift_title 解析（如 '3.31白班' → 3 月）
  2. 备注里 'X月份' / 'X月D日借支' 等中文月份关键词（task #6 P1）
  3. fallback pay_time 月

项目关键词过滤（task #6 P1）：
  projects.payroll_filter_keywords (JSON 数组) 不空时，
  备注列必须含任一关键词才装入；都不含则跳过
"""
import sys
import re
import json
sys.path.insert(0, 'D:/小鱼AI数据')
from datetime import datetime
from etl._utils import (find_col, safe_float, get_or_create_worker,
                        parse_shift_title, derive_business_month)


def _detect_header_row(ws, max_check=8):
    """找含金额列+时间列+姓名列的表头行"""
    for ridx, row in enumerate(ws.iter_rows(max_row=max_check, values_only=True), start=1):
        if not row:
            continue
        head_text = '|'.join(str(v) for v in row if v)
        # 标准发薪 xlsx：付款金额/实发金额/... + 付款时间/转账时间/...
        if (any(k in head_text for k in ('姓名', '户名', '收款人'))
                and any(k in head_text for k in ('付款金额', '实发金额', '转账金额',
                                                  '发放金额', '到账金额'))
                and any(k in head_text for k in ('付款时间', '转账时间', '交易时间',
                                                  '发放时间', '到账时间'))):
            return ridx, list(row)
        # 银行代发代扣业务明细：户名 + 账号 + 金额 + 经办日
        if ('账号' in head_text and '户名' in head_text and '金额' in head_text
                and any(k in head_text for k in ('经办日', '经办时间', '期望日'))):
            return ridx, list(row)
    return None, []


def _parse_pay_time(v):
    if v is None:
        return None
    if isinstance(v, datetime):
        return v
    if isinstance(v, (int, float)):
        # YYYYMMDD 整数格式（代发代扣 '经办日' col 常见）
        s = str(int(v))
        if len(s) == 8:
            try:
                return datetime.strptime(s, '%Y%m%d')
            except ValueError:
                pass
        return None
    if isinstance(v, str):
        s = v.strip()
        for fmt in ('%Y-%m-%d %H:%M:%S', '%Y/%m/%d %H:%M:%S',
                    '%Y-%m-%d %H:%M', '%Y/%m/%d %H:%M',
                    '%Y-%m-%d', '%Y/%m/%d',
                    '%Y%m%d'):  # 代发代扣 '经办日' 常见 YYYYMMDD
            try:
                return datetime.strptime(s, fmt)
            except ValueError:
                continue
    return None


def _bm_from_note(note, pay_time):
    """从备注解析年月 → YYYY-MM
    支持：
      1. YYYY.M / YYYY.MM / YYYY-M / YYYY/M / YYYY年M月（含完整年月）
         如 '2026.3珠海长隆' / '2026.2海洋王国' / '2026年3月份' / '2026-03 工资'
      2. X月 / X月份 / X月D日（年用 pay_time 推断；备注月 > pay_time 月+1 视为跨年回退）
    """
    if not note or pay_time is None:
        return None
    s = str(note)
    # 1. 完整年月：YYYY 后跟 . - / 年 等分隔，再跟 1-2 位月
    m = re.search(r'(\d{4})[年.\-/](\d{1,2})(?:月|[^\d]|$)', s)
    if m:
        y, mon = int(m.group(1)), int(m.group(2))
        if 1 <= mon <= 12 and 2020 <= y <= 2099:
            return f'{y:04d}-{mon:02d}'
    # 2. 仅 X月（年靠 pay_time 推断）
    m = re.search(r'(\d{1,2})月', s)
    if m:
        mon = int(m.group(1))
        if 1 <= mon <= 12:
            year = pay_time.year
            if mon > pay_time.month + 1:
                year -= 1
            return f'{year:04d}-{mon:02d}'
    return None


def _exists_fish_prod_record(cur, project_id, name_raw, pay_time, work_amount):
    cur.execute("""SELECT 1 FROM payrolls
                   WHERE project_id=%s AND name_raw=%s
                     AND pay_time=%s AND work_amount=%s
                     AND source_type='fish-prod-mini'
                   LIMIT 1""",
                (project_id, name_raw, pay_time, work_amount))
    return cur.fetchone() is not None


def _load_keyword_rules(cur, project_id, format_id=None):
    """读本项目 enabled=1 的 payroll/project 列规则。
    format_id 给定 → 取 NULL 兜底 + 当前 format 的；为 None → 取全部

    返回 [(file_columns, column_names, keywords, mode)]
    """
    if format_id is not None:
        cur.execute("""SELECT file_columns, column_names, mode, keywords
                       FROM project_attribution_rules
                       WHERE project_id=%s AND category='payroll'
                         AND scope='project' AND rule_type='column' AND enabled=1
                         AND (format_id IS NULL OR format_id=%s)""",
                    (project_id, int(format_id)))
    else:
        cur.execute("""SELECT file_columns, column_names, mode, keywords
                       FROM project_attribution_rules
                       WHERE project_id=%s AND category='payroll'
                         AND scope='project' AND rule_type='column' AND enabled=1""",
                    (project_id,))
    rules = []
    for fcols, cols, mode, raw in cur.fetchall():
        try:
            kws = raw if isinstance(raw, list) else json.loads(raw or '[]')
            kws = [k for k in kws if k]
            rules.append((fcols or '', cols or '', kws, mode or 'include'))
        except (json.JSONDecodeError, TypeError):
            pass
    return rules


def _file_columns_match(file_columns_str, headers):
    """文件特征列在 headers 中按指定顺序连续相邻出现。

    file_columns_str: 空白(空格/Tab/换行)分隔列名（'#$%' 转义为真实空格）。空字符串视为'总是匹配'。
    匹配方式：列名是表头单元格的子串即算命中，但顺序严格连续。
    """
    if not file_columns_str:
        return True
    parts = [s.replace('#$%', ' ') for s in file_columns_str.split() if s.strip()]
    if not parts:
        return True
    n = len(parts)
    for start in range(len(headers) - n + 1):
        ok = True
        for i, p in enumerate(parts):
            h = headers[start + i]
            if h is None or p not in str(h):
                ok = False
                break
        if ok:
            return True
    return False


def _lookup_bill_month_for_file(cur, source_file_id):
    """取 raw_files.source_bill_ids[0] → fish-prod.mini_a_bill.bill_month。
    用户拍板：达达类项目不挂多 bill，取第一个即可。
    返回 'YYYY-MM' 字符串或 None。
    """
    import json as _json
    cur.execute("SELECT source_bill_ids FROM raw_files WHERE id=%s", (source_file_id,))
    row = cur.fetchone()
    if not row or not row[0]:
        return None
    bill_ids = row[0]
    if isinstance(bill_ids, str):
        try:
            bill_ids = _json.loads(bill_ids)
        except (ValueError, TypeError):
            return None
    if not bill_ids:
        return None
    bid = bill_ids[0]
    # fish-prod.mini_a_bill.bill_month
    from scripts._db import connect
    conn_p = connect('fish-prod')
    try:
        cur_p = conn_p.cursor()
        cur_p.execute("SELECT bill_month FROM mini_a_bill WHERE id=%s", (bid,))
        r = cur_p.fetchone()
        if r and r[0]:
            return str(r[0]).split(' ')[0][:7]  # 'YYYY-MM'
    finally:
        conn_p.close()
    return None


def _process_precomputed_payroll(cur, *, project_id, enterprise_id, business_cycle,
                                  source_file_id, sheet_name, rows, bill_month=None,
                                  format_id=None, collector=None):
    """precomputed 路径：standard.payroll 输出 list[dict] → 写 mart_payrolls。

    新架构纯净版：不读 project_attribution_rules / fish_prod 去重等老机制。
    项目级过滤 → 用户在 UI 配 project_validity_rules（apply_validity 已在上游调）。
    业务日期 → pay_time → derive_business_month；后续接 project_payroll_biz_date_rules。

    三元组去重 (name_raw, pay_time, work_amount)：
        同 project 已有同三元组（任何 source_file_id / source_type） → skip。
        覆盖累积窗口型转账明细多版本场景（同 bill 多个时段快照都通过 dispatcher
        dedup_basename 入库）。

    bill_month: 'YYYY-MM' 字符串。bill_month 规则启用时使用（达达类无时间字段）。
    """
    import json as _json
    from etl._utils import derive_business_month
    from etl.parsers.handlers.payroll_biz_date import load_bd_rules, resolve_business_date
    bd_rules = load_bd_rules(cur, project_id, format_id=format_id)

    # collector 模式下 wipe_for_pull 已清，seen_triples 查 DB 必返回空；
    # 跨文件去重由 collector.flush 处理，此处只查 DB 兼容旧路径
    seen_triples = set()
    if collector is None:
        cur.execute("""SELECT name_raw, pay_time, work_amount FROM payrolls
                       WHERE project_id=%s""", (project_id,))
        for n, pt_v, wa in cur.fetchall():
            try:
                seen_triples.add((n, pt_v, float(wa)))
            except (TypeError, ValueError):
                pass

    insert_sql = """INSERT IGNORE INTO payrolls
                    (etl_batch_id, ingested_at, enterprise_id, project_id, worker_id,
                     business_month, pay_time, parsed_shift_date,
                     work_amount, payroll_kind, alipay_status,
                     source_type, source_file_id, source_ref, name_raw, id_card_raw,
                     is_valid, invalid_reason, count_as_faxin, extra_data)
                    VALUES (0, NOW(), %s, %s, %s, %s, %s, %s, %s, %s, NULL,
                            'xlsx_payroll', %s, %s, %s, %s, %s, %s, %s, %s)"""
    n_ins = n_skip = n_dup_triple = 0
    worker_cache = {}
    batch = []
    # collector 模式：每行算好 biz_d/bm，push 给 collector，不写库
    collected_rows = [] if collector is not None else None
    for r in rows:
        name = r.get('name_raw')
        amt = r.get('work_amount')
        pt = r.get('pay_time')
        if not name or amt is None or pt is None:
            n_skip += 1
            continue
        triple_key = (name, pt, float(amt))
        if triple_key in seen_triples:
            n_dup_triple += 1
            continue
        seen_triples.add(triple_key)
        # 业务日期判定: project_payroll_biz_date_rules (extract → infer → bill_month → fallback pay_time)
        # bill_month 兜底已在 resolve_business_date 内部实现：extract/infer 都失败时返回
        # date(bill_year, bill_month, 1)，derive_business_month 再派生即可。外层不再强制覆盖，
        # 否则会把 extract 抽出来的真实工时月（如班次名称里的"4月"）错算到当前 pull 的 bill_month。
        biz_d = resolve_business_date(pt, r.get('extra_data'), bd_rules, bill_month=bill_month)
        bm = derive_business_month(biz_d, business_cycle)
        if bm is None:
            n_skip += 1
            continue
        id_card = r.get('id_card_raw')
        pkind = r.get('payroll_kind_hint') or 'shift_dated'

        if collector is not None:
            collected_rows.append({
                'name_raw': name,
                'id_card_raw': id_card,
                'pay_time': pt,
                'biz_d': biz_d,
                'bm': bm,
                'work_amount': amt,
                'payroll_kind': pkind,
                'is_valid': int(r.get('is_valid', 1)),
                'invalid_reason': r.get('invalid_reason'),
                'count_as_faxin': int(r.get('count_as_faxin', 1)),
                'extra_data': r.get('extra_data'),
                'row_idx': r.get('row_idx', '?'),
            })
            continue

        wkey = (name, id_card or '')
        wid = worker_cache.get(wkey)
        if wid is None:
            wid = get_or_create_worker(cur, name, project_id, id_card=id_card or None)
            worker_cache[wkey] = wid
        if not wid:
            n_skip += 1
            continue
        extra_data = r.get('extra_data')
        extra_json = _json.dumps(extra_data, ensure_ascii=False) if extra_data else None
        batch.append((
            enterprise_id, project_id, wid, bm, pt, biz_d, amt, pkind,
            source_file_id, f'{sheet_name}#R{r.get("row_idx", "?")}',
            name, id_card,
            int(r.get('is_valid', 1)), r.get('invalid_reason'),
            int(r.get('count_as_faxin', 1)), extra_json,
        ))

    if collector is not None and collected_rows:
        collector.add(project_id=project_id, enterprise_id=enterprise_id,
                       source_file_id=source_file_id, sheet_name=sheet_name,
                       rows=collected_rows)
        return {'collected': len(collected_rows), 'parsed': len(rows),
                'note': 'precomputed_payroll_collector'}

    if batch:
        cur.executemany(insert_sql, batch)
        n_ins = cur.rowcount
    return {'inserted': n_ins, 'skipped': n_skip,
            'dup_triple': n_dup_triple, 'parsed': len(rows),
            'note': 'precomputed_payroll'}


def process_sheet(cur, *, project_id, enterprise_id, business_cycle,
                  source_file_id, sheet_name, ws, precomputed_rows=None,
                  format_id=None, collector=None):
    """装入 mart_payrolls。

    项目归属：由文件 source_project_ids 决定，dispatcher 循环各项目调一次本函数；
    本函数不做行级过滤（无 sheet/keyword 规则）。
    业务日期：走 build_bm_extractor（payroll_bm 配置）+ pay_time 偏移 fallback。

    precomputed_rows: 给定时走简化路径 (_process_precomputed_payroll)，
        跳过 keyword_rules / fish_prod 去重 / bm_extractor。
    format_id: format 模式下从命中 rule 透传。
    """
    if precomputed_rows is not None:
        # 从 raw_files 关联的 bill_id 取 bill_month（达达类无时间字段项目用）
        bill_month = _lookup_bill_month_for_file(cur, source_file_id)
        return _process_precomputed_payroll(
            cur, project_id=project_id, enterprise_id=enterprise_id,
            business_cycle=business_cycle, source_file_id=source_file_id,
            sheet_name=sheet_name, rows=precomputed_rows, bill_month=bill_month,
            format_id=format_id, collector=collector,
        )
    header_row, headers = _detect_header_row(ws)
    if header_row is None:
        return {'inserted': 0, 'skipped': 0, 'parsed': 0,
                'note': 'no payroll header detected'}

    ni = find_col(headers, '姓名', '户名', '收款人')
    # 优先用工人"实到金额"(扣个人服务费/手续费后);"付款金额"是含费总额放最后
    # priority=True:按 keyword 顺序优先,而非列顺序
    ai = find_col(headers, '实际到账金额', '到账金额', '实发金额',
                   '转账金额', '发放金额', '金额', '付款金额', priority=True)
    ti = find_col(headers, '付款时间', '转账时间', '交易时间', '发放时间', '到账时间',
                   '经办日', '经办时间')
    ici = find_col(headers, '身份证', '证件号码')
    mi = find_col(headers, '手机', '电话')
    sti = find_col(headers, '班次名称', '班次', '发薪备注', '批次名称', '批次')
    ri = find_col(headers, '备注/摘要', '备注', '摘要', '注释', '转账备注')

    if ni is None or ai is None or ti is None:
        return {'inserted': 0, 'skipped': 0, 'parsed': 0,
                'note': f'missing required cols (name={ni}, amount={ai}, time={ti})'}

    # 加载本项目规则。决策矩阵：
    #   ① 项目无规则                               → no_filter 全装入
    #   ② 项目有规则,但本文件 file_columns 都不命中 → 整文件跳过
    #   ③ 至少一条规则 file_columns 命中且无列/关键词 → 白名单,全装入此文件
    #   ④ 至少一条规则 file_columns 命中且有列+关键词 → 行级 include/exclude 过滤
    keyword_rules = _load_keyword_rules(cur, project_id, format_id=format_id)
    file_matched_any = False
    whitelisted = False
    active_includes = []
    active_excludes = []
    for fcols, cols_str, kws, mode in keyword_rules:
        if not _file_columns_match(fcols, headers):
            continue
        file_matched_any = True
        if not cols_str or not kws:
            whitelisted = True
            continue
        target_idx = None
        for cn in cols_str.split('|'):
            cn = cn.strip()
            if not cn:
                continue
            idx = find_col(headers, cn)
            if idx is not None:
                target_idx = idx
                break
        if target_idx is None:
            continue
        # 4 模式：include / exclude（子串）、eq / neq（精确等于）
        m = mode or 'include'
        if m in ('exclude', 'neq'):
            active_excludes.append((target_idx, kws, m))
        else:
            active_includes.append((target_idx, kws, m))

    if keyword_rules and not file_matched_any:
        return {'inserted': 0, 'skipped': 0, 'parsed': 0,
                'note': '项目配了规则但本文件 file_columns 都不命中 → 整文件跳过'}
    use_filter = bool(active_includes or active_excludes) and not whitelisted

    from etl._attribution import build_bm_extractor
    bm_extractor = build_bm_extractor(cur, project_id, headers, format_id=format_id)

    # 预加载 fish-prod 优先去重 set（避免每行 SELECT）
    cur.execute("""SELECT name_raw, pay_time, work_amount FROM payrolls
                   WHERE project_id=%s AND source_type='fish-prod-mini'""",
                (project_id,))
    fish_prod_set = set()
    for n, pt_v, wa in cur.fetchall():
        try:
            fish_prod_set.add((n, pt_v, float(wa)))
        except (TypeError, ValueError):
            pass

    # worker_id 缓存（避免重复查询/创建）
    worker_cache = {}  # key=(name, id_card or '', mobile or '') → worker_id

    is_natural_cycle = (not business_cycle) or (business_cycle == '自然月')

    insert_sql = """INSERT IGNORE INTO payrolls
                    (etl_batch_id, ingested_at, enterprise_id, project_id, worker_id,
                     business_month, pay_time, parsed_shift_date,
                     work_amount, payroll_kind, alipay_status,
                     source_type, source_file_id, source_ref, name_raw, id_card_raw,
                     count_as_faxin)
                    VALUES (0, NOW(), %s, %s, %s, %s, %s, %s, %s, %s, NULL,
                            'xlsx_payroll', %s, %s, %s, %s, 1)"""

    n_ins = n_skip = n_dup_fish_prod = n_dup_unique = n_filtered = 0
    n_no_day_skip = 0  # 抽到 ym 但非自然月业务周期 → 报错跳过
    n_parsed = 0

    BATCH_SIZE = 500
    batch_data = []

    def _flush_batch():
        nonlocal n_ins, n_dup_unique
        if not batch_data:
            return
        cur.executemany(insert_sql, batch_data)
        ins_count = cur.rowcount  # pymysql executemany INSERT IGNORE: rowcount = 实际插入行数
        n_ins += ins_count
        n_dup_unique += len(batch_data) - ins_count
        batch_data.clear()

    # === Phase 1: 扫文件收集所有行 + 不重复的 worker key ===
    pending_rows = []  # 每项: (enterprise_id, project_id, None_wid, bm, pt, parsed_dt, amt,
                       #        kind, source_file_id, source_ref, name, id_card, mobile, wkey)
    unique_workers = {}  # wkey → None（待解决 worker_id）
    for ridx, row in enumerate(ws.iter_rows(min_row=header_row + 1, values_only=True),
                                start=header_row + 1):
        if not row or ni >= len(row) or row[ni] is None:
            continue
        name = str(row[ni]).strip()
        if not name or name in ('合计', '小计', '总计'):
            continue
        amt = safe_float(row[ai]) if ai < len(row) else None
        if amt is None or amt <= 0:
            n_skip += 1
            continue
        pt = _parse_pay_time(row[ti] if ti < len(row) else None)
        if pt is None:
            n_skip += 1
            continue

        n_parsed += 1
        id_card = str(row[ici]).strip() if ici is not None and ici < len(row) and row[ici] else None
        mobile = str(row[mi]).strip() if mi is not None and mi < len(row) and row[mi] else None
        shift_title = str(row[sti]).strip() if sti is not None and sti < len(row) and row[sti] else None
        note_val = str(row[ri]).strip() if ri is not None and ri < len(row) and row[ri] else None

        # 行级过滤:exclude/neq 优先 → include/eq 必须命中 → 否则保留
        def _hit_kw(v_str, kws, mode):
            if mode in ('eq', 'neq'):
                return any(kw == v_str for kw in kws)
            return any(kw in v_str for kw in kws)
        if use_filter:
            excluded = False
            for idx, kws, mode in active_excludes:
                if idx >= len(row):
                    continue
                v = row[idx]
                if v is None:
                    continue
                v_str = str(v).strip()
                if _hit_kw(v_str, kws, mode):
                    excluded = True
                    break
            if excluded:
                n_filtered += 1
                continue
            if active_includes:
                matched = False
                for idx, kws, mode in active_includes:
                    if idx >= len(row):
                        continue
                    v = row[idx]
                    if v is None:
                        continue
                    v_str = str(v).strip()
                    if _hit_kw(v_str, kws, mode):
                        matched = True
                        break
                if not matched:
                    n_filtered += 1
                    continue

        # fish-prod 优先：跳过已有同三元组（用预加载 set，避免每行 SELECT）
        if (name, pt, float(amt)) in fish_prod_set:
            n_dup_fish_prod += 1
            continue

        # 业务日期推断：
        # ① 指定规则：扫候选列(包含班次名称/备注/...) → 通用 date 解析器
        # ② 推断规则：fallback = pay_time - N 单位
        bm_kind, bm_val = bm_extractor(row, pt)
        parsed_dt = None
        bm = None
        kind = None
        if bm_kind == 'ymd':
            parsed_dt = bm_val
            bm = derive_business_month(parsed_dt, business_cycle)
            kind = 'shift_dated'
        elif bm_kind == 'ym':
            y, mo = bm_val
            if is_natural_cycle:
                bm = f'{y:04d}-{mo:02d}'
                # 业务日"未明指"但 calc/prepay 按 parsed_shift_date 范围过滤需要值;
                # 补月中 15 日作 calc 代理(不代表"业务日精确到日")
                from datetime import date as _date
                parsed_dt = _date(y, mo, 15)
                kind = 'shift_dated'
            else:
                # 非自然月业务周期 + 仅年月 → 无法定业务周期,报错跳过
                n_no_day_skip += 1
                continue
        else:
            # 兜底:bm_extractor 没匹配任何规则 → 用 pay_time 当业务日(无偏移)
            biz_d = pt.date() if hasattr(pt, 'date') else pt
            parsed_dt = biz_d
            bm = derive_business_month(parsed_dt, business_cycle)
            kind = 'pay_time_based'

        if bm is None:
            n_skip += 1
            continue

        wkey = (name, id_card or '', mobile or '')
        unique_workers[wkey] = None
        pending_rows.append((enterprise_id, project_id, bm, pt, parsed_dt, amt, kind,
                             source_file_id, f'{sheet_name}#R{ridx}', name, id_card, mobile, wkey))

    # === Phase 2: 批量解决 worker_id（每个 unique worker 调一次 get_or_create_worker）===
    for wkey in unique_workers:
        wname, wid_card, wmobile = wkey
        wid = get_or_create_worker(cur, wname, project_id,
                                    id_card=wid_card or None,
                                    mobile=wmobile or None)
        unique_workers[wkey] = wid

    # === Phase 3: 用 worker_id 拼装 + 批量 INSERT ===
    for r in pending_rows:
        ent_id, pid, bm, pt, parsed_dt, amt, kind, sfid, sref, name, id_card, mobile, wkey = r
        wid = unique_workers.get(wkey)
        if not wid:
            n_skip += 1
            continue
        batch_data.append((ent_id, pid, wid, bm, pt, parsed_dt, amt, kind,
                           sfid, sref, name, id_card))
        if len(batch_data) >= BATCH_SIZE:
            _flush_batch()
    _flush_batch()

    return {'inserted': n_ins, 'skipped': n_skip,
            'filtered_by_keyword': n_filtered,
            'dup_fish_prod_priority': n_dup_fish_prod,
            'dup_unique_key': n_dup_unique,
            'no_day_skip_non_natural_cycle': n_no_day_skip,
            'parsed': n_parsed,
            'mode': 'project_filter' if use_filter else 'no_filter'}
