"""用户认证API路由"""
from datetime import timedelta
import os
from fastapi import APIRouter, Depends, HTTPException, status, Request
from sqlalchemy.orm import Session
from src.services.auth_service import AuthService, ACCESS_TOKEN_EXPIRE_MINUTES
from src.core.auth_middleware import (
    get_current_active_user_from_request,
    get_current_admin_user_from_request,
)
from src.core.rate_limit import (
    clear_login_guard,
    guard_login,
    guard_refresh,
    guard_sms,
)
from src.core.security_preflight import is_production
from src.schemas.user_schemas import (
    UserRegister, UserLogin, UserResponse, UserUpdate, PasswordChange,
    Token, UserBrief, SmsCodeRequest, SmsCodeResponse, PhoneLogin, RefreshRequest
)
from src.db.models.user import User
from src.core.container_integration import get_db_session_depends
from src.core.config import config

router = APIRouter(prefix="/api/auth", tags=["用户认证"])

def _token_for_user(db: Session, user: User) -> Token:
    if user.is_active is False:
        raise HTTPException(status_code=401, detail="用户账户已被禁用")
    claims = {"sub": user.username, "user_id": getattr(user, "id")}
    access_token = AuthService.create_access_token(
        data=claims,
        expires_delta=timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES),
    )
    AuthService.update_last_login(db, getattr(user, "id"))
    return Token(
        access_token=access_token,
        token_type="bearer",
        expires_in=ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        refresh_token=AuthService.create_refresh_token(data=claims),
        user=UserResponse.model_validate(user),
    )

@router.post("/sms-code", response_model=SmsCodeResponse, summary="发送手机验证码")
async def send_sms_code(request: Request, data: SmsCodeRequest):
    # 同一手机号和同一来源都有小时级上限，验证码不能被无限索取。
    guard_sms(request, data.phone)
    result = AuthService.send_sms_code(data.phone)
    if is_production():
        # 双保险：生产环境永远不把验证码回显给调用方。
        result.pop("dev_code", None)
    return SmsCodeResponse(**result)

@router.post("/phone-login", response_model=Token, summary="手机号验证码登录或首次注册")
async def phone_login(
    request: Request,
    data: PhoneLogin,
    db: Session = get_db_session_depends(),
):
    guard_login(request, data.phone)
    user = AuthService.authenticate_or_create_phone_user(
        db, data.phone, data.code, data.invite_code, data.nickname
    )
    clear_login_guard(data.phone)
    return _token_for_user(db, user)

@router.post("/refresh", response_model=Token, summary="用续期凭条换取新的访问令牌")
async def refresh_access_token(
    request: Request,
    data: RefreshRequest,
    db: Session = get_db_session_depends(),
):
    """访问令牌到期后在后台静默换新，使用者无需重新登录。

    换发时会挂失用过的这张续期凭条并下发新的一张，因此同一张凭条只能用一次；
    凭条被盗用后，真实用户的下一次换发就会失败，异常可以被发现。
    """
    guard_refresh(request)
    token_data = AuthService.verify_token(data.refresh_token, expected_type="refresh")
    if token_data.username is None:
        raise HTTPException(status_code=401, detail="无效的续期凭条")

    user = AuthService.get_user_by_username(db, token_data.username)
    if user is None:
        raise HTTPException(status_code=401, detail="无效的续期凭条")

    AuthService.revoke_token(data.refresh_token)
    return _token_for_user(db, user)

@router.post("/anonymous-login", response_model=Token, summary="匿名登录")
async def anonymous_login(
    db: Session = get_db_session_depends()
):
    """匿名登录 — 仅在 ALLOW_ANONYMOUS_ACCESS=true 时可用，自动以默认访客账户登录"""
    if not config.allow_anonymous_access:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="匿名访问未启用，请先注册账号",
        )

    user = AuthService.get_or_create_guest_user(
        db, config.guest_username, config.guest_email
    )
    return _token_for_user(db, user)

@router.post("/register", response_model=UserResponse, summary="用户注册")
async def register(
    user_data: UserRegister,
    db: Session = get_db_session_depends()
):
    """用户注册"""
    if os.getenv("ALLOW_LEGACY_REGISTRATION", "false").lower() != "true":
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail="用户名密码注册已关闭，请使用手机号验证码登录",
        )
    try:
        # 创建用户
        user = AuthService.create_user(
            db=db,
            username=user_data.username,
            email=user_data.email,
            password=user_data.password,
            nickname=user_data.nickname
        )
        
        return UserResponse.model_validate(user)
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"注册失败: {str(e)}"
        )

@router.post("/login", response_model=Token, summary="用户登录")
async def login(
    request: Request,
    user_data: UserLogin,
    db: Session = get_db_session_depends()
):
    """用户登录"""
    # 按账号和来源分别限流，密码不能被无限次尝试。
    guard_login(request, user_data.username)

    user = AuthService.authenticate_user(db, user_data.username, user_data.password)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="用户名或密码错误",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # 检查用户是否激活
    if user.is_active is False:  # 使用显式字段比较，避免SQLAlchemy布尔比较问题
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="用户账户已被禁用",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # 登录成功后清零该账号的失败计数，正常用户不会被历史失败拖累。
    clear_login_guard(user_data.username)
    return _token_for_user(db, user)

@router.get("/me", response_model=UserResponse, summary="获取当前用户信息")
async def get_current_user_info(
    current_user: User = Depends(get_current_active_user_from_request)
):
    """获取当前用户信息（由认证中间件注入）"""
    return UserResponse.model_validate(current_user)

@router.put("/me", response_model=UserResponse, summary="更新用户资料")
async def update_profile(
    user_update: UserUpdate,
    current_user: User = Depends(get_current_active_user_from_request),
    db: Session = get_db_session_depends()
):
    """更新用户资料"""
    try:
        # 更新用户资料
        updated_user = AuthService.update_user_profile(
            db=db,
            user_id=getattr(current_user, 'id'),  # 获取实际的id值
            nickname=user_update.nickname,
            bio=user_update.bio,
            avatar_url=user_update.avatar_url
        )
        
        return UserResponse.model_validate(updated_user)
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"更新资料失败: {str(e)}"
        )

@router.post("/change-password", summary="修改密码")
async def change_password(
    password_data: PasswordChange,
    current_user: User = Depends(get_current_active_user_from_request),
    db: Session = get_db_session_depends()
):
    """修改密码"""
    try:
        # 修改密码
        success = AuthService.change_password(
            db=db,
            user_id=getattr(current_user, 'id'),  # 获取实际的id值
            old_password=password_data.old_password,
            new_password=password_data.new_password
        )
        
        if not success:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="密码修改失败"
            )
            
        return {"message": "密码修改成功"}
            
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"密码修改失败: {str(e)}"
        )

@router.post("/logout", summary="用户登出")
async def logout(
    request: Request,
    current_user: User = Depends(get_current_active_user_from_request),
):
    """用户登出，并让本次使用的令牌立即失效。

    此前登出只是让前端把令牌删掉，那枚令牌在服务端依然被接受，
    泄露后无法收回。现在会把它的编号写进挂失名单，剩余有效期内一律拒绝。
    """
    authorization = request.headers.get("Authorization", "")
    revoked = False
    if authorization.startswith("Bearer "):
        revoked = AuthService.revoke_token(authorization[7:])
    return {"message": "登出成功", "token_revoked": revoked}

@router.get("/users", response_model=list[UserBrief], summary="获取用户列表")
async def get_users(
    request: Request,
    skip: int = 0,
    limit: int = 20,
    db: Session = get_db_session_depends()
):
    """获取用户列表（由认证中间件验证管理员权限）"""
    # 使用中间件验证管理员权限
    current_user = get_current_admin_user_from_request(request)
    users = db.query(User).filter(User.is_active == True).offset(skip).limit(limit).all()
    return [UserBrief.model_validate(user) for user in users]

@router.get("/users/{user_id}", response_model=UserBrief, summary="获取指定用户信息")
async def get_user_by_id(
    user_id: int,
    db: Session = get_db_session_depends(),
    current_user: User = Depends(get_current_active_user_from_request)
):
    """获取指定用户信息"""
    user = AuthService.get_user_by_id(db, user_id)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="用户不存在"
        )
    
    # 检查用户是否激活
    if user.is_active is False:  # 使用显式字段比较，避免SQLAlchemy布尔比较问题
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="用户不存在"
        )
    
    return UserBrief.model_validate(user)

@router.get("/verify-token", summary="验证令牌")
async def verify_token(
    current_user: User = Depends(get_current_active_user_from_request)
):
    """验证令牌有效性"""
    return {
        "valid": True,
        "user_id": current_user.id,
        "username": current_user.username
    }
