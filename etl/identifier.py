"""identifier：纯识别角色（不操作 COS、不写 mart）

输入：openpyxl Workbook
输出：{
    'file_detected_type': 'attendance+wage_sheet'   # 文件级标签（用于 COS 归位决策 + raw_files.detected_type 字段）
    'sheets': [
        {'name': 'Sheet1', 'kind': 'attendance', 'handler': 'standard', 'rule_id': 12},
        {'name': '工资表',  'kind': 'wage_sheet', 'handler': 'standard', 'rule_id': 7},
        ...
    ],
    'kao_zhang_fa_gong': True/False,                # 是否含"考账发工"4 类任一
    'has_unknown': True/False,                      # 是否有未识别 sheet
}

dispatcher 拿到这个结果后：
  - 用 file_detected_type 写回 raw_files.detected_type
  - 用 kao_zhang_fa_gong 决定 COS 归位 kind
  - 按 sheet kind+handler 分别 dispatch 到 parsers
"""
from etl.classify import classify_sheet as _classify_v1


# 4 类核心数据 → "考账发工"
KAO_ZHANG_FA_GONG_KINDS = {'attendance', 'bill', 'wage_sheet', 'payroll'}


def identify(wb, project_id=None, conn=None):
    """识别整个 workbook。

    project_id+conn 都给 → 走 classify_v2（项目级 DB 规则；缺规则回退 DEFAULT_RULES + bump match_count）
    任一为 None         → 走 classify_v1（旧硬编码引擎）
    """
    use_v2 = bool(project_id and conn)
    if use_v2:
        from etl.classify_v2 import classify_sheet as _classify_v2

    sheets = []
    seen_kinds = set()
    for sname in wb.sheetnames:
        ws = wb[sname]
        if getattr(ws, 'sheet_state', 'visible') != 'visible':
            continue
        if use_v2:
            matches = _classify_v2(ws, project_id=project_id, conn=conn,
                                    all_matches=True)
            if matches:
                first = matches[0]
                sheets.append({
                    'name': sname,
                    'kind': first['kind'],
                    'handler': first.get('handler'),
                    'rule_id': first.get('rule_id'),
                    'column_mapping': first.get('column_mapping'),
                    'matches': matches,
                })
                for m in matches:
                    seen_kinds.add(m['kind'])
            else:
                sheets.append({
                    'name': sname,
                    'kind': 'unknown',
                    'handler': None,
                    'rule_id': None,
                    'column_mapping': None,
                    'matches': [],
                })
                seen_kinds.add('unknown')
        else:
            kind = _classify_v1(ws)
            sheets.append({
                'name': sname, 'kind': kind,
                'matches': [{'kind': kind, 'handler': None,
                              'rule_id': None, 'column_mapping': None}]
                            if kind not in ('empty', 'unknown') else [],
            })
            seen_kinds.add(kind)

    valid = sorted(k for k in seen_kinds if k not in ('empty', 'unknown'))
    file_detected_type = '+'.join(valid) if valid else 'unknown'

    return {
        'file_detected_type': file_detected_type[:32],   # raw_files.detected_type VARCHAR(32)
        'sheets': sheets,
        'kao_zhang_fa_gong': bool(seen_kinds & KAO_ZHANG_FA_GONG_KINDS),
        'has_unknown': 'unknown' in seen_kinds,
    }
