FROM python:3.11-slim

# 避免 pyc 与缓冲，容器内运行更可观测
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DATABASE_PATH=/data/ems.db \
    PORT=8000

WORKDIR /app

# 先复制依赖清单并安装（利用层缓存）
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# 复制应用代码
COPY app/ ./app/

# 数据卷：SQLite 数据库放在独立挂载点
RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8000

# 容器启动时自动初始化数据库（表已存在则跳过），再启动 Web
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh
ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["gunicorn", "app.wsgi:app", "--bind", "0.0.0.0:8000", "--workers", "2", "--threads", "4"]
