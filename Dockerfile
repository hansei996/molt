# molt —— 账本（Warden）的官方镜像
#
# ## 为什么镜像里只装账本，不装壳
#
# 账本是"记忆待在哪台机器上"的那个权威：一个进程、一个目录、一个 token，
# 装一次就不用再管。壳（Windows 游戏机 / NAS 容器）要的是它自己那台机器的
# 路径、python、容器名 —— 那些东西装进镜像等于把某台机器的细节固化进发行物。
#
# 所以：**镜像 = 账本**；壳用 `pip install molt` + `molt init`（见 docs/INSTALL.md）。
#
# ## 为什么用 COPY 而不是 pip install
#
# `pip install .` 在 3.12-slim 里会去下载构建后端（镜像里未必有 setuptools），
# 而 molt 的运行时依赖是**零** —— 直接把包目录放进 /app，用 `-m molt.cli` 跑，
# 构建过程完全不需要网络。少一步，就少一种"构建时网不通"的失败。

FROM python:3.12-slim

LABEL org.opencontainers.image.title="molt warden" \
      org.opencontainers.image.description="Molt 的账本：让你的 AI 伴侣自己换身体" \
      org.opencontainers.image.source="https://github.com/hansei996/molt" \
      org.opencontainers.image.licenses="Apache-2.0"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MOLT_ROOT=/var/lib/molt \
    MOLT_PORT=6186

WORKDIR /app

# 只要包本身。docs/、tools/、tests/ 不进镜像 —— 镜像里的东西越少，
# 出问题时能怀疑的地方就越少。
COPY molt/ /app/molt/
COPY docker/entrypoint.sh /usr/local/bin/molt-entrypoint

RUN chmod +x /usr/local/bin/molt-entrypoint \
    && groupadd --system --gid 10001 molt \
    && useradd --system --uid 10001 --gid molt --home-dir /var/lib/molt molt \
    && mkdir -p /var/lib/molt \
    && chown -R molt:molt /var/lib/molt

# 不以 root 跑：这个进程监听 6186 并写一个目录，不需要任何特权。
USER molt

VOLUME ["/var/lib/molt"]
EXPOSE 6186

# ## 健康检查
# 用 `python -c` 而不是 curl：slim 镜像里没有 curl，装一个只为探活不值。
# 判据是 /status 返回 200 —— 那是账本真的能答话，而不是"进程还在"。
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD ["/usr/local/bin/python", "-c", "import os,sys,urllib.request;t=os.environ.get('MOLT_TOKEN','');r=urllib.request.Request('http://127.0.0.1:'+os.environ.get('MOLT_PORT','6186')+'/status',headers={'X-Molt-Token':t});sys.exit(0 if urllib.request.urlopen(r,timeout=4).status==200 else 1)"]

ENTRYPOINT ["/usr/local/bin/molt-entrypoint"]
