"""压缩包解压：raw_files 中 zip/rar 文件 → 解压 → 每个内部文件入新 raw_files 行

设计：
  - 父 raw_file (zip/rar) parse_status 改成 'extracted'
  - 内部每个文件按 sha256 计算新 hash → INSERT raw_files
    - source_project_ids 复制父文件
    - source_filenames = [父文件名 + '/' + 内部路径]
    - ai_cos_key 写到 ai 桶 _extracted/{父hash前12}/{安全文件名} 路径
    - parse_status='pending' 等 dispatcher 处理
  - 加密压缩包：从 ai 桶 _config/密码.txt 查密码

支持：
  - zip：标准库 zipfile (含 ZipCrypto 加密)
  - rar：rarfile（依赖系统级 unrar）— TODO

用法：
  python etl/raw/extract_archives.py            # 处理所有未解压的 zip/rar (parse_status='pending' 且 mime='zip_or_rar')
  python etl/raw/extract_archives.py --file-id N  # 单个文件
"""
import sys
import io
import json
import hashlib
import zipfile
import argparse
import re
sys.path.insert(0, 'D:/小鱼AI数据')
from datetime import datetime
from scripts._db import connect
from scripts._cos import download_bytes, upload_bytes, exists


# ============================================================
# 密码字典缓存
# ============================================================
_password_cache = None

def _load_passwords():
    """从 ai 桶 _config/密码.txt 读密码字典 (filename -> password)"""
    global _password_cache
    if _password_cache is not None:
        return _password_cache
    try:
        text = download_bytes('_config/密码.txt').decode('utf-8')
    except Exception:
        _password_cache = {}
        return _password_cache
    pwds = {}
    for line in text.split('\n'):
        line = line.strip()
        if not line: continue
        parts = re.split(r'[\t]+|\s{2,}', line, maxsplit=1)
        if len(parts) == 2:
            fname, pwd = parts[0].strip(), parts[1].strip()
            pwds[fname] = pwd
    _password_cache = pwds
    return pwds


def _safe_filename(name):
    """文件名转 ai 桶 key 安全字符（保留中文，去 path 分隔符）"""
    return name.replace('\\', '_').replace('/', '_')


# ============================================================
# zip 解压 + 装入 raw_files
# ============================================================
def extract_zip(cur, parent_fid):
    """解压 raw_files.id=parent_fid 的 zip，每个内部文件 → 新 raw_files 行
    返回 [{filename, status, child_fid}]
    """
    cur.execute("""SELECT file_hash, ai_cos_key, source_filenames, source_project_ids,
                          source_bill_ids, parse_status
                   FROM raw_files WHERE id=%s""", (parent_fid,))
    row = cur.fetchone()
    if not row:
        return [{'status': 'parent_not_found'}]
    parent_hash, parent_key, names_j, projs_j, bills_j, status = row
    parent_names = json.loads(names_j) if isinstance(names_j, str) else (names_j or [])
    projs = json.loads(projs_j) if isinstance(projs_j, str) else (projs_j or [])
    bills = json.loads(bills_j) if isinstance(bills_j, str) else (bills_j or [])
    parent_filename = parent_names[0] if parent_names else f'{parent_hash[:8]}.zip'

    body = download_bytes(parent_key)
    if body[:2] != b'PK':
        return [{'status': 'not_zip', 'magic': repr(body[:8])}]

    zf = zipfile.ZipFile(io.BytesIO(body))
    pwds = _load_passwords()
    # 先尝试不加密读，失败时按文件名查密码
    candidate_pwds = []
    if parent_filename in pwds:
        candidate_pwds.append(pwds[parent_filename].encode('utf-8'))
    # 全密码作为兜底（很少，OK）
    candidate_pwds.extend(p.encode('utf-8') for p in set(pwds.values()))

    results = []
    for info in zf.infolist():
        if info.is_dir():
            continue
        # 跳过 macOS metadata
        if info.filename.startswith('__MACOSX/') or info.filename.endswith('.DS_Store'):
            continue
        try:
            content = zf.read(info)
        except RuntimeError as e:
            if 'password' not in str(e).lower():
                results.append({'filename': info.filename, 'status': 'read_failed', 'error': str(e)})
                continue
            content = None
            for pwd in candidate_pwds:
                try:
                    content = zf.read(info, pwd=pwd)
                    break
                except RuntimeError:
                    continue
            if content is None:
                results.append({'filename': info.filename, 'status': 'password_unknown'})
                continue

        # 子文件 hash + ai 桶 key
        child_hash = hashlib.sha256(content).hexdigest()
        safe_name = _safe_filename(info.filename)
        child_key = f'_extracted/{parent_hash[:12]}/{safe_name}'
        child_source_name = f'{parent_filename}/{info.filename}'

        # raw_files 已有？
        cur.execute("""SELECT id, source_project_ids, source_filenames
                       FROM raw_files WHERE file_hash=%s""", (child_hash,))
        existing = cur.fetchone()
        if existing:
            cid, ep_j, en_j = existing
            ep = json.loads(ep_j) if isinstance(ep_j, str) else (ep_j or [])
            en = json.loads(en_j) if isinstance(en_j, str) else (en_j or [])
            for p in projs:
                if p not in ep: ep.append(p)
            if child_source_name not in en:
                en.append(child_source_name)
            cur.execute("""UPDATE raw_files SET source_project_ids=%s, source_filenames=%s,
                           last_seen_at=NOW() WHERE id=%s""",
                        (json.dumps(ep), json.dumps(en, ensure_ascii=False), cid))
            results.append({'filename': info.filename, 'status': 'duplicate', 'child_fid': cid})
            continue

        # 新文件 → 上传 ai 桶 + INSERT raw_files
        if not exists(child_key):
            upload_bytes(child_key, content)
        cur.execute("""INSERT INTO raw_files
                       (file_hash, file_size, ai_cos_key,
                        source_urls, source_filenames, source_bill_ids, source_project_ids,
                        first_uploaded_at, last_seen_at, parse_status)
                       VALUES (%s, %s, %s, '[]', %s, %s, %s, NOW(), NOW(), 'pending')""",
                    (child_hash, len(content), child_key,
                     json.dumps([child_source_name], ensure_ascii=False),
                     json.dumps(bills),
                     json.dumps(projs)))
        results.append({'filename': info.filename, 'status': 'new', 'child_fid': cur.lastrowid})

    # 父 raw_file 标 extracted
    cur.execute("""UPDATE raw_files SET parse_status='extracted', parsed_at=NOW(),
                   parse_error=NULL WHERE id=%s""", (parent_fid,))
    return results


_UNRAR_TOOL = r'C:/Program Files/WinRAR/UnRAR.exe'


def extract_rar(cur, parent_fid):
    """rar 解压（用 WinRAR 自带 UnRAR.exe）"""
    import rarfile, tempfile, os
    rarfile.UNRAR_TOOL = _UNRAR_TOOL

    cur.execute("""SELECT file_hash, ai_cos_key, source_filenames, source_project_ids,
                          source_bill_ids, parse_status
                   FROM raw_files WHERE id=%s""", (parent_fid,))
    row = cur.fetchone()
    if not row:
        return [{'status': 'parent_not_found'}]
    parent_hash, parent_key, names_j, projs_j, bills_j, status = row
    parent_names = json.loads(names_j) if isinstance(names_j, str) else (names_j or [])
    projs = json.loads(projs_j) if isinstance(projs_j, str) else (projs_j or [])
    bills = json.loads(bills_j) if isinstance(bills_j, str) else (bills_j or [])
    parent_filename = parent_names[0] if parent_names else f'{parent_hash[:8]}.rar'

    body = download_bytes(parent_key)
    if body[:4] != b'Rar!':
        return [{'status': 'not_rar', 'magic': repr(body[:4])}]

    # rarfile 需要文件路径（不能直接 BytesIO）
    pwds = _load_passwords()
    candidate_pwds = []
    if parent_filename in pwds:
        candidate_pwds.append(pwds[parent_filename])
    candidate_pwds.extend(set(pwds.values()))

    with tempfile.NamedTemporaryFile(suffix='.rar', delete=False) as tmp:
        tmp.write(body)
        tmp_path = tmp.name

    results = []
    try:
        rf = rarfile.RarFile(tmp_path)
        for info in rf.infolist():
            if info.is_dir(): continue
            if info.filename.endswith('.DS_Store'): continue
            try:
                content = rf.read(info)
            except rarfile.PasswordRequired:
                content = None
                for pwd in candidate_pwds:
                    try:
                        content = rf.read(info, pwd=pwd)
                        break
                    except (rarfile.PasswordRequired, rarfile.BadRarFile):
                        continue
                if content is None:
                    results.append({'filename': info.filename, 'status': 'password_unknown'})
                    continue

            child_hash = hashlib.sha256(content).hexdigest()
            safe_name = _safe_filename(info.filename)
            child_key = f'_extracted/{parent_hash[:12]}/{safe_name}'
            child_source_name = f'{parent_filename}/{info.filename}'

            cur.execute("""SELECT id, source_project_ids, source_filenames
                           FROM raw_files WHERE file_hash=%s""", (child_hash,))
            existing = cur.fetchone()
            if existing:
                cid, ep_j, en_j = existing
                ep = json.loads(ep_j) if isinstance(ep_j, str) else (ep_j or [])
                en = json.loads(en_j) if isinstance(en_j, str) else (en_j or [])
                for p in projs:
                    if p not in ep: ep.append(p)
                if child_source_name not in en:
                    en.append(child_source_name)
                cur.execute("""UPDATE raw_files SET source_project_ids=%s, source_filenames=%s,
                               last_seen_at=NOW() WHERE id=%s""",
                            (json.dumps(ep), json.dumps(en, ensure_ascii=False), cid))
                results.append({'filename': info.filename, 'status': 'duplicate', 'child_fid': cid})
                continue

            if not exists(child_key):
                upload_bytes(child_key, content)
            cur.execute("""INSERT INTO raw_files
                           (file_hash, file_size, ai_cos_key,
                            source_urls, source_filenames, source_bill_ids, source_project_ids,
                            first_uploaded_at, last_seen_at, parse_status)
                           VALUES (%s, %s, %s, '[]', %s, %s, %s, NOW(), NOW(), 'pending')""",
                        (child_hash, len(content), child_key,
                         json.dumps([child_source_name], ensure_ascii=False),
                         json.dumps(bills),
                         json.dumps(projs)))
            results.append({'filename': info.filename, 'status': 'new', 'child_fid': cur.lastrowid})

        cur.execute("""UPDATE raw_files SET parse_status='extracted', parsed_at=NOW(),
                       parse_error=NULL WHERE id=%s""", (parent_fid,))
    finally:
        try: os.unlink(tmp_path)
        except: pass
    return results


def detect_archive_type(body):
    if body[:2] == b'PK': return 'zip'
    if body[:4] == b'Rar!': return 'rar'
    return None


# ============================================================
# 主入口：扫描所有压缩文件 raw_files
# ============================================================
def main(file_id=None):
    conn = connect('fish-test')
    cur = conn.cursor()

    if file_id:
        fids = [file_id]
    else:
        # 找还未解压的、可能是压缩包的 raw_files
        # detected_type='unknown' 或者 source_filenames 含 .zip/.rar 后缀
        cur.execute("""SELECT id, source_filenames FROM raw_files
                       WHERE parse_status IN ('pending', 'failed', 'skipped')
                         AND parse_status != 'extracted'""")
        fids = []
        for r in cur.fetchall():
            names = json.loads(r[1]) if isinstance(r[1], str) else (r[1] or [])
            if any(n.lower().endswith(('.zip', '.rar')) for n in names):
                fids.append(r[0])

    print(f'待处理压缩文件: {len(fids)} 个')

    n_ok = n_fail = 0
    for fid in fids:
        cur.execute("SELECT ai_cos_key, source_filenames FROM raw_files WHERE id=%s", (fid,))
        key, names_j = cur.fetchone()
        names = json.loads(names_j) if isinstance(names_j, str) else (names_j or [])
        fname = names[0] if names else '?'
        try:
            body = download_bytes(key)
            atype = detect_archive_type(body)
            if atype == 'zip':
                results = extract_zip(cur, fid)
            elif atype == 'rar':
                results = extract_rar(cur, fid)
            else:
                results = [{'status': f'unknown_archive_{body[:4]!r}'}]
            conn.commit()
            n_new = sum(1 for r in results if r.get('status') == 'new')
            n_dup = sum(1 for r in results if r.get('status') == 'duplicate')
            n_skip = sum(1 for r in results if r.get('status') not in ('new', 'duplicate'))
            print(f'  id={fid:3d} {atype or "?":3s} {fname[:50]:50s} new={n_new} dup={n_dup} skip={n_skip}')
            for r in results:
                if r.get('status') not in ('new', 'duplicate'):
                    print(f'         {r}')
            n_ok += 1
        except Exception as e:
            conn.rollback()
            n_fail += 1
            print(f'  id={fid:3d} [FAIL] {fname[:50]} {type(e).__name__}: {e}')

    print(f'\n汇总：OK={n_ok}, FAIL={n_fail}')


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--file-id', type=int)
    args = ap.parse_args()
    main(file_id=args.file_id)
