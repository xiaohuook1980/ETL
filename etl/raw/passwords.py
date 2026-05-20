"""加密文件密码查找：先查 ai 桶 _config/密码.txt（filename<TAB>password），
没匹中 → 从文件名正则提取（如"密码xl321"）

[[reference_cos_password_file]]
"""
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts._cos import download_bytes, upload_bytes


_password_cache = None


def _load_passwords():
    """从 ai 桶 _config/密码.txt 读密码字典 (filename → password)"""
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
        if not line or line.startswith('#'):
            continue
        parts = re.split(r'[\t]+|\s{2,}', line, maxsplit=1)
        if len(parts) == 2:
            fname, pwd = parts[0].strip(), parts[1].strip()
            pwds[fname] = pwd
    _password_cache = pwds
    return pwds


def reload_passwords():
    """强制重新读密码文件（写入新密码后调）"""
    global _password_cache
    _password_cache = None
    return _load_passwords()


# 文件名密码提示 regex（"密码XXX" / "pwd XXX" / "pw=XXX"）
_FILENAME_PWD_PATTERNS = [
    re.compile(r'密码\s*[:：=]?\s*([A-Za-z0-9_\-@#$]{2,20})'),
    re.compile(r'\b(?:pwd|password|pw)\s*[:：=]?\s*([A-Za-z0-9_\-@#$]{2,20})', re.IGNORECASE),
]


def lookup_password(filename):
    """查文件密码：
    1. _config/密码.txt 精确匹配文件全名
    2. 去后缀（stem）匹配
    3. 从文件名 regex 提取（"密码xl321" → xl321）
    返回 password 字符串 or None"""
    if not filename:
        return None
    pwds = _load_passwords()
    # 1. 全名匹中
    if filename in pwds:
        return pwds[filename]
    # 2. stem 匹中
    stem = os.path.splitext(filename)[0]
    if stem in pwds:
        return pwds[stem]
    # 3. regex 提取
    for pat in _FILENAME_PWD_PATTERNS:
        m = pat.search(filename)
        if m:
            return m.group(1)
    return None


def append_password_to_file(filename, password):
    """把新密码追加到 ai 桶 _config/密码.txt（不替换现有）"""
    try:
        text = download_bytes('_config/密码.txt').decode('utf-8')
    except Exception:
        text = ''
    if not text.endswith('\n'):
        text += '\n'
    text += f'{filename}\t{password}\n'
    upload_bytes('_config/密码.txt', text.encode('utf-8'))
    reload_passwords()


def decrypt_office(content_bytes, password):
    """用密码解密 xls/xlsx 文件（OLE2 加密）。返回解密后的 bytes。
    失败抛 ValueError。"""
    import io as _io
    import msoffcrypto
    try:
        encrypted = msoffcrypto.OfficeFile(_io.BytesIO(content_bytes))
        encrypted.load_key(password=password)
        out = _io.BytesIO()
        encrypted.decrypt(out)
        return out.getvalue()
    except Exception as e:
        raise ValueError(f'解密失败（密码错误或文件不是 OLE2 加密）: {type(e).__name__}: {e}')
