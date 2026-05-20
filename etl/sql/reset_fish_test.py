"""执行 init_fish_test.sql 重置 fish-test 库。

用法：
    python etl/sql/reset_fish_test.py

读 init_fish_test.sql 全文，拆分成单条 SQL 语句逐条执行。
DROP+CREATE 整库（用户已确认可清空）。
"""
import sys
import re
sys.path.insert(0, 'D:/小鱼AI数据')
from pathlib import Path
from scripts._db import connect

SQL_PATH = Path(__file__).parent / 'init_fish_test.sql'


def split_statements(sql_text):
    """按 ; 拆分 SQL，过滤注释行和空语句。
    状态机版：跳过单引号 string literal 内的 ; 不拆。
    """
    # 去掉行注释 -- xxx（在引号外才生效，简化处理：先按行去 --）
    cleaned_lines = []
    in_string = False
    for line in sql_text.split('\n'):
        # 找 -- 但要排除引号内
        out = []
        i = 0
        local_in_str = in_string
        while i < len(line):
            ch = line[i]
            if ch == "'" and (i == 0 or line[i-1] != '\\'):
                local_in_str = not local_in_str
                out.append(ch)
            elif not local_in_str and ch == '-' and i + 1 < len(line) and line[i+1] == '-':
                break  # 行注释开始
            else:
                out.append(ch)
            i += 1
        in_string = local_in_str  # 跨行字符串状态延续
        cleaned_lines.append(''.join(out))
    cleaned = '\n'.join(cleaned_lines)

    # 按 ; 拆，跳过引号内的 ;
    parts = []
    buf = []
    in_str = False
    for ch in cleaned:
        if ch == "'":
            in_str = not in_str
            buf.append(ch)
        elif ch == ';' and not in_str:
            stmt = ''.join(buf).strip()
            if stmt:
                parts.append(stmt)
            buf = []
        else:
            buf.append(ch)
    tail = ''.join(buf).strip()
    if tail:
        parts.append(tail)
    return parts


def main():
    sql_text = SQL_PATH.read_text(encoding='utf-8')
    statements = split_statements(sql_text)
    print(f'共 {len(statements)} 条语句')

    conn = connect('fish-test')
    cur = conn.cursor()

    for i, stmt in enumerate(statements, 1):
        first_word = stmt.split()[0].upper() if stmt.split() else ''
        # 截短显示
        preview = ' '.join(stmt.split())[:80]
        try:
            cur.execute(stmt)
            # SELECT 类语句拿结果
            if first_word == 'SELECT':
                rows = cur.fetchall()
                print(f'  [{i:3d}] {first_word:10s} {preview}')
                for r in rows:
                    print(f'        → {r}')
            else:
                print(f'  [{i:3d}] {first_word:10s} {preview}')
        except Exception as e:
            print(f'  [{i:3d}] [FAIL] {first_word:10s} {preview}')
            print(f'        {type(e).__name__}: {e}')
            conn.rollback()
            raise

    conn.commit()
    conn.close()
    print('\n✓ fish-test 库重置完成')


if __name__ == '__main__':
    main()
