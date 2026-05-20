"""金额字段表达式 mini parser

支持语法：
  - 列名引用：用双引号包裹，如 `"总工资（元）"`、`"劳务费用等（元）"`
  - 数字常量：整数 / 小数，如 `0.85`、`100`
  - 二元运算：`+` `-` `*` `/`
  - 一元负号：`-X`
  - 括号：`( )`

示例：
  - `"总工资（元）" + "劳务费用等（元）"`
  - `("实发" - "扣款") * 0.85`
  - `"金额" + 100`

裸列名（不含任何双引号 + 不含 + - * / ( ) 任一）→ 视为单列引用，老规则不破坏。

不用 eval，自己写 tokenizer + 递归下降 parser，安全。
"""
import re

_TOKEN_RE = re.compile(
    r'\s*(?:'
    r'(?P<COL>"[^"]*")'                  # 列名 "any chars"
    r'|(?P<NUM>\d+(?:\.\d+)?)'           # 数字
    r'|(?P<OP>[+\-*/()])'                # 运算符 / 括号
    r')\s*'
)


def _tokenize(s):
    tokens = []
    pos = 0
    while pos < len(s):
        if s[pos].isspace():
            pos += 1
            continue
        m = _TOKEN_RE.match(s, pos)
        if not m or m.start() != pos:
            raise ValueError(f'expr 语法错误 @ pos={pos}: {s!r}')
        if m.group('COL') is not None:
            tokens.append(('COL', m.group('COL')[1:-1]))
        elif m.group('NUM') is not None:
            tokens.append(('NUM', float(m.group('NUM'))))
        elif m.group('OP') is not None:
            tokens.append(('OP', m.group('OP')))
        pos = m.end()
    return tokens


def _safe_div(a, b):
    return a / b if b != 0 else 0.0


_BINOPS = {
    '+': lambda a, b: a + b,
    '-': lambda a, b: a - b,
    '*': lambda a, b: a * b,
    '/': _safe_div,
}


def _parse_expr(tokens, pos):
    """加减层（最低优先级）"""
    left, pos = _parse_term(tokens, pos)
    while pos < len(tokens) and tokens[pos] in (('OP', '+'), ('OP', '-')):
        op = tokens[pos][1]
        pos += 1
        right, pos = _parse_term(tokens, pos)
        l, r = left, right
        fn = _BINOPS[op]
        left = (lambda env, l=l, r=r, fn=fn: fn(l(env), r(env)))
    return left, pos


def _parse_term(tokens, pos):
    """乘除层"""
    left, pos = _parse_factor(tokens, pos)
    while pos < len(tokens) and tokens[pos] in (('OP', '*'), ('OP', '/')):
        op = tokens[pos][1]
        pos += 1
        right, pos = _parse_factor(tokens, pos)
        l, r = left, right
        fn = _BINOPS[op]
        left = (lambda env, l=l, r=r, fn=fn: fn(l(env), r(env)))
    return left, pos


def _parse_factor(tokens, pos):
    """数字 / 列名 / 括号 / 一元负号"""
    if pos >= len(tokens):
        raise ValueError('expr 意外结束')
    t = tokens[pos]
    if t == ('OP', '-'):
        inner, pos = _parse_factor(tokens, pos + 1)
        return (lambda env, f=inner: -f(env)), pos
    if t == ('OP', '+'):
        return _parse_factor(tokens, pos + 1)
    if t == ('OP', '('):
        inner, pos = _parse_expr(tokens, pos + 1)
        if pos >= len(tokens) or tokens[pos] != ('OP', ')'):
            raise ValueError("expr 缺 ')'")
        return inner, pos + 1
    if t[0] == 'NUM':
        v = t[1]
        return (lambda env, v=v: v), pos + 1
    if t[0] == 'COL':
        col = t[1]
        return (lambda env, c=col: float(env.get(c) or 0)), pos + 1
    raise ValueError(f'unexpected token {t}')


def looks_like_expr(s):
    """是否走表达式分支：含双引号视为表达式（明确 opt-in）"""
    if not isinstance(s, str):
        return False
    return '"' in s


def compile_expr(s):
    """编译表达式 → (callable(env_dict), referenced_col_names_list)

    env_dict: {col_name: number}; 未在 env 里的列按 0 处理（safe_div 同样处理）
    抛 ValueError 表示语法错误（调用方应回退到单列引用 / 跳过）
    """
    s = (s or '').strip()
    if not s:
        raise ValueError('expr 空字符串')
    tokens = _tokenize(s)
    if not tokens:
        raise ValueError('expr 无 token')
    referenced = []
    seen = set()
    for t in tokens:
        if t[0] == 'COL' and t[1] not in seen:
            seen.add(t[1])
            referenced.append(t[1])
    fn, pos = _parse_expr(tokens, 0)
    if pos < len(tokens):
        raise ValueError(f'expr 解析后有剩余 token: {tokens[pos:]}')
    return fn, referenced
