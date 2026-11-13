# 门户运行镜像（NAS 容器化发布用）
#
# 设计取舍：
# 1) 前端产物直接使用仓库里的 static/app（由开发机 npm run build 生成），镜像内不装 node；
# 2) 只装运行期依赖 requirements.txt，不含 tests/、backups/、参考/；
# 3) config/、firmware/、tls/、.env 一律由宿主机挂载，镜像里只留空目录占位，
#    避免升级时覆盖线上飞书配置、设备注册表与快照库；
# 4) uvicorn 固定单 worker：会话、镜像缓存、限频计数、考勤内存状态都在进程内。
FROM python:3.10-slim

# PYTHONUNBUFFERED 让日志实时进 docker logs；PIP_NO_CACHE_DIR 控制镜像体积
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Shanghai \
    WEB_HOST=0.0.0.0 \
    WEB_PORT=8000

# tzdata 供 TZ 生效（门户按 Asia/Shanghai 计算日期与倒计时）
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && ln -snf "/usr/share/zoneinfo/${TZ}" /etc/localtime \
    && echo "${TZ}" > /etc/timezone \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# 运行期需要的代码与静态资源（deploy.py、tests、web 源码不进镜像）
COPY app ./app
COPY static ./static
COPY scripts ./scripts
COPY docs ./docs
COPY README.md run.sh ./

# 首启会自动生成 config/presets.json 等文件，所以这些目录必须可写
RUN mkdir -p config firmware tls backups

EXPOSE 8000

# 未登录可访问的 /login 是存活探针：200 表示应用已起
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import os,sys,urllib.request; \
p=os.environ.get('WEB_PORT','8000'); \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:'+p+'/login', timeout=4).status == 200 else 1)"

# 单 worker；--proxy-headers 让 NAS 反代后面的客户端 IP 透传（考勤网段校验依赖它）
CMD ["sh", "-c", "exec python -m uvicorn app.main:app --host 0.0.0.0 --port \"${WEB_PORT:-8000}\" --workers 1 --proxy-headers --forwarded-allow-ips \"${FORWARDED_ALLOW_IPS:-127.0.0.1}\""]
