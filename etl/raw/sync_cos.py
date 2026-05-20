"""COS 同步层：fish-prod.mini_a_bill.url → ai-1306031257/_inbox/

规则：
  - 跨桶读 prepreoject-1306031257（用 cos-ai 凭据 SDK GetObject，只读）
  - 写 ai-1306031257（唯一可写桶，硬规则）
  - 文件按 sha256 hash 去重写到 fish-test.raw_files
  - 新文件落 _inbox/{17位时间戳}_{原文件名}，归位由解析 worker 决定
  - 同 hash 多 fileUrl：累加溯源数组，不重复上传
"""
import sys, json, hashlib, re
sys.path.insert(0, 'D:/小鱼AI数据')
from datetime import datetime
from scripts._db import connect
from scripts._cos import client as cos_client, BUCKET as AI_BUCKET, upload_bytes

ALLOWED_WRITE_BUCKET = 'ai-1306031257'
assert AI_BUCKET == ALLOWED_WRITE_BUCKET, f'COS 写桶必须是 {ALLOWED_WRITE_BUCKET}（硬规则）'


def parse_cos_url(fileUrl):
    """https://{bucket}.cos.{region}.myqcloud.com/{key} → (bucket, region, key) 或 None"""
    m = re.match(r'https?://([^.]+)\.cos\.([^.]+)\.myqcloud\.com/(.+)', fileUrl)
    if not m:
        return None
    return m.group(1), m.group(2), m.group(3)


def fetch_external_object(bucket, key):
    """跨桶 GetObject（只读）"""
    resp = cos_client.get_object(Bucket=bucket, Key=key)
    return resp['Body'].get_raw_stream().read()


def _to_list(v):
    """JSON 列读出来可能是 list 也可能是 str，统一为 list"""
    if v is None: return []
    if isinstance(v, list): return v
    return json.loads(v)


def _ts17(dt):
    """datetime → 17 位时间戳 YYYYMMDDhhmmssSSS"""
    return dt.strftime('%Y%m%d%H%M%S') + f'{dt.microsecond // 1000:03d}'


def sync_one_url(cur, fileUrl, src_time, filename, bill_id, project_id):
    """同步单个 fileUrl 到 ai 桶 + raw_files
    返回 (raw_files.id, status)，status ∈ {new, duplicate, skip}
    """
    parsed = parse_cos_url(fileUrl)
    if not parsed:
        return None, 'skip'
    src_bucket, _, src_key = parsed

    # 拉文件
    body = fetch_external_object(src_bucket, src_key)
    sha = hashlib.sha256(body).hexdigest()
    size = len(body)

    # 同 hash 是否已在 raw_files？
    cur.execute("""SELECT id, source_urls, source_filenames, source_bill_ids, source_project_ids
                   FROM raw_files WHERE file_hash=%s""", (sha,))
    row = cur.fetchone()

    if row:
        # 累加溯源数组（去重追加）
        rid, urls, filenames, bill_ids, proj_ids = row
        urls = _to_list(urls); filenames = _to_list(filenames)
        bill_ids = _to_list(bill_ids); proj_ids = _to_list(proj_ids)
        changed = False
        if fileUrl not in urls:    urls.append(fileUrl); changed = True
        if filename not in filenames: filenames.append(filename); changed = True
        if bill_id not in bill_ids: bill_ids.append(bill_id); changed = True
        if project_id not in proj_ids: proj_ids.append(project_id); changed = True
        cur.execute("""UPDATE raw_files
                       SET source_urls=%s, source_filenames=%s,
                           source_bill_ids=%s, source_project_ids=%s,
                           last_seen_at=NOW()
                       WHERE id=%s""",
                    (json.dumps(urls, ensure_ascii=False),
                     json.dumps(filenames, ensure_ascii=False),
                     json.dumps(bill_ids), json.dumps(proj_ids), rid))
        return rid, 'duplicate'

    # 新文件：写 ai 桶 _inbox/
    ai_key = f'_inbox/{_ts17(src_time)}_{filename}'
    upload_bytes(ai_key, body)

    cur.execute("""INSERT INTO raw_files
                   (file_hash, file_size, ai_cos_key,
                    source_urls, source_filenames, source_bill_ids, source_project_ids,
                    first_uploaded_at, last_seen_at, parse_status)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NOW(), 'pending')""",
                (sha, size, ai_key,
                 json.dumps([fileUrl], ensure_ascii=False),
                 json.dumps([filename], ensure_ascii=False),
                 json.dumps([bill_id]), json.dumps([project_id]),
                 src_time))
    return cur.lastrowid, 'new'


def sync_project(project_id, business_month=None):
    """同步指定项目所有 mini_a_bill.url 到 ai 桶 + raw_files（含 etl_batches 记录）
    business_month: 'YYYY-MM' 时只同步该业务月对应 raw_mini_a_bill 行的 url。"""
    conn = connect('fish-test')
    cur = conn.cursor()

    # ===== 启动批次 =====
    cur.execute("""INSERT INTO etl_batches
                   (started_at, scope_enterprise, scope_project, modules,
                    triggered_by, status)
                   SELECT NOW(), e.short_name, p.title,
                          JSON_ARRAY('cos_sync'), 'cli', 'running'
                   FROM enterprises e JOIN projects p ON p.enterprise_id=e.id
                   WHERE p.id=%s""", (project_id,))
    batch_id = cur.lastrowid
    conn.commit()
    print(f'[batch] started id={batch_id} project={project_id}')

    try:
        sql = """SELECT id, project_id, url FROM raw_mini_a_bill
                 WHERE project_id=%s AND url IS NOT NULL"""
        args = [project_id]
        if business_month:
            sql += " AND (bill_month=%s OR bill_month LIKE %s)"
            args += [business_month, f'{business_month}%']
        cur.execute(sql, args)
        rows = cur.fetchall()
        bm_label = f' (业务月={business_month})' if business_month else ''
        print(f'项目 {project_id}: {len(rows)} 行 mini_a_bill 含 url{bm_label}')

        n_new = n_dup = n_skip = 0
        for bill_id, pid, url_json in rows:
            arr = _to_list(url_json)
            for item in arr:
                fileUrl = item.get('fileUrl') or item.get('url')
                if not fileUrl:
                    n_skip += 1; continue
                filename = item.get('originalFileName') or fileUrl.rsplit('/', 1)[-1]
                time_str = item.get('time')
                try:
                    src_time = datetime.strptime(time_str, '%Y-%m-%d %H:%M:%S') if time_str else datetime.now()
                except Exception:
                    src_time = datetime.now()
                try:
                    rid, status = sync_one_url(cur, fileUrl, src_time, filename, bill_id, pid)
                    if status == 'new':       n_new += 1
                    elif status == 'duplicate': n_dup += 1
                    else:                     n_skip += 1
                    conn.commit()
                    print(f'  [{status:9}] bill_id={bill_id} {filename[:50]} → raw_files.id={rid}')
                except Exception as e:
                    conn.rollback()
                    n_skip += 1
                    print(f'  [skip     ] bill_id={bill_id} {filename[:50]} {type(e).__name__}: {str(e)[:80]}')

        # ===== 完成批次 =====
        cur.execute("""UPDATE etl_batches SET status='ok', finished_at=NOW(),
                       raw_rows=JSON_OBJECT('raw_files_new', %s,
                                             'raw_files_duplicate', %s,
                                             'skipped', %s)
                       WHERE id=%s""",
                    (n_new, n_dup, n_skip, batch_id))
        conn.commit()
        print(f'\n汇总 项目 {project_id}: new={n_new} duplicate={n_dup} skip={n_skip}')
        print(f'[batch] ok id={batch_id}')

    except Exception as e:
        cur.execute("""UPDATE etl_batches SET status='failed', finished_at=NOW(),
                       error_message=%s WHERE id=%s""",
                    (str(e)[:65000], batch_id))
        conn.commit()
        raise
    finally:
        conn.close()


if __name__ == '__main__':
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument('--project-id', type=int, required=True)
    ap.add_argument('--business-month', help='YYYY-MM；只同步该业务月对应的 url')
    args = ap.parse_args()
    sync_project(args.project_id, business_month=args.business_month)
