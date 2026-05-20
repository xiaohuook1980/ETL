"""ETL 通用工具：日期/班次/列名/字典查询/班次名解析等共享函数"""
import re
from datetime import datetime, date, timedelta


# ============================================================
# 1. 日期/数值/班次/列名 基础工具
# ============================================================

def parse_excel_date(v):
    """支持 datetime / date / Excel 序列号 (int|float|str) / 'YYYY-MM-DD' 字符串"""
    if v is None or v == '':
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    if isinstance(v, (int, float)):
        try:
            return (datetime(1899, 12, 30) + timedelta(days=int(v))).date()
        except (ValueError, OverflowError):
            return None
    if isinstance(v, str):
        s = v.strip()
        if not s:
            return None
        # 各种字符串日期格式
        for fmt in ('%Y-%m-%d', '%Y/%m/%d', '%m/%d/%Y', '%Y.%m.%d'):
            try:
                return datetime.strptime(s.split(' ')[0], fmt).date()
            except ValueError:
                continue
        # 点分含单位数月日（'2026.3.26' / '2026.4.1'）
        m = re.match(r'^(\d{4})\.(\d{1,2})\.(\d{1,2})$', s)
        if m:
            try:
                return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            except ValueError:
                pass
        # Excel 序列号（纯数字 / 单一小数点）
        if re.match(r'^\d+(\.\d+)?$', s):
            try:
                return parse_excel_date(int(float(s)))
            except ValueError:
                return None
    return None


def normalize_shift(v):
    """班次归一：白班 / 夜班 / 原值"""
    if v is None: return None
    s = str(v).strip()
    if '白' in s or s == '日班' or '早' in s: return '白班'
    if '夜' in s or s == '晚班': return '夜班'
    return s or None


def find_col(headers, *candidates, priority=False):
    """从表头里找包含任一关键词的列索引；找不到返回 None。

    priority=False（默认）:按列顺序扫,第一个含任一关键词的列返回（关键词顺序不影响）
    priority=True:按关键词优先级扫,先用 candidates[0] 全表头找,找到即返回;否则下一关键词
    """
    if priority:
        for c in candidates:
            for i, h in enumerate(headers):
                if h is None: continue
                if c in str(h):
                    return i
        return None
    for i, h in enumerate(headers):
        if h is None: continue
        h_str = str(h)
        for c in candidates:
            if c in h_str:
                return i
    return None


def safe_float(v):
    if v is None or v == '': return None
    try: return float(v)
    except (ValueError, TypeError): return None


# ============================================================
# 2. 班次标题 → 工时日 解析（B1 移植自 inline 脚本）
# ============================================================

def parse_shift_title(title, pay_time):
    """班次标题 → (parsed_shift_date, payroll_kind)

    payroll_kind:
        'shift_dated'    — 从 title 解析出具体工时日
        'pay_time_based' — 解析失败 fallback 到 pay_time 当天

    识别格式（按优先级）：
        1. 'M.D...'   点分隔（如 '3.31白班'）
        2. 'M-D...'   横杠分隔（如 '2-15结算' / '4-30' / '1-28-1'）  ← B1 新增
        3. 'M/D...'   斜杠分隔（如 '3/31'）                          ← B1 新增
        4. 'M月D日...'
        5. 'YYYYMMDD' 8 位纯数字
        6. '借支' / '预支' → pay_time 月的 15 日（中月发放）         ← B1 新增

    跨年处理：M.month > pay_time.month + 1 时年份回退一年（避免把 12 月底跑到次年 1 月初的算到次年）
    """
    pt = pay_time.date() if pay_time and hasattr(pay_time, 'date') else pay_time
    if pt is None:
        return None, None

    if not title:
        return pt, 'pay_time_based'

    s = str(title).strip()

    # 借支/预支：归到 pay_time 月的 15 日
    if s in ('借支', '预支'):
        try:
            return date(pt.year, pt.month, 15), 'shift_dated'
        except ValueError:
            return pt, 'pay_time_based'

    # YYYYMMDD（8 位纯数字）
    m = re.match(r'^(\d{4})(\d{2})(\d{2})$', s)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3))), 'shift_dated'
        except ValueError:
            pass

    # 'YYYY年M月D日...' / 'YYYY年M月D...' 含完整年份
    m = re.match(r'^(\d{4})年(\d{1,2})月(\d{1,2})', s)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3))), 'shift_dated'
        except ValueError:
            pass

    # 'M月D日...'
    m = re.match(r'^(\d{1,2})月(\d{1,2})', s)
    if m:
        d = _build_date(int(m.group(1)), int(m.group(2)), pt)
        if d:
            return d, 'shift_dated'

    # 'M.D' / 'M-D' / 'M/D' 三种分隔符统一识别（开头）
    # 注意：用 (?!\d{3,}) 防止把 '20251231...' 这种 8 位数误识为 'M=20'
    m = re.match(r'^(\d{1,2})[.\-/](\d{1,2})(?!\d{2,})', s)
    if m:
        d = _build_date(int(m.group(1)), int(m.group(2)), pt)
        if d:
            return d, 'shift_dated'

    # 标题中部含 'M月D日'（如"高陵顺丰WA5月1日出勤结算"）
    m = re.search(r'(?<!\d)(\d{1,2})月(\d{1,2})日?', s)
    if m:
        d = _build_date(int(m.group(1)), int(m.group(2)), pt)
        if d:
            return d, 'shift_dated'

    # 标题中部含 'M.D'（如"清远4.30劳务报酬"）—— 用 search 匹配
    # 用 (?<![\d]) 前置无数字 + (?!\d{2,}) 后置无多位数，避免 '4.302' 被截
    m = re.search(r'(?<!\d)(\d{1,2})[.\-/](\d{1,2})(?!\d{2,})', s)
    if m:
        d = _build_date(int(m.group(1)), int(m.group(2)), pt)
        if d:
            return d, 'shift_dated'

    return pt, 'pay_time_based'


def _build_date(month, day, ref_pt):
    """按 ref_pt 推断年份。month > ref_pt.month + 1 时回退一年（处理跨年场景）"""
    if ref_pt is None:
        return None
    year = ref_pt.year
    if month > ref_pt.month + 1:
        year -= 1
    try:
        return date(year, month, day)
    except ValueError:
        return None


def parse_business_date(text, pay_time):
    r"""通用业务日期解析器（用于 attribution_rules.payroll_bm 配置的列扫描）。

    Args:
        text: 字段内容，str|None
        pay_time: 支付时间 datetime/date，用于跨年回退 + 仅月时推年

    Returns:
        (kind, value):
            ('ymd', date)            — 抽到完整年月日
            ('ym',  (year, month))   — 抽到仅年月（无日；自然月业务周期可定，非自然月需报错）
            (None,  None)            — 抽不到

    优先级（按出现顺序，第一个命中即返回）：
        1. YYYY 年/.−/ M 月/.−/ D 日? / 8 位 YYYYMMDD          → 'ymd'
        2. M 月/.−/ D 日?（年靠 pay_time 推）                  → 'ymd'
        3. '借支' / '预支' → pay_time 月 15 日                  → 'ymd'
        4. YYYY 年/.−/ M 月?（无日）                           → 'ym'
        5. (?<!\d) M 月（仅月）                                → 'ym'（年靠 pay_time 推）
    """
    if not text:
        return None, None
    pt = pay_time.date() if pay_time and hasattr(pay_time, 'date') else pay_time
    s = str(text).strip()
    if not s:
        return None, None

    # ─────── 'ymd' 完整年月日 ───────

    # 借支/预支
    if s in ('借支', '预支') and pt is not None:
        try:
            return 'ymd', date(pt.year, pt.month, 15)
        except ValueError:
            return None, None

    # 8 位 YYYYMMDD（开头）
    m = re.match(r'^(\d{4})(\d{2})(\d{2})(?!\d)', s)
    if m:
        try:
            return 'ymd', date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            pass

    # 'YYYY[年.\-/]M[月.\-/]D[日]?' 完整年月日（含中部出现）
    m = re.search(r'(\d{4})[年.\-/](\d{1,2})[月.\-/](\d{1,2})日?(?!\d)', s)
    if m:
        try:
            return 'ymd', date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            pass

    # 'M月D[日]?'（开头/中部，年靠 pt）
    m = re.search(r'(?<!\d)(\d{1,2})月(\d{1,2})日?(?!\d)', s)
    if m:
        d = _build_date(int(m.group(1)), int(m.group(2)), pt)
        if d:
            return 'ymd', d

    # 'M.D' / 'M-D' / 'M/D'（开头或中部，前后无多位数避免 4位年/2位日 误匹）
    m = re.search(r'(?<!\d)(\d{1,2})[.\-/](\d{1,2})(?!\d{2,})', s)
    if m:
        # 排除 4 位年的情况：先看前面是不是 \d{4}（YMD 已处理）
        # 这里 (?<!\d) 已保证前无数字，所以 4 位年不会到这里
        d = _build_date(int(m.group(1)), int(m.group(2)), pt)
        if d:
            return 'ymd', d

    # ─────── 'ym' 仅年月 ───────

    # 'YYYY[年.\-/]M[月]?' 仅年月（无 D）
    m = re.search(r'(\d{4})[年.\-/](\d{1,2})月?(?!\d)', s)
    if m:
        y, mo = int(m.group(1)), int(m.group(2))
        if 2020 <= y <= 2099 and 1 <= mo <= 12:
            return 'ym', (y, mo)

    # '(?<!\d)M月'（仅月，年靠 pt 推 + 跨年回退）
    m = re.search(r'(?<!\d)(\d{1,2})月(?!\d)', s)
    if m and pt is not None:
        mo = int(m.group(1))
        if 1 <= mo <= 12:
            y = pt.year
            if mo > pt.month + 1:
                y -= 1
            return 'ym', (y, mo)

    return None, None


# ============================================================
# 3. workers 字典：4 档匹配（B3 升档，task #11）
# ============================================================

def _clean_id_card(raw):
    """清洗身份证：去空格，含 * 视为脱敏"""
    if not raw:
        return None
    c = str(raw).strip().replace(' ', '')
    if '*' in c:
        return None  # 脱敏，归档 2（暂不实现匹配）
    if len(c) != 18:
        return None
    return c


def _clean_mobile(raw):
    if not raw:
        return None
    m = str(raw).strip()
    if not m or len(m) < 11:
        return None
    return m


def bulk_get_or_create_workers(cur, names, project_id):
    """批量按 name_only 档拉/建 worker。

    返回 dict: {name: worker_id}
    适用 attendance PDF / xlsx 解析（无 id_card/mobile 场景）。
    """
    names = sorted(set(n.strip() for n in names if n and n.strip()))
    if not names:
        return {}

    # 一次性查所有本项目+name_only 已有 worker
    placeholders = ','.join(['%s'] * len(names))
    cur.execute(f"""SELECT name, id FROM workers
                    WHERE binding_project_id=%s AND name IN ({placeholders})""",
                [project_id] + names)
    cache = {n: i for n, i in cur.fetchall()}

    # 缺失的批量 INSERT
    missing = [n for n in names if n not in cache]
    if missing:
        cur.executemany(
            """INSERT INTO workers (name, binding_project_id, id_source, first_seen_at, last_seen_at)
               VALUES (%s, %s, 'name_only', NOW(), NOW())""",
            [(n, project_id) for n in missing])
        # 重新拉一次拿 id（lastrowid 在 executemany 下不可靠）
        cur.execute(f"""SELECT name, id FROM workers
                        WHERE binding_project_id=%s AND name IN ({placeholders})""",
                    [project_id] + names)
        cache = {n: i for n, i in cur.fetchall()}
    return cache


def get_or_create_worker(cur, name, project_id, *, id_card=None, mobile=None):
    """4 档优先级查找/创建 workers：
        档 1 full_id      — id_card_clean 命中 (binding_project_id=NULL)
        档 2 desensitized — 脱敏身份证（暂不实现，预留 id_source）
        档 3 name_mobile  — name + mobile 命中 (binding_project_id=NULL)
        档 4 name_only    — name + binding_project_id 命中

    同名异人自动识别：
        档 1 命中前若 (name, project_id) 已有不同 id_card_clean 的 worker → 后写者标 duplicate_flag=1

    跨项目同名（档 4）：视为不同人（按 binding_project_id 隔开）
    """
    if not name:
        return None
    name = str(name).strip()
    if not name:
        return None

    id_card_clean = _clean_id_card(id_card)
    mobile_clean = _clean_mobile(mobile)

    # ===== 档 1：full_id =====
    if id_card_clean:
        cur.execute("SELECT id FROM workers WHERE id_card_clean=%s", (id_card_clean,))
        row = cur.fetchone()
        if row:
            return row[0]
        # 同名异人侦测：同项目同名但不同 id_card 已存在 → 标 duplicate_flag
        cur.execute("""SELECT id, id_card_clean FROM workers
                       WHERE name=%s AND binding_project_id=%s""",
                    (name, project_id))
        existing = cur.fetchone()
        is_dup = bool(existing and existing[1] and existing[1] != id_card_clean)
        cur.execute("""INSERT INTO workers
                       (name, id_card_clean, mobile, id_source, duplicate_flag,
                        first_seen_at, last_seen_at, note)
                       VALUES (%s, %s, %s, 'full_id', %s, NOW(), NOW(), %s)""",
                    (name, id_card_clean, mobile_clean, 1 if is_dup else 0,
                     '同名异人自动识别' if is_dup else None))
        return cur.lastrowid

    # ===== 档 3：name + mobile =====
    if mobile_clean:
        cur.execute("SELECT id FROM workers WHERE name=%s AND mobile=%s",
                    (name, mobile_clean))
        row = cur.fetchone()
        if row:
            return row[0]
        cur.execute("""INSERT INTO workers
                       (name, mobile, id_source, first_seen_at, last_seen_at)
                       VALUES (%s, %s, 'name_mobile', NOW(), NOW())""",
                    (name, mobile_clean))
        return cur.lastrowid

    # ===== 档 4：name + project_id =====
    cur.execute("""SELECT id FROM workers
                   WHERE name=%s AND binding_project_id=%s""",
                (name, project_id))
    row = cur.fetchone()
    if row:
        return row[0]
    cur.execute("""INSERT INTO workers
                   (name, binding_project_id, id_source, first_seen_at, last_seen_at)
                   VALUES (%s, %s, 'name_only', NOW(), NOW())""",
                (name, project_id))
    return cur.lastrowid


# ============================================================
# 4. 业务月派生（B2 完整实现）
# ============================================================

def derive_business_month(shift_date, business_cycle='自然月'):
    """按项目周期派生业务月（CHAR(7) YYYY-MM）

    支持的 business_cycle 模式：
        '自然月'         — shift_date 所在自然月
        '上月D1-本月D2'  — 周期跨自然月，业务月 = 周期对应的"本月"
                          例：'上月26-本月25'
                              shift_date=4-30 → 周期 4-26~5-25 → 业务月 5
                              shift_date=4-25 → 周期 3-26~4-25 → 业务月 4
    """
    if shift_date is None:
        return None
    if business_cycle == '自然月' or not business_cycle:
        return shift_date.strftime('%Y-%m')
    m = re.match(r'上月(\d+)-本月(\d+)', business_cycle)
    if m:
        d1 = int(m.group(1))
        if shift_date.day >= d1:
            ny, nm = (shift_date.year, shift_date.month + 1) \
                     if shift_date.month < 12 else (shift_date.year + 1, 1)
            return f'{ny:04d}-{nm:02d}'
        return shift_date.strftime('%Y-%m')
    # 未知格式 fallback 自然月
    return shift_date.strftime('%Y-%m')


def derive_business_period(shift_date, business_cycle):
    """按业务周期算出 shift_date 所属周期的 (start, end) 边界（含）

    支持 business_cycle:
      '自然月'         → (月初, 月底)
      '上月D-本月D-1'  → 非自然月，按 start_day 切分
                          shift_date.day >= D → (本月D, 下月D-1)
                          shift_date.day < D → (上月D, 本月D-1)
    返回 (start_date, end_date) 都是 date 对象（含两端）
    """
    if shift_date is None:
        return None, None
    d = shift_date.date() if hasattr(shift_date, 'date') else shift_date
    if business_cycle == '自然月' or not business_cycle:
        import calendar
        last_day = calendar.monthrange(d.year, d.month)[1]
        return date(d.year, d.month, 1), date(d.year, d.month, last_day)
    m = re.match(r'上月(\d+)-本月(\d+)', business_cycle)
    if m:
        start_day = int(m.group(1))
        end_day = int(m.group(2))
        if d.day >= start_day:
            # 周期 = (本月 start_day, 下月 end_day)
            sy, sm = d.year, d.month
            ey, em = (d.year, d.month + 1) if d.month < 12 else (d.year + 1, 1)
        else:
            # 周期 = (上月 start_day, 本月 end_day)
            sy, sm = (d.year, d.month - 1) if d.month > 1 else (d.year - 1, 12)
            ey, em = d.year, d.month
        return date(sy, sm, start_day), date(ey, em, end_day)
    # fallback 自然月
    import calendar
    last_day = calendar.monthrange(d.year, d.month)[1]
    return date(d.year, d.month, 1), date(d.year, d.month, last_day)


def get_business_cycle(cur, project_id, ref_date=None):
    """从 fish-test.business_cycles 表查项目业务周期 → 字符串（兼容 derive_business_month 老接口）

    优先查 business_cycles 表（按 effective_start/end 选最新生效记录）；
    无记录时 fallback 到 projects.business_cycle 字段（兼容旧代码）。

    返回值：
      '自然月'           — cycle_type=自然月（即每月 1 日 ~ 月底）
      '上月X-本月Y'      — cycle_type=非自然月, start_day=X, end_day=X-1
                          如 start_day=26 → '上月26-本月25'
    """
    sql = """SELECT cycle_type, start_day FROM business_cycles
             WHERE project_id=%s"""
    args = [project_id]
    if ref_date:
        sql += """ AND (effective_start IS NULL OR effective_start <= %s)
                   AND (effective_end IS NULL OR effective_end >= %s)"""
        args.extend([ref_date, ref_date])
    sql += " ORDER BY effective_start DESC, id DESC LIMIT 1"
    cur.execute(sql, args)
    r = cur.fetchone()
    if r:
        cycle_type, start_day = r
        if cycle_type == '自然月':
            return '自然月'
        end_day = start_day - 1
        if end_day < 1:
            end_day = 31  # 极端 start_day=1 视为自然月，但理论上自然月会走前面分支
        return f'上月{start_day}-本月{end_day}'
    # fallback 老字段
    cur.execute("SELECT business_cycle FROM projects WHERE id=%s", (project_id,))
    fallback = cur.fetchone()
    return (fallback[0] if fallback and fallback[0] else '自然月')
