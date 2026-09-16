# 数据分析沙箱镜像
#
# 设计约束：
#   1. 依赖全部在构建期装好 —— 运行期不 pip install。容器的 --network=none
#      既是数据外泄防线，也顺带杜绝了运行期拉不确定版本的供应链风险。
#   2. 非 root 用户：与 ExecutorConfig.run_as_uid 对齐，容器内无提权空间。
#
# 构建：
#   docker build -t data-analyst-sandbox:latest src/sandbox/image
#
# ⚠️ 上线前请把基础镜像固定到 digest（现在是浮动 tag，上游换内容会导致
#    沙箱行为静默漂移）：
#   1) docker pull python:3.13-slim
#   2) docker images --digests python:3.13-slim
#   3) 把下面的 FROM 改成 FROM python:3.13-slim@sha256:<实际值>

FROM python:3.13-slim

# pip 源可在构建时覆盖，避免卡死在国内服务器上访问 pypi.org：
#   docker build --build-arg PIP_INDEX_URL=http://mirrors.tencentyun.com/pypi/simple \
#                --build-arg PIP_TRUSTED_HOST=mirrors.tencentyun.com -t data-analyst-sandbox:latest .
# 不传这两个参数时行为与原来完全一致（走 PyPI 默认源）。
ARG PIP_INDEX_URL
ARG PIP_TRUSTED_HOST
ENV PIP_INDEX_URL=${PIP_INDEX_URL}
ENV PIP_TRUSTED_HOST=${PIP_TRUSTED_HOST}

# 中文字体（fonts-noto-cjk）。端到端实测发现：没装它时 matplotlib 画中文
# 全是方块，模型得花一轮去排查字体、再把图表标签换成英文 —— 用户看到的
# 图就永远是英文。装上字体把这个坑从根上填掉。
# 换成清华源是构建加速；apt 层装完即清缓存，不进镜像层。
RUN sed -i 's|deb.debian.org|mirrors.tuna.tsinghua.edu.cn|g' /etc/apt/sources.list.d/debian.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*

# 数据分析常用套件在开发期就要全装好 —— 容器 --network=none，
# 跑起来之后装不了也查不了。
RUN python -m pip install --no-cache-dir \
        pandas==2.3.* \
        numpy==2.* \
        matplotlib==3.10.* \
        openpyxl==3.1.* \
        xlrd==2.0.* \
    && rm -rf /root/.cache/pip

# 把 matplotlib 默认字体改成 Noto Sans CJK 系列 —— 装上字体只是第一步，
# matplotlib 本身不知道。改它打包的 matplotlibrc 后所有 plt.xxx 才会自动
# 选到中文字体，模型就不用每次都 plt.rcParams['font.family'] = '...' 了。
# 顺手 axes.unicode_minus=False，否则负号会显示成方块。
# 最后清掉 fontmanager 缓存，让它下次启动时重新扫描（包含刚装的 CJK 字体）。
RUN python - <<'PYEOF'
import os, re
import matplotlib
mpl_dir = os.path.join(os.path.dirname(matplotlib.__file__), 'mpl-data')
rc_path = os.path.join(mpl_dir, 'matplotlibrc')
text = open(rc_path).read()
text = re.sub(r'^#\s*font\.family\s*:\s*sans-serif', 'font.family: sans-serif', text, flags=re.M)
text = re.sub(
    r'^#\s*font\.sans-serif\s*:[^\n]*',
    'font.sans-serif: Noto Sans CJK JP, Noto Sans CJK SC, Noto Sans CJK TC, DejaVu Sans',
    text, flags=re.M,
)
text = re.sub(r'^#\s*axes\.unicode_minus\s*:\s*True', 'axes.unicode_minus: False', text, flags=re.M)
open(rc_path, 'w').write(text)
PYEOF
RUN fc-cache -fv 2>&1 | tail -1 && rm -rf /root/.cache/matplotlib

# 与 ExecutorConfig.run_as_uid 对齐。这里只作存在性保证 ——
# 真正的降权来自 docker run 的 --user=1000。
RUN useradd --uid 1000 --create-home sandbox 2>/dev/null || true

# 容器内的工作约定：
#   /work  只读：宿主拷进来的 script.py
#   /data  只读：本次会话**实际用到**的数据文件副本
#   /out   可写：唯一允许落盘的地方（图表、结果 csv）
# / 根文件系统由 docker run --read-only 挂成只读，所以这里不必额外处理。
RUN mkdir -p /out /data && chmod 777 /out

# 不需要长期监听任何端口 —— ECharts 在前端渲染，这里只负责算数和出图
CMD ["python", "-I", "-X", "utf8=1", "--version"]
