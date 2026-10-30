"""用户认证服务"""
import logging
import os
import hmac
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional, Union
from uuid import uuid4
from pwdlib import PasswordHash
from pwdlib.hashers.argon2 import Argon2Hasher
from pwdlib.hashers.bcrypt import BcryptHasher
from jose import JWTError, jwt  # type: ignore
from fastapi import HTTPException, status
from sqlalchemy.orm import Session
from src.core.redis_client import RedisError, get_redis_client
from src.core.security_preflight import get_secret_key, is_production
from src.db.models.user import User
from src.schemas.user_schemas import TokenData

logger = logging.getLogger(__name__)

# 新密码使用 Argon2；BcryptHasher 只用于兼容已有 bcrypt 密码。
password_hash = PasswordHash((Argon2Hasher(), BcryptHasher()))

# JWT配置
#
# 密钥不再在导入期固化为模块常量：此前的写法带有一个写死在源码里的兜底值，
# 部署时漏配 SECRET_KEY 也能正常启动，等于用一把公开的钥匙签发所有令牌。
# 现在统一由 security_preflight 解析——生产环境缺失会在启动自检时被拒绝，
# 开发环境则退回到进程内一次性随机密钥。
ALGORITHM = "HS256"

# 默认 2 小时。长效令牌一旦泄露，影响窗口过长（历史默认是 30 天）。
# 正常用户由续期凭条（refresh token）保持登录，不会频繁看到重新登录。
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", 120))

# 续期凭条有效期，决定"多久不用就需要重新登录"。
REFRESH_TOKEN_EXPIRE_MINUTES = int(os.getenv("REFRESH_TOKEN_EXPIRE_MINUTES", 30 * 24 * 60))

# 同一枚短信验证码允许的最大尝试次数，防止六位数字被穷举。
SMS_CODE_MAX_ATTEMPTS = int(os.getenv("SMS_CODE_MAX_ATTEMPTS", 5))

class AuthService:
    """认证服务类"""
    
    @staticmethod
    def verify_password(plain_password: str, hashed_password: str) -> bool:
        """验证密码"""
        return password_hash.verify(plain_password, hashed_password)
    
    @staticmethod
    def get_password_hash(password: str) -> str:
        """获取密码哈希"""
        return password_hash.hash(password)
    
    @staticmethod
    def _encode_token(data: dict, token_type: str, expires_delta: timedelta) -> str:
        """签发一枚带编号和类型的令牌。

        ``jti`` 是这枚令牌的唯一编号，登出或账号出事时据此挂失；
        ``type`` 区分访问令牌与续期凭条，防止拿续期凭条直接访问接口。
        """
        now = datetime.now(timezone.utc)
        to_encode = data.copy()
        to_encode.update({
            "exp": now + expires_delta,
            "iat": now,
            "jti": uuid4().hex,
            "type": token_type,
        })
        return jwt.encode(to_encode, get_secret_key(), algorithm=ALGORITHM)

    @staticmethod
    def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
        """创建访问令牌（默认 2 小时）"""
        return AuthService._encode_token(
            data,
            "access",
            expires_delta or timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES),
        )

    @staticmethod
    def create_refresh_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
        """创建续期凭条。

        访问令牌收敛到小时级后，用它在后台静默换取新的访问令牌，
        使用者不会频繁被要求重新登录。
        """
        return AuthService._encode_token(
            data,
            "refresh",
            expires_delta or timedelta(minutes=REFRESH_TOKEN_EXPIRE_MINUTES),
        )

    @staticmethod
    def _revocation_key(jti: str) -> str:
        return f"auth:token:revoked:{jti}"

    @staticmethod
    def is_token_revoked(jti: str) -> bool:
        """该编号的令牌是否已被挂失。

        缓存不可用时按"未挂失"处理并记录错误日志：否则一次 Redis 抖动
        会让全站登录失效。代价是挂失名单在缓存故障期间不生效，
        因此这条日志需要接入告警。
        """
        if not jti:
            return False
        try:
            return bool(get_redis_client().exists(AuthService._revocation_key(jti)))
        except RedisError:
            logger.error("令牌挂失名单不可用，本次按未挂失放行；请检查 Redis", exc_info=True)
            return False

    @staticmethod
    def revoke_token(token: str) -> bool:
        """挂失一枚令牌，使其在剩余有效期内不再被接受。"""
        try:
            payload = jwt.decode(
                token,
                get_secret_key(),
                algorithms=[ALGORITHM],
                options={"verify_exp": False},
            )
        except JWTError:
            return False

        jti = payload.get("jti")
        if not jti:
            # 自检模块启用前签发的旧令牌没有编号，无法单独挂失。
            return False

        expires_at = payload.get("exp")
        remaining = 60
        if isinstance(expires_at, (int, float)):
            remaining = int(expires_at - datetime.now(timezone.utc).timestamp())
        if remaining <= 0:
            return True  # 已过期，无需再挂失

        try:
            get_redis_client().setex(AuthService._revocation_key(jti), remaining, "1")
            return True
        except RedisError:
            logger.error("写入令牌挂失名单失败；该令牌在过期前仍然有效", exc_info=True)
            return False

    @staticmethod
    def verify_token(token: str, expected_type: str = "access") -> TokenData:
        """验证令牌：签名、有效期、类型、是否已挂失，缺一不可。"""
        try:
            payload = jwt.decode(token, get_secret_key(), algorithms=[ALGORITHM])
        except JWTError as e:
            if "expired" in str(e).lower():
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="令牌已过期",
                    headers={"WWW-Authenticate": "Bearer"},
                )
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="无效的认证令牌",
                headers={"WWW-Authenticate": "Bearer"},
            )

        invalid = HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="无效的认证令牌",
            headers={"WWW-Authenticate": "Bearer"},
        )

        user_id: int = payload.get("user_id")  # type: ignore
        username: str = payload.get("sub")  # type: ignore
        if user_id is None or username is None:
            raise invalid

        # 令牌类型必须匹配：续期凭条不能当访问令牌直接调接口，反之亦然。
        # 历史令牌没有 type 字段，按访问令牌处理以免升级当天全员掉线。
        if payload.get("type", "access") != expected_type:
            raise invalid

        if AuthService.is_token_revoked(payload.get("jti", "")):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="登录已失效，请重新登录",
                headers={"WWW-Authenticate": "Bearer"},
            )

        return TokenData(user_id=user_id, username=username)
    
    @staticmethod
    def get_user_from_token(db: Session, token: str) -> Optional[User]:
        """验证令牌并返回对应用户

        令牌无效时抛出 HTTPException（与 verify_token 一致）；
        令牌有效但用户不存在时返回 None。
        统一的令牌验证入口，供认证中间件和 WebSocket 端点复用。
        """
        token_data = AuthService.verify_token(token)
        if token_data.username is None:
            return None
        return AuthService.get_user_by_username(db, token_data.username)

    @staticmethod
    def authenticate_user(db: Session, username: str, password: str) -> Optional[User]:
        """认证用户"""
        # 支持用户名或邮箱登录
        user = db.query(User).filter(
            (User.username == username) | (User.email == username)
        ).first()
        
        if not user:
            return None
        # User.to_dict() intentionally excludes password hashes by default.
        # Authentication runs server-side and should read the mapped attribute
        # directly instead of weakening the model's safe serialization default.
        if not AuthService.verify_password(password, str(user.hashed_password)):
            return None
        
        return user
    
    @staticmethod
    def get_user_by_username(db: Session, username: str) -> Optional[User]:
        """根据用户名获取用户"""
        return db.query(User).filter(User.username == username).first()
    
    @staticmethod
    def get_user_by_email(db: Session, email: str) -> Optional[User]:
        """根据邮箱获取用户"""
        return db.query(User).filter(User.email == email).first()

    @staticmethod
    def get_user_by_phone(db: Session, phone: str) -> Optional[User]:
        return db.query(User).filter(User.phone == phone).first()

    @staticmethod
    def send_sms_code(phone: str) -> dict:
        """签发一次性验证码并存入 Redis。

        模拟模式只用于本机开发，且只在本机开发时才会把验证码回显给调用方：

        * 验证码始终随机生成。此前默认是固定的 ``123456``，等于所有账号共用
          一个人人皆知的口令。``SMS_MOCK_CODE`` 仍可显式指定，便于自动化测试，
          但生产环境根本走不到模拟模式。
        * ``dev_code`` 只在非生产环境返回。此前它无条件写进响应体，
          任何人只要知道手机号，问一次就能拿到验证码登录该账号。
        * 生产环境如果仍是模拟模式，直接拒绝服务——启动自检本应已拦下这种
          部署，这里是第二道防线，避免运行期被改成模拟模式后无声降级。
        """
        redis_client = get_redis_client()
        rate_key = f"auth:sms:rate:{phone}"
        code_key = f"auth:sms:code:{phone}"
        try:
            if redis_client.exists(rate_key):
                ttl = max(redis_client.ttl(rate_key), 1)
                raise HTTPException(status_code=429, detail=f"请 {ttl} 秒后再获取验证码")

            provider = os.getenv("SMS_PROVIDER", "mock").lower()
            if provider != "mock":
                raise HTTPException(status_code=503, detail="真实短信服务尚未配置")
            if is_production():
                logger.error("生产环境仍在使用模拟短信，已拒绝发送验证码")
                raise HTTPException(status_code=503, detail="短信服务尚未配置，暂时无法登录")

            code = os.getenv("SMS_MOCK_CODE") or f"{secrets.randbelow(1000000):06d}"
            redis_client.setex(code_key, 300, code)
            redis_client.setex(rate_key, 60, "1")
            return {
                "message": "验证码已发送",
                "expires_in": 300,
                "retry_after": 60,
                "dev_code": code,
            }
        except HTTPException:
            raise
        except RedisError as exc:
            raise HTTPException(status_code=503, detail="验证码服务暂不可用") from exc

    @staticmethod
    def verify_sms_code(phone: str, code: str) -> None:
        """校验验证码；无论成功失败都只允许尝试有限次。

        此前验证码在有效期内可以无限次猜测，六位数字在五分钟里被穷举完全可行。
        现在每个手机号的每一枚验证码最多接受 5 次尝试，超出即作废并要求重新获取。
        """
        redis_client = get_redis_client()
        key = f"auth:sms:code:{phone}"
        attempt_key = f"auth:sms:attempt:{phone}"

        try:
            attempts = redis_client.incr(attempt_key)
            if attempts == 1:
                redis_client.expire(attempt_key, 300)
        except RedisError as exc:
            raise HTTPException(status_code=503, detail="验证码服务暂不可用") from exc

        if attempts > SMS_CODE_MAX_ATTEMPTS:
            redis_client.delete(key)
            raise HTTPException(status_code=429, detail="验证码尝试次数过多，请重新获取")

        expected = redis_client.get(key)
        if not expected or not hmac.compare_digest(str(expected), code):
            raise HTTPException(status_code=400, detail="验证码错误或已过期")

        redis_client.delete(key)
        redis_client.delete(attempt_key)

    @staticmethod
    def authenticate_or_create_phone_user(
        db: Session,
        phone: str,
        code: str,
        invite_code: Optional[str] = None,
        nickname: Optional[str] = None,
    ) -> User:
        user = AuthService.get_user_by_phone(db, phone)
        if user:
            AuthService.verify_sms_code(phone, code)
            return user

        allowed_codes = {
            item.strip() for item in os.getenv("INVITE_CODES", "").split(",") if item.strip()
        }
        if not invite_code or invite_code.strip() not in allowed_codes:
            raise HTTPException(status_code=403, detail="邀请码无效")
        if not nickname or not nickname.strip():
            raise HTTPException(status_code=400, detail="首次登录请设置昵称")

        AuthService.verify_sms_code(phone, code)

        generated_password = secrets.token_urlsafe(32)
        user = User(
            username=f"u_{phone}",
            email=f"{phone}@phone.local",
            phone=phone,
            hashed_password=AuthService.get_password_hash(generated_password),
            nickname=nickname.strip(),
            is_verified=True,
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        return user
    
    @staticmethod
    def get_user_by_id(db: Session, user_id: int) -> Optional[User]:
        """根据ID获取用户"""
        return db.query(User).filter(User.id == user_id).first()
    
    @staticmethod
    def create_user(db: Session, username: str, email: str, password: str, nickname: Optional[str] = None) -> User:
        """创建用户"""
        # 检查用户名是否已存在
        if AuthService.get_user_by_username(db, username):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="用户名已存在"
            )
        
        # 检查邮箱是否已存在
        if AuthService.get_user_by_email(db, email):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="邮箱已被注册"
            )
        
        # 创建新用户
        hashed_password = AuthService.get_password_hash(password)
        db_user = User(
            username=username,
            email=email,
            hashed_password=hashed_password,
            nickname=nickname or username
        )
        
        db.add(db_user)
        db.commit()
        db.refresh(db_user)
        
        return db_user
    
    @staticmethod
    def update_user_profile(db: Session, user_id: int, **kwargs) -> User:
        """更新用户资料"""
        user = AuthService.get_user_by_id(db, user_id)
        if not user:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="用户不存在"
            )
        
        # 更新允许的字段
        allowed_fields = ['nickname', 'bio', 'avatar_url']
        for field, value in kwargs.items():
            if field in allowed_fields and value is not None:
                setattr(user, field, value)
        
        db.commit()
        db.refresh(user)
        
        return user
    
    @staticmethod
    def change_password(db: Session, user_id: int, old_password: str, new_password: str) -> bool:
        """修改密码"""
        user = AuthService.get_user_by_id(db, user_id)
        if not user:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="用户不存在"
            )
        
        # 验证旧密码
        if not AuthService.verify_password(old_password, str(user.hashed_password)):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="旧密码错误"
            )
        
        # 更新密码
        user.set_hashed_password(AuthService.get_password_hash(new_password))
        db.commit()
        
        return True
    
    @staticmethod
    def update_last_login(db: Session, user_id: int) -> None:
        """更新最后登录时间"""
        user = AuthService.get_user_by_id(db, user_id)
        if user:
            setattr(user, 'last_login_at', datetime.now(timezone.utc))
            db.commit()

    @staticmethod
    def get_or_create_guest_user(db: Session, username: str, email: str) -> User:
        """获取或创建默认访客用户（用于匿名访问）"""
        user = AuthService.get_user_by_username(db, username)
        if user:
            return user

        # 生成随机密码（访客账户密码不对外暴露）
        import secrets
        random_password = secrets.token_urlsafe(32)
        hashed_password = AuthService.get_password_hash(random_password)

        db_user = User(
            username=username,
            email=email,
            hashed_password=hashed_password,
            nickname="访客",
        )
        db.add(db_user)
        db.commit()
        db.refresh(db_user)
        return db_user
