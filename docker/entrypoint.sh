#!/bin/sh
# molt 账本容器的入口。
#
# 这个脚本只做一件事：**没有 token 就不启动**。
#
# `molt serve` 在不给 `--token` 时会随机生成一个并只打印一次 —— 那对一个
# 前台命令是对的（人就坐在那儿看着），对容器是灾难：token 打在 `docker logs`
# 里，谁都能 `docker logs` 一下拿到；重启一次就换一个，所有壳全部 401，
# 而症状是"连不上账本"，看起来像网络问题。
#
# 所以这里把"没配 token"从"随机生成"改成"拒绝启动并说清楚怎么配"。
# 失败要失败在启动那一刻，而不是失败在第一次迁徙的那一秒。

set -eu

if [ -z "${MOLT_TOKEN:-}" ]; then
    cat >&2 <<'EOF'
molt: 没有设置 MOLT_TOKEN，账本不敢启动。

  这个 token 是壳与账本之间的唯一凭据：它能换租约、能确认谁取走了快照。
  容器里没有"随机生成一个"这个选项 —— 那样每次重启都会换，所有壳一起 401。

  生成一个并记住它（壳那边要用同一个）：
      MOLT_TOKEN="$(python3 -c 'import secrets;print(secrets.token_urlsafe(24))')"

  或者：先在你自己的机器上跑 `molt init --role warden --root <目录>`，
  它会生成 <目录>/warden.token，把这个文件的内容填进 MOLT_TOKEN。

  跑起来的样子：
      docker run -d --name molt-warden -p 6186:6186 \
        -v molt-data:/var/lib/molt \
        -e MOLT_TOKEN="$MOLT_TOKEN" \
        ghcr.io/hansei996/molt:latest
EOF
    exit 78   # EX_CONFIG
fi

if [ "${MOLT_TOKEN}" = "change-me" ]; then
    echo "molt: MOLT_TOKEN 还是占位符 change-me，换一个真实的再启动。" >&2
    exit 78
fi

# 传了别的参数就照办（`molt ...` 的命令行原样生效），
# 什么都没传才是标准账本。
if [ "$#" -gt 0 ]; then
    exec python -m molt.cli "$@"
fi

echo "molt: 账本启动  root=${MOLT_ROOT}  port=${MOLT_PORT}" >&2
exec python -m molt.cli serve \
    --root "${MOLT_ROOT}" \
    --host 0.0.0.0 \
    --port "${MOLT_PORT}" \
    --token "${MOLT_TOKEN}"
