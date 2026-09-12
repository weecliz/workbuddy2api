FROM python:3.12-slim

WORKDIR /app

# 本文件在 deploy/ 下，构建时 context 必须是仓库根目录：
#   docker build -f deploy/Dockerfile -t workbuddy2api .
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 内核模块（converter 及其适配器）已归拢到 core/ 包
COPY core/ ./core/

EXPOSE 8787

# 用 -m 而不是直接跑脚本：core/converter.py 内部是相对导入，
# 直接执行会因缺少包上下文而 ImportError。
CMD ["python3", "-m", "core.converter", "--host", "0.0.0.0", "--port", "8787", "--skip-check"]
