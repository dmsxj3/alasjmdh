"""
mod_handler 一键自检 —— 依次跑策略、prefs、ui、overlay 四个后端自检与端到端。

用法（项目根目录）：
    toolkit\\python.exe dev_tools/mod_handler_test.py
或：
    python dev_tools/mod_handler_test.py

全部不需要模拟器、不需要游戏，失败会以非 0 退出码结束。
真机相关的验证请用 dev_tools/mod_handler_doctor.py。
"""
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HERE = os.path.dirname(os.path.abspath(__file__))

SUITES = [
    ('策略 / 状态机', 'mod_handler_selftest.py'),
    ('prefs 后端  ', 'mod_prefs_selftest.py'),
    ('ui 后端     ', 'mod_ui_selftest.py'),
    ('overlay 后端', 'mod_overlay_selftest.py'),
    ('端到端      ', 'mod_handler_e2e.py'),
    ('AST 静态扫描', 'mod_handler_astcheck.py'),
]


def find_python():
    """优先用项目自带的 3.7 运行时，保证与 ALAS 本身一致。"""
    for name in ('python.exe', 'python'):
        candidate = os.path.join(ROOT, 'toolkit', name)
        if os.path.exists(candidate):
            return candidate
    return sys.executable


def main():
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass

    python = find_python()
    print(f'python  : {python}')
    print(f'project : {ROOT}')

    results = []
    for title, script in SUITES:
        path = os.path.join(HERE, script)
        print(f'\n{"=" * 78}\n  {title}  ({script})\n{"=" * 78}')
        proc = subprocess.run([python, path], cwd=ROOT)
        results.append((title, proc.returncode))

    print(f'\n{"=" * 78}\n  汇总\n{"=" * 78}')
    failed = 0
    for title, code in results:
        status = 'OK  ' if code == 0 else 'FAIL'
        if code != 0:
            failed += 1
        print(f'  [{status}] {title}  (exit={code})')
    print(f'\n{"全部通过" if not failed else str(failed) + " 个套件失败"}')
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
