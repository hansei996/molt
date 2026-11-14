"""临时自检脚本：验证 control.py 的 JWT 签名与配置读取。"""
import base64
import json
import sys
from pathlib import Path

sys.path.insert(0, r"C:\Users\13553\Downloads\DSH_workspace\molt")

from molt.control import (
    read_dashboard_credentials,
    sign_dashboard_jwt,
)

print("=== 1) 签一个 token 并解出来 ===")
tok = sign_dashboard_jwt("test-secret-abc", "admin", ttl=300)
parts = tok.split(".")
print("  段数:", len(parts), "(必须是 3)")
for name, seg in zip(("header", "payload", "sig"), parts):
    pad = seg + "=" * (-len(seg) % 4)
    raw = base64.urlsafe_b64decode(pad)
    if name != "sig":
        print(f"  {name}: {raw.decode()}")
    else:
        print(f"  sig: {len(raw)} bytes (SHA256=32)")

print()
print("=== 2) 同一个 secret 两次签名必须不同（iat/exp 相同则相同，这里期望相同）===")
t2 = sign_dashboard_jwt("test-secret-abc", "admin", ttl=300)
print("  与上一次相同:", t2 == tok, "（同秒同参 -> 相同是正常的）")

print()
print("=== 3) 不同 secret 签出来的 sig 必须不同 ===")
t3 = sign_dashboard_jwt("other-secret", "admin", ttl=300)
print("  sig 部分不同:", t3.split(".")[2] != tok.split(".")[2])

print()
print("=== 4) 读真实 cmd_config.json 的凭据 ===")
cfg = Path(r"G:\molt-p0\land-guard\data\cmd_config.json")
if cfg.exists():
    secret, user = read_dashboard_credentials(cfg)
    print(f"  username: {user!r}")
    print(f"  secret 长度: {len(secret)}")
    print(f"  secret 前 8 位: {secret[:8]}…")
    real = sign_dashboard_jwt(secret, user, ttl=60)
    print(f"  真实 token 段数: {len(real.split('.'))}")
else:
    print("  [SKIP] 文件不存在:", cfg)

print()
print("=== 5) 坏输入必须给出清楚的错误 ===")
for label, fn in [
    ("空 secret", lambda: sign_dashboard_jwt("", "u")),
    ("空 username", lambda: sign_dashboard_jwt("s", "")),
    ("文件不存在", lambda: read_dashboard_credentials(Path("nope.json"))),
]:
    try:
        fn()
        print(f"  {label}: [FAIL] 没抛异常")
    except Exception as e:
        print(f"  {label}: {type(e).__name__}: {str(e)[:70]}")
