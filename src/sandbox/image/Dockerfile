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

# 数据分析常用套件在开发期就要全装好 —— 容器 --network=none，
# 跑起来之后装不了也查不了。
RUN python -m pip install --no-cache-dir \
        pandas==2.3.* \
        numpy==2.* \
        matplotlib==3.10.* \
        openpyxl==3.1.* \
        xlrd==2.0.* \
    && rm -rf /root/.cache/pip

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
