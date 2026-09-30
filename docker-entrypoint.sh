#!/bin/sh
# 容器入口：初始化依赖（构建时已安装）与 SQLite 数据库后启动服务。
set -e

mkdir -p "$(dirname "$DATABASE_PATH")"

if command -v python >/dev/null 2>&1; then
  PY=python
else
  PY=python3
fi

# init_db 幂等：表已存在时不会清除数据
"$PY" - <<'EOF'
import os
from app.db import init_db
init_db()
print("数据库已就绪:", os.environ.get("DATABASE_PATH", "/data/ems.db"))
EOF

exec "$@"
