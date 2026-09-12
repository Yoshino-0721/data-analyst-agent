"""真实 Docker 集成测试 —— 桩测试覆盖不到的那一层。

桩测试能证明「我以为它会这样」，只有真跑容器才能证明「它真的这样」。
这里验的是那些**错了会造成实际损失**的行为：代码到底有没有被隔离、
超时是不是真的被杀、产物会不会丢。

默认跳过（pyproject 里 -m "not docker"），手动跑：

    pytest -m docker -v

前置：
    python scripts/check_docker.py --build
"""

from __future__ import annotations

import tempfile
import time
from pathlib import Path

import pytest

from src.sandbox.docker_executor import DockerExecutor
from src.sandbox.executor import ExecStatus, ExecutionRequest, ExecutorConfig

pytestmark = pytest.mark.docker


@pytest.fixture(scope="module")
def sandbox_image_available() -> None:
    executor = DockerExecutor()
    ok, reason = executor.available()
    if not ok:
        pytest.skip(f"沙箱环境不可用：{reason}")


@pytest.fixture()
def executor(sandbox_image_available) -> DockerExecutor:
    return DockerExecutor(ExecutorConfig(keep_recent_runs=3))


@pytest.fixture()
def workspace():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        yield root / "runs", root / "artifacts"


def make_request(runs: Path, artifacts: Path, **kwargs) -> ExecutionRequest:
    kwargs.setdefault("timeout_seconds", 30.0)
    return ExecutionRequest(work_dir=runs / "run_test", artifact_dir=artifacts, **kwargs)


# =============================================================== 基本执行


class TestRealContainerExecution:
    def test_runs_code_and_captures_stdout(self, executor, workspace):
        runs, artifacts = workspace
        result = executor.execute(
            make_request(runs, artifacts, code="print('hello from sandbox')")
        )
        assert result.status is ExecStatus.OK
        assert "hello from sandbox" in result.stdout
        assert result.exit_code == 0

    def test_chinese_output_is_readable(self, executor, workspace):
        """中文列名能不能读，决定了模型能不能修对 —— 这是硬需求。"""
        runs, artifacts = workspace
        result = executor.execute(
            make_request(runs, artifacts, code="print('销售额合计：123 元')")
        )
        assert result.status is ExecStatus.OK
        assert "销售额合计：123 元" in result.stdout

    def test_pandas_is_available(self, executor, workspace):
        """数据分析场景的基础依赖必须在镜像里，运行期装不了（无网络）。"""
        runs, artifacts = workspace
        result = executor.execute(
            make_request(
                runs,
                artifacts,
                code="import pandas as pd\nprint('pandas', pd.__version__)",
            )
        )
        assert result.status is ExecStatus.OK
        assert "pandas" in result.stdout

    def test_traceback_paths_are_stripped(self, executor, workspace):
        """给模型的 traceback 不能带着容器内路径之外的宿主痕迹。"""
        runs, artifacts = workspace
        result = executor.execute(
            make_request(runs, artifacts, code="df = {}\nprint(df['不存在的列'])")
        )
        assert result.status is ExecStatus.RUNTIME_ERROR
        assert "KeyError" in result.stderr
        payload = result.to_tool_payload()
        assert str(runs) not in str(payload)


# =============================================================== 隔离（真实生效验证）


class TestIsolationIsReal:
    """这几条是「沙箱到底有没有用」的实证，不是配置检查。

    隔离有两道门，**两道都要验**：
      1. 静态预检（precheck_code）—— 省一次容器启动，并给模型明确信号；
      2. 容器本身（--network=none / --read-only）—— 真正的防线。

    第一道门挡在前面，如果只写「正常写法」的用例，实际验到的永远是预检，
    容器那层反而没人守、坏了也不知道。所以下面每个风险点都验两段：
    先确认预检会拦，再用预检查不见的等价写法去撞容器，确认第二道门自己站得住。
    （威胁模型里写明了：黑名单只是提示层，不是安全防线。）
    """

    # ---- 网络 ----

    def test_network_blocked_at_precheck(self, executor, workspace):
        """第一道门：直白的 import socket 被预检拦下，容器根本不用起。"""
        runs, artifacts = workspace
        result = executor.execute(
            make_request(runs, artifacts, code="import socket\nprint(socket)")
        )
        assert result.status is ExecStatus.REJECTED
        assert "NETWORK WORKED" not in result.stdout

    def test_network_blocked_by_container_when_precheck_bypassed(self, executor, workspace):
        """第二道门：绕开预检，--network=none 必须自己拦得住。

        importlib 不在预检黑名单里，且模块名是拼接出来的，
        预检的字面量匹配看不见它 —— 撞到的只能是容器那层。
        """
        runs, artifacts = workspace
        result = executor.execute(
            make_request(
                runs,
                artifacts,
                code="import importlib\n"
                     "sock = importlib.import_module('soc' + 'ket')\n"
                     "sock.setdefaulttimeout(3)\n"
                     "sock.create_connection(('1.1.1.1', 80))\n"
                     "print('NETWORK WORKED')",
                timeout_seconds=20,
            )
        )
        assert result.status is ExecStatus.RUNTIME_ERROR
        assert "NETWORK WORKED" not in result.stdout

    # ---- 越权写文件 ----

    def test_outside_write_blocked_at_precheck(self, executor, workspace):
        """第一道门：挂载点之外的绝对路径，预检直接拒。"""
        runs, artifacts = workspace
        result = executor.execute(
            make_request(runs, artifacts, code="open('/evil.txt', 'w').write('x')")
        )
        assert result.status is ExecStatus.REJECTED

    def test_outside_write_blocked_by_container_when_precheck_bypassed(self, executor, workspace):
        """第二道门：路径拼出来预检就看不见了，得靠 --read-only 兜住。"""
        runs, artifacts = workspace
        result = executor.execute(
            make_request(
                runs,
                artifacts,
                code="p = '/ev' + 'il.txt'\n"
                     "open(p, 'w').write('x')\n"
                     "print('WRITE WORKED')",
                timeout_seconds=20,
            )
        )
        assert result.status is ExecStatus.RUNTIME_ERROR
        assert "WRITE WORKED" not in result.stdout

    def test_is_not_root(self, executor, workspace):
        runs, artifacts = workspace
        result = executor.execute(
            make_request(runs, artifacts, code="import os\nprint('uid', os.getuid())")
        )
        assert result.status is ExecStatus.OK
        assert "uid 0" not in result.stdout

    def test_data_mount_is_read_only(self, executor, workspace):
        """挂载的数据目录不可写 —— 原始数据不能被代码改掉。"""
        runs, artifacts = workspace
        result = executor.execute(
            make_request(
                runs,
                artifacts,
                code="open('/data/tampered.txt', 'w').write('x')",
                timeout_seconds=20,
            )
        )
        assert result.status is ExecStatus.RUNTIME_ERROR


class TestMinimalDataMountingInContainer:
    def test_only_requested_file_is_visible(self, executor, workspace, tmp_path):
        """最小权限：只挂载本次用到的文件，其他文件在容器里根本不存在。"""
        runs, artifacts = workspace
        used = tmp_path / "used.csv"
        unused = tmp_path / "confidential.csv"
        used.write_text("a,b\n1,2\n", encoding="utf-8")
        unused.write_text("salary\n999\n", encoding="utf-8")

        result = executor.execute(
            make_request(
                runs,
                artifacts,
                code="import os\nprint(sorted(os.listdir('/data')))",
                data_files=[used],
            )
        )
        assert result.status is ExecStatus.OK
        assert "used.csv" in result.stdout
        assert "confidential.csv" not in result.stdout

    def test_mounted_data_is_readable(self, executor, workspace, tmp_path):
        runs, artifacts = workspace
        source = tmp_path / "销售.csv"
        source.write_text("city,amount\nBeijing,120\n", encoding="utf-8")

        result = executor.execute(
            make_request(
                runs,
                artifacts,
                code="print(open('/data/销售.csv', encoding='utf-8').read())",
                data_files=[source],
            )
        )
        assert result.status is ExecStatus.OK
        assert "Beijing,120" in result.stdout


# =============================================================== 超时与资源


class TestGuardingInRealContainer:
    def test_timeout_kills_container(self, executor, workspace):
        """看门狗必须在预算内收尾 —— 30 秒的睡眠不能拖住服务 30 秒。"""
        runs, artifacts = workspace
        started = time.monotonic()
        result = executor.execute(
            make_request(
                runs,
                artifacts,
                code="import time\nprint('started')\ntime.sleep(60)",
                timeout_seconds=3.0,
            )
        )
        elapsed = time.monotonic() - started

        assert result.status is ExecStatus.TIMEOUT
        assert elapsed < 25, f"超时没在预算内收尾：{elapsed:.1f}s"
        # 被强杀前已经写出的内容要能捞回来
        assert "started" in result.stdout
        assert "超时" in result.error_summary

    def test_no_orphan_containers_left_behind(self, executor, workspace):
        """超时强杀后不能留下跑着的容器 —— 那会持续占 CPU/内存。"""
        import subprocess

        runs, artifacts = workspace
        executor.execute(
            make_request(
                runs, artifacts, code="import time\ntime.sleep(60)", timeout_seconds=3.0
            )
        )
        listing = subprocess.run(
            ["docker", "ps", "--filter", "name=daa-run", "--format", "{{.Names}}"],
            capture_output=True,
            text=True,
            check=False,
        )
        assert listing.stdout.strip() == "", f"有残留容器：{listing.stdout}"

    def test_oom_is_classified(self, executor, workspace):
        """OOM 要能被认出来 —— 认错了模型就会往错误方向改。"""
        runs, artifacts = workspace
        result = executor.execute(
            make_request(
                runs,
                artifacts,
                code="x = bytearray(900 * 1024 * 1024)\nprint('allocated')",
                timeout_seconds=60,
            )
        )
        assert result.status is ExecStatus.OOM
        assert "内存" in result.hint


# =============================================================== 产物


class TestArtifactsInRealContainer:
    def test_artifact_is_copied_out(self, executor, workspace):
        runs, artifacts = workspace
        result = executor.execute(
            make_request(
                runs,
                artifacts,
                code="import pathlib\n"
                     "pathlib.Path('/out/chart.csv').write_text('a,b\\n1,2', encoding='utf-8')",
            )
        )
        assert result.status is ExecStatus.OK
        assert [p.name for p in result.artifacts] == ["chart.csv"]
        # 运行目录被回收后产物仍在 —— 这才是「拷出」的意义
        assert result.artifacts[0].exists()
        assert "a,b" in result.artifacts[0].read_text(encoding="utf-8")

    def test_plot_can_be_generated(self, executor, workspace):
        """真实场景：画一张图出来。matplotlib 缓存目录必须是可写的。"""
        runs, artifacts = workspace
        result = executor.execute(
            make_request(
                runs,
                artifacts,
                code="import matplotlib\n"
                     "matplotlib.use('Agg')\n"
                     "import matplotlib.pyplot as plt\n"
                     "plt.plot([1, 2, 3], [1, 4, 9])\n"
                     "plt.savefig('/out/chart.png')\n"
                     "print('plotted')",
                timeout_seconds=60,
            )
        )
        assert result.status is ExecStatus.OK
        assert any(p.name == "chart.png" for p in result.artifacts)
