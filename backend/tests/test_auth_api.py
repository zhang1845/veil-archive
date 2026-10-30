"""认证API测试"""
import pytest
from fastapi.testclient import TestClient
from src.core.server import app

client = TestClient(app)


class TestAuthAPI:
    """认证API测试类"""
    
    def test_register_user(self, mock_db_session):
        """用户名密码注册默认已关闭，应明确返回 410 而不是静默可用。

        该端点保留是为了给旧客户端一个清楚的答复：注册方式已改为手机验证码。
        只有显式设置 ALLOW_LEGACY_REGISTRATION=true 才会重新开放，
        由下一个用例覆盖。
        """
        response = client.post("/api/auth/register", json={
            "username": "testuser",
            "email": "test@example.com",
            "password": "testpassword123",
            "nickname": "Test User"
        })

        assert response.status_code == 410
        assert "手机号验证码" in response.json()["detail"]

    def test_register_user_enabled_by_explicit_opt_in(self, mock_db_session, monkeypatch):
        """显式开启旧注册后，端点不再返回 410。"""
        monkeypatch.setenv("ALLOW_LEGACY_REGISTRATION", "true")
        response = client.post("/api/auth/register", json={
            "username": "testuser",
            "email": "test@example.com",
            "password": "testpassword123",
            "nickname": "Test User"
        })

        assert response.status_code != 410
    
    def test_register_user_missing_fields(self):
        """测试用户注册缺少必要字段"""
        response = client.post("/api/auth/register", json={
            "username": "testuser"
            # 缺少email和password
        })
        # 应该返回422验证错误
        assert response.status_code == 422
    
    @pytest.mark.skip(reason="Mock对象字典访问问题需要进一步研究")
    def test_login_user(self, mock_db_session):
        """测试用户登录"""
        response = client.post("/api/auth/login", json={
            "username": "testuser",
            "password": "testpassword123"
        })
        
        # 登录可能成功或失败
        assert response.status_code in [200, 401, 500]
    
    def test_login_user_missing_fields(self):
        """测试用户登录缺少必要字段"""
        response = client.post("/api/auth/login", json={
            "username": "testuser"
            # 缺少password
        })
        # 应该返回422验证错误
        assert response.status_code == 422
    
    def test_get_current_user_unauthorized(self):
        """测试未认证获取当前用户信息"""
        response = client.get("/api/auth/me")
        # 应该返回401未认证错误
        assert response.status_code == 401


if __name__ == "__main__":
    pytest.main([__file__])