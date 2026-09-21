"""路径配置: 数据与结果目录可通过环境变量覆盖, 便于 Docker 卷挂载。

  ZTP_DATA_DIR    K线缓存 (默认 ./data_cache)
  ZTP_RESULTS_DIR 日志/交易记录/状态 (默认 ./results)
"""

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def data_dir() -> Path:
    p = Path(os.environ.get("ZTP_DATA_DIR", ROOT / "data_cache"))
    p.mkdir(parents=True, exist_ok=True)
    return p


def results_dir() -> Path:
    p = Path(os.environ.get("ZTP_RESULTS_DIR", ROOT / "results"))
    p.mkdir(parents=True, exist_ok=True)
    return p


def reference_dir() -> Path:
    return ROOT / "data" / "reference"
