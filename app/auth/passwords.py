"""密码散列：PBKDF2-HMAC-SHA256，随机盐，恒定时间比较。

格式：pbkdf2_sha256$<iterations>$<salt_hex>$<hash_hex>
禁止默认密码：所有账号由 emergency_ctl 显式创建。
"""
import hashlib
import hmac
import secrets

ITERATIONS = 200_000


def hash_password(password: str) -> str:
    if not isinstance(password, str) or not password:
        raise ValueError("密码不能为空")
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                                 salt, ITERATIONS)
    return f"pbkdf2_sha256${ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iters, salt_hex, hash_hex = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                                     bytes.fromhex(salt_hex), int(iters))
        return hmac.compare_digest(digest.hex(), hash_hex)
    except Exception:
        return False


def new_session_token() -> str:
    """会话令牌：32 字节随机 hex（Cookie/Authorization 携带的原文）。"""
    return secrets.token_hex(32)


def token_digest(token: str) -> str:
    """会话文件中只存令牌摘要（文件泄露不等于会话可用）。"""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
