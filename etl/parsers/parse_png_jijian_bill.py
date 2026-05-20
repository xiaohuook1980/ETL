"""计件账单 PNG 解析 → mart bill_totals (按日一行)

适用场景：万汇橙界计件等"图片账单"项目
- 数据源：钉钉截图 / 微信图片，含"出库结算明细表"等表格
- 表格列：日期 / 短驳交干 / 仓间调拨 / 重量
- 算法：每日重量 × 项目单价（如 0.04 元/千克）= 当日金额
- 装入：bill_totals 每日一行（source_ref='YYYY-MM-DD'）
- 不装：bill_persons（计件账单无人员名单）

OCR 后端：腾讯云 RecognizeTableAccurateOCR（同 cos-ai 凭据）
解析：从 OCR 输出文本提取"4月X日 N N N"行，取最大数=重量
"""
import sys
import re
import base64
import configparser
sys.path.insert(0, 'D:/小鱼AI数据')
from datetime import date
from pathlib import Path
from etl.mart.bills import upsert_bill_total


# ============================================================
# OCR 后端（腾讯云 GeneralAccurateOCR + 坐标聚合行）
# ============================================================
def ocr_image(image_bytes, y_tolerance=10):
    """调腾讯云 GeneralAccurateOCR，按 Y 坐标 ±y_tolerance 聚合成行
    （RecognizeTableAccurateOCR 对钉钉聊天截图底部识别精度低，会漏识"重量"列；
     GeneralAccurateOCR 识别更全，按坐标聚合行恢复表格结构）
    返回行列表（每行内按 X 坐标排序的文本）
    """
    from tencentcloud.common import credential
    from tencentcloud.ocr.v20181119 import ocr_client, models

    cp = configparser.ConfigParser()
    cp.read(Path(r'D:/小鱼AI数据/db_config.txt'), encoding='utf-8')
    cred = credential.Credential(cp['cos-ai']['secret_id'], cp['cos-ai']['secret_key'])
    client = ocr_client.OcrClient(cred, 'ap-guangzhou')

    req = models.GeneralAccurateOCRRequest()
    req.ImageBase64 = base64.b64encode(image_bytes).decode('utf-8')
    resp = client.GeneralAccurateOCR(req)

    items = []
    for d in resp.TextDetections or []:
        if not d.Polygon:
            continue
        y = sum(p.Y for p in d.Polygon) / 4
        x = min(p.X for p in d.Polygon)
        items.append((y, x, d.DetectedText))
    items.sort(key=lambda t: (t[0], t[1]))

    # 聚合：相邻 y 差 < y_tolerance 视为同一行
    lines = []
    current_y = None
    current_line = []
    for y, x, text in items:
        if current_y is None or abs(y - current_y) < y_tolerance:
            current_line.append((x, text))
            current_y = y if current_y is None else (current_y + y) / 2
        else:
            lines.append(' '.join(t for _, t in sorted(current_line, key=lambda t: t[0])))
            current_line = [(x, text)]
            current_y = y
    if current_line:
        lines.append(' '.join(t for _, t in sorted(current_line, key=lambda t: t[0])))
    return '\n'.join(lines)


# ============================================================
# 文本解析：从 OCR 输出抽取每日数据
# ============================================================
def extract_daily_weights(text, year, month):
    """从 OCR 文本提取 {date: weight} 字典

    钉钉截图常含两份表格（早版+修订版）。策略：**晚行覆盖早行**（按 OCR 输出顺序）。
    每行内部取最大数 = 重量列（重量 ≈ 短驳+仓间，是行中最大值）。
    合理范围 10000~500000 过滤异常值。

    已知限制：当晚版的某日期行 OCR 漏识重量列时（如截图底部模糊），结果偏小。
    解决方向：换更精准 OCR 引擎（Claude Vision / PaddleOCR）。
    """
    line_pattern = re.compile(rf'{month}月(\d{{1,2}})日')
    num_pattern = re.compile(r'\d+\.?\d*')

    daily = {}  # 后写入的覆盖前写入的
    for line in text.split('\n'):
        line = line.strip()
        days_in_line = [int(d) for d in line_pattern.findall(line) if 1 <= int(d) <= 31]
        if not days_in_line:
            continue

        nums = [float(x) for x in num_pattern.findall(line)]
        valid = [n for n in nums if 10000 <= n <= 500000]
        N = len(days_in_line)

        # 单日行：取该行最大数 = 重量
        if N == 1:
            try:
                d = date(year, month, days_in_line[0])
            except ValueError:
                continue
            if valid:
                daily[d] = max(valid)
        # 多日合并行：按表格列结构拆（短驳N + 仓间N + 重量N，每日重量=valid[i+2N]）
        elif N >= 2 and len(valid) >= 3 * N:
            for i, day in enumerate(days_in_line):
                try:
                    d = date(year, month, day)
                except ValueError:
                    continue
                daily[d] = valid[i + 2 * N]
        # 多日但数字不足 3N → 拆不出，跳过
    return daily


# ============================================================
# 装入 mart_bill_totals
# ============================================================
def process_png(cur, *, project_id, enterprise_id, source_file_id,
                image_bytes, business_month=None):
    """主入口
    business_month: 'YYYY-MM' 不传则按 OCR 数据推断
    """
    # 项目单价（计件按 '元/千克'）
    cur.execute("""SELECT price, unit FROM unit_prices
                   WHERE project_id=%s AND unit LIKE '%%千克%%'
                   ORDER BY id DESC LIMIT 1""", (project_id,))
    row = cur.fetchone()
    if not row:
        return {'inserted': 0, 'note': f'no unit_price for project {project_id}'}
    unit_price = float(row[0])

    # OCR
    text = ocr_image(image_bytes)

    # 推断 year/month
    if business_month:
        year, month = int(business_month[:4]), int(business_month[5:7])
    else:
        # fallback：按当前年 + OCR 文本含的月份
        m = re.search(r'(\d{4})年(\d{1,2})月', text)
        if not m:
            return {'inserted': 0, 'note': 'cannot infer year/month'}
        year, month = int(m.group(1)), int(m.group(2))
    bm = f'{year:04d}-{month:02d}'

    daily = extract_daily_weights(text, year, month)
    if not daily:
        return {'inserted': 0, 'note': 'no daily weights extracted'}

    n_ins = n_upd = 0
    total_amt = 0.0
    for d, weight in sorted(daily.items()):
        amt = round(weight * unit_price, 2)
        total_amt += amt
        action = upsert_bill_total(cur,
            enterprise_id=enterprise_id, project_id=project_id,
            business_month=bm, amount=amt,
            source_type='png_ocr_jijian',
            source_file_id=source_file_id,
            source_ref=str(d))
        if action == 'insert': n_ins += 1
        else: n_upd += 1

    return {'inserted': n_ins, 'updated': n_upd,
            'parsed_days': len(daily), 'bm': bm,
            'unit_price': unit_price,
            'total_amount': round(total_amt, 2)}
