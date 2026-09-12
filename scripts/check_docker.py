"""Docker 环境验证 —— 重启后跑这一条就能确认沙箱是否真的可用。

按「由轻到重」四步走，任何一步失败都给出**可行动**的原因，
而不是甩一堆看不懂的 daemon 报错：

    ① docker 命令在不在 PATH
    ② daemon 连不连得上（WSL2 后端是否已生效）
    ③ 沙箱镜像在不在（可顺手构建）
    ④ 真跑几个容器，验证隔离里最容易配错的几项是否真的生效

运行：
    python scripts/check_docker.py
    python scripts/check_docker.py --build   # 镜像不存在时顺手构建
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
IMAGE = "data-analyst-sandbox:latest"
DOCKERFILE_DIR = ROOT / "src" / "sandbox" / "image"

# 每个探针：说明 / 容器内代码 / 期望「失败还是成功」
# 隔离项要**失败**才算对 —— 能写出文件、能联网，说明防护没生效
PROBES = [
    (
        "无网络（--network=none）",
        ["--network=none"],
        "import socket\n"
        "socket.setdefaulttimeout(3)\n"
        "socket.create_connection(('1.1.1.1', 80))\n"
        "print('UNEXPECTED: 竟然能联网')",
        False,
    ),
    (
        "根文件系统只读（--read-only）",
        ["--read-only"],
        "open('/evil.txt', 'w').write('x')\nprint('UNEXPECTED: 竟然能写系统目录')",
        False,
    ),
    (
        "非 root（--user=1000）",
        ["--user=1000"],
        "import os\nprint('uid =', os.getuid())\nassert os.getuid() != 0",
        True,
    ),
    (
        "中文输出可读（-X utf8=1）",
        [],
        "print('销售额：120 元')",
        True,
    ),
    (
        "中文字体可用（matplotlib）",
        [],
        "import matplotlib.font_manager as fm\n"
        "cjk = [f.name for f in fm.fontManager.ttflist if 'CJK' in f.name]\n"
        "assert cjk, '没有 CJK 字体，图表里的中文会变方块'\n"
        "import matplotlib\n"
        "matplotlib.rcParams['font.sans-serif'] = ['Noto Sans CJK JP'] + matplotlib.rcParams['font.sans-serif']\n"
        "import matplotlib.pyplot as plt\n"
        "fig, ax = plt.subplots()\n"
        "ax.set_title('各地区销售额'); ax.set_xlabel('地区'); ax.set_ylabel('销售额')\n"
        "fig.savefig('/tmp/chart.png')\n"
        "print('CJK fonts:', len(cjk))",
        True,
    ),
]


def run(args: list[str], *, timeout: int = 180) -> tuple[int, str, str]:
    """跑一条命令。**不抛异常** —— 找不到命令、超时都返回非零码 + 原因。

    这个脚本存在的意义就是「优雅地报告 Docker 不可用」，
    它自己被 FileNotFoundError 砸崩就太讽刺了。
    """
    try:
        process = subprocess.run(
            args, capture_output=True, text=True, timeout=timeout, check=False
        )
    except FileNotFoundError:
        return 127, "", f"找不到命令：{args[0]}"
    except subprocess.TimeoutExpired:
        return 124, "", f"命令超时（{timeout}s）：{' '.join(args[:3])}"
    return process.returncode, process.stdout.strip(), process.stderr.strip()


def main() -> int:
    print("=" * 66)
    print("Docker 环境验证")
    print("=" * 66)

    # ① CLI
    code, out, _ = run(["docker", "--version"], timeout=60)
    if code != 0:
        print("\n[✗] docker 命令不可用")
        print("    刚装完 Docker Desktop 的话：需要**重启**一次，")
        print("    WSL2 后端与 PATH 才会生效。重启后再跑这个脚本。")
        return 1
    print(f"\n[✓] {out}")

    # ② daemon
    code, out, err = run(["docker", "info", "--format", "{{.ServerVersion}}"], timeout=120)
    if code != 0:
        print("\n[✗] 连不上 Docker daemon —— Docker Desktop 还没启动")
        print(f"    {err[:300]}")
        print("    处理：启动 Docker Desktop，等托盘图标不再转圈（约 1 分钟）再试。")
        return 1
    print(f"[✓] daemon 已就绪（Server {out}）")

    # ③ 镜像
    code, _, _ = run(["docker", "image", "inspect", IMAGE])
    if code != 0:
        print(f"\n[!] 沙箱镜像 {IMAGE} 不存在")
        if "--build" not in sys.argv:
            print(f"    构建：docker build -t {IMAGE} {DOCKERFILE_DIR}")
            print("    或带 --build 重跑本脚本，我来构建。")
            return 1
        print("    正在构建（首次拉依赖，约 1-3 分钟）...")
        code, out, err = run(
            ["docker", "build", "-t", IMAGE, str(DOCKERFILE_DIR)], timeout=1200
        )
        if code != 0:
            print(f"[✗] 构建失败：\n{err[-1500:]}")
            return 1
        print("[✓] 镜像构建完成")
    else:
        print(f"[✓] 沙箱镜像已存在（{IMAGE}）")

    # ④ 隔离探针
    print("\n[ ] 验证容器隔离（隔离项要**失败**才说明防护生效）")
    all_ok = True
    for label, flags, code_text, should_succeed in PROBES:
        # 无网络是通用底线：每个探针都保证不出网
        effective_flags = list(flags)
        if "--network=none" not in effective_flags:
            effective_flags.append("--network=none")

        code, out, err = run(
            [
                "docker", "run", "--rm", *effective_flags,
                "--security-opt=no-new-privileges", "--pids-limit=64",
                IMAGE, "python", "-I", "-X", "utf8=1", "-u", "-c", code_text,
            ],
            timeout=180,
        )
        got_success = code == 0
        ok = got_success == should_succeed
        all_ok = all_ok and ok
        mark = "✓" if ok else "✗"
        print(f"    [{mark}] {label} → 退出码 {code}")
        if not ok:
            print(
                f"        期望{'成功' if should_succeed else '失败'}，"
                f"实际{'成功' if got_success else '失败'}"
            )
            print(f"        {(out or err)[:300]}")

    print("\n" + "=" * 66)
    if all_ok:
        print("全部通过。真实集成测试：pytest -m docker -v")
    else:
        print("有探针未通过，请检查上面带 ✗ 的项。")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
