"""pytest 全局配置。

做一件事：把仓库根目录加进 sys.path，让 `from src.sandbox import ...`
可直接导入 —— 这样换台机器 clone 下来就能跑测试，不需要
`pip install -e .` 也不依赖任何包管理状态。
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
