#!/usr/bin/env python3
"""运行 tests/ 下全部单元测试 (无需 pytest)。用法: python run_tests.py [过滤子串]"""

import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent
flt = sys.argv[1] if len(sys.argv) > 1 else ""
tests = sorted(p for p in (ROOT / "tests").glob("test_*.py") if flt in p.name)

fail = []
for t in tests:
    print(f"\n===== {t.name} =====", flush=True)
    r = subprocess.run([sys.executable, str(t)], cwd=ROOT, timeout=600)
    if r.returncode != 0:
        fail.append(t.name)

print("\n" + "=" * 50)
if fail:
    print(f"失败: {', '.join(fail)}")
    sys.exit(1)
print(f"全部通过 ({len(tests)} 个测试文件)")
