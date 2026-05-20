"""统一 DB 连接工具：从 db_config.txt 读凭据，按 section 名取连接

用法：
    from scripts._db import connect
    conn = connect('fish-prod')   # 老库（原始数据，只读）
    conn = connect('fish-test')   # 新库（基础数据）

凭据文件：D:/小鱼AI数据/db_config.txt（INI-like 格式，[section] + key=value）
"""
import os
import configparser
import pymysql
from pathlib import Path

# 默认走仓库根目录的 db_config.txt；env XIAOYU_DB_CONFIG 可覆盖
CONFIG_PATH = Path(os.environ.get(
    'XIAOYU_DB_CONFIG',
    Path(__file__).resolve().parents[1] / 'db_config.txt'
))


def _load_config():
    cp = configparser.ConfigParser()
    cp.read(CONFIG_PATH, encoding='utf-8')
    return cp


def connect(section='fish-prod', **kwargs):
    """按 section 连数据库。kwargs 覆盖默认参数（如 charset、autocommit）。"""
    cp = _load_config()
    if section not in cp:
        raise ValueError(f'db_config.txt 中没有 [{section}] section')
    s = cp[section]
    params = dict(
        host=s['host'],
        port=int(s['port']),
        user=s['user'],
        password=s['password'],
        database=s['database'],
        charset='utf8mb4',
    )
    params.update(kwargs)
    return pymysql.connect(**params)


if __name__ == '__main__':
    # 自检：两个库都能连上
    for section in ('fish-prod', 'fish-test'):
        try:
            conn = connect(section)
            cur = conn.cursor()
            cur.execute('SELECT VERSION(), DATABASE()')
            ver, db = cur.fetchone()
            cur.execute('SHOW TABLES')
            tcnt = len(cur.fetchall())
            conn.close()
            print(f'  ✓ [{section}] {db} (MySQL {ver}, {tcnt} 张表)')
        except Exception as e:
            print(f'  ✗ [{section}] {type(e).__name__}: {e}')
