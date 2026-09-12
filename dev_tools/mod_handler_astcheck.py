"""
mod_handler_astcheck — 静态扫描「像 stdlib 模块名、被当模块使用、却从未 import」。

背景（第七轮审查 N7-1/N7-3）：mod_handler_doctor.py 的 --apply 分支用了
`json.load` 但文件从未 `import json`，从引入起就是坏的 —— 读三轮代码都没发现，
实跑才炸。这类「未定义名」可以在**不运行**的前提下用 AST 抓出来。

方法（报告给的启发式，刻意保持简单）：
  1. 收集文件里 import 进来的名字（Import 的根名/别名；ImportFrom 的别名或原名）；
  2. 收集所有「被赋值/定义」的名字（Name-Store、函数/类定义、参数、导入目标等）；
  3. 遍历 `X.yyy`（Attribute 链根）与 `X(...)` 这类用法：若 X 在常见 stdlib 候选名单里、
     既没 import 也没赋值 -> 报告。

已知局限（都是刻意取舍）：
  - 带 `from x import *` 的文件直接跳过：* 提供的名字无法静态确定，硬扫会误报
    （上游的 dev_tools/item_statistics.py 就是这样，别对上游文件报错）；
  - 只对 stdlib 候选名单报警，不追踪本地名字 —— 本地名错了 flake8/IDE 会比这更早发现；
  - 不做作用域分析：把「局部赋值」也算作已定义，宁可漏报也不误报。
"""
import ast
import glob
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 常见 stdlib 模块名（只收 mod_handler 体系实际会碰到的，名单保持克制，
# 避免和本地变量/常量撞名造成误报）
STDLIB_CANDIDATES = {
    'argparse', 'ast', 'base64', 'collections', 'copy', 'datetime', 'functools',
    'glob', 'hashlib', 'http', 'io', 'itertools', 'json', 'logging', 'math',
    'os', 'random', 're', 'shutil', 'socket', 'statistics', 'struct',
    'subprocess', 'sys', 'tempfile', 'threading', 'time', 'traceback', 'types',
    'unittest', 'urllib', 'xml',
}

# 扫描目标：我们自己写的文件（mod_handler 包 + dev_tools 的 mod_* 脚本）
TARGETS = (sorted(glob.glob(os.path.join(ROOT, 'module', 'mod_handler', '*.py')))
           + sorted(glob.glob(os.path.join(ROOT, 'dev_tools', 'mod_handler*.py')))
           + [os.path.join(ROOT, 'dev_tools', 'mod_discover.py')])


def scan_file(path):
    """返回 [(line, col, name, snippet)] —— 用到但未导入的 stdlib 名字列表。"""
    with open(path, encoding='utf-8') as f:
        src = f.read()
    tree = ast.parse(src, filename=path)

    imported = set()
    wildcard = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported.add((alias.asname or alias.name).split('.')[0])
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name == '*':
                    wildcard = True
                else:
                    imported.add(alias.asname or alias.name)

    assigned = set(imported)
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            assigned.add(node.id)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            assigned.add(node.name)
        elif isinstance(node, ast.arg):
            assigned.add(node.arg)

    problems = []
    for node in ast.walk(tree):
        root = None
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            root = node.value.id
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            root = node.id
        if root is None:
            continue
        if root in STDLIB_CANDIDATES and root not in assigned:
            problems.append((node.lineno, node.col_offset, root))
    return problems, wildcard


def main():
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass

    print(f'AST 静态扫描（stdlib 名字未导入）—— 目标 {len(TARGETS)} 个文件')
    problems = []
    skipped = []
    for path in TARGETS:
        try:
            found, wildcard = scan_file(path)
        except SyntaxError as e:
            problems.append((0, 0, path, f'语法错误: {e}'))
            continue
        if wildcard:
            skipped.append(os.path.basename(path))
        for line, col, name in found:
            rel = os.path.relpath(path, ROOT)
            problems.append((line, col, name, f'{rel}:{line}:{col} 用了 stdlib `{name}` 但从未 import'))

    if skipped:
        print(f'  跳过（含通配导入）: {", ".join(skipped)}')
    if problems:
        print(f'\n  发现 {len(problems)} 处：')
        for p in problems:
            print(f'  [FAIL] {p}')
        print(f'\n===== AST 静态扫描: {len(problems)} 处未导入名 =====')
        return 1
    print('\n===== AST 静态扫描: 全部通过 =====')
    return 0


if __name__ == '__main__':
    sys.exit(main())
