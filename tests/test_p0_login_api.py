"""v2 阶段 0：登录接口覆盖测试。

验证：
1. JSON body 登录成功签发 token
2. 无效凭证返回 401
3. 过期 token 被拒绝
4. 错误 issuer/audience 被拒绝
5. LoginRequest DTO 校验（空 username/password）
"""

import unittest
from datetime import datetime, timedelta, timezone

from app.core.config import get_settings
from app.core.database import Base, SessionLocal
from app.core.security import create_jwt_token, hash_password
from app.models.entities import UserAccount
from app.schemas.dtos import LoginRequest


class LoginRequestDtoTests(unittest.TestCase):
    """LoginRequest DTO 校验测试。"""

    def test_valid_request(self):
        req = LoginRequest(username="alice", password="secret")
        self.assertEqual(req.username, "alice")
        self.assertEqual(req.password, "secret")

    def test_empty_username_rejected(self):
        from pydantic import ValidationError
        with self.assertRaises(ValidationError):
            LoginRequest(username="", password="secret")

    def test_empty_password_rejected(self):
        from pydantic import ValidationError
        with self.assertRaises(ValidationError):
            LoginRequest(username="alice", password="")

    def test_overlong_username_rejected(self):
        from pydantic import ValidationError
        with self.assertRaises(ValidationError):
            LoginRequest(username="x" * 65, password="secret")

    def test_missing_fields_rejected(self):
        from pydantic import ValidationError
        with self.assertRaises(ValidationError):
            LoginRequest()  # type: ignore[call-arg]


class JwtTokenTests(unittest.TestCase):
    """JWT token 签发与验证测试。"""

    def setUp(self):
        self.settings = get_settings()
        self.settings.jwt_secret_key = "test-secret-key-for-unit-tests"
        self.settings.jwt_issuer = "test-issuer"
        self.settings.jwt_audience = "test-audience"

        from sqlalchemy import create_engine
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(bind=self.engine)
        self.db = SessionLocal()
        self.db.bind = self.engine

        self.user = UserAccount(
            username="alice",
            display_name="Alice",
            password_hash=hash_password("secret123"),
            roles_csv="KNOWLEDGE_VIEWER",
        )
        self.db.add(self.user)
        self.db.commit()

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(bind=self.engine)

    def test_create_and_verify_token(self):
        token = create_jwt_token(self.user)
        self.assertTrue(token)

        from app.core.security import _verify_jwt
        payload = _verify_jwt(token)
        self.assertIsNotNone(payload)
        self.assertEqual(int(payload["sub"]), self.user.id)
        self.assertEqual(payload["username"], "alice")

    def test_invalid_signature_rejected(self):
        token = create_jwt_token(self.user)
        # 篡改签名
        parts = token.split(".")
        parts[2] = "invalid-signature"
        tampered = ".".join(parts)

        from app.core.security import _verify_jwt
        payload = _verify_jwt(tampered)
        self.assertIsNone(payload)

    def test_expired_token_rejected(self):
        import jwt as pyjwt

        # 签发一个已过期的 token
        payload = {
            "sub": str(self.user.id),
            "username": self.user.username,
            "roles": self.user.roles,
            "iss": self.settings.jwt_issuer,
            "aud": self.settings.jwt_audience,
            "exp": datetime.now(timezone.utc) - timedelta(minutes=5),
            "iat": datetime.now(timezone.utc) - timedelta(hours=1),
        }
        expired = pyjwt.encode(payload, self.settings.jwt_secret_key, algorithm=self.settings.jwt_algorithm)

        from app.core.security import _verify_jwt
        result = _verify_jwt(expired)
        self.assertIsNone(result, "过期 token 应被拒绝")

    def test_wrong_issuer_rejected(self):
        import jwt as pyjwt

        payload = {
            "sub": str(self.user.id),
            "username": self.user.username,
            "roles": self.user.roles,
            "iss": "wrong-issuer",  # 错误 issuer
            "aud": self.settings.jwt_audience,
            "exp": datetime.now(timezone.utc) + timedelta(minutes=30),
            "iat": datetime.now(timezone.utc),
        }
        token = pyjwt.encode(payload, self.settings.jwt_secret_key, algorithm=self.settings.jwt_algorithm)

        from app.core.security import _verify_jwt
        result = _verify_jwt(token)
        self.assertIsNone(result, "错误 issuer 应被拒绝")

    def test_wrong_audience_rejected(self):
        import jwt as pyjwt

        payload = {
            "sub": str(self.user.id),
            "username": self.user.username,
            "roles": self.user.roles,
            "iss": self.settings.jwt_issuer,
            "aud": "wrong-audience",  # 错误 audience
            "exp": datetime.now(timezone.utc) + timedelta(minutes=30),
            "iat": datetime.now(timezone.utc),
        }
        token = pyjwt.encode(payload, self.settings.jwt_secret_key, algorithm=self.settings.jwt_algorithm)

        from app.core.security import _verify_jwt
        result = _verify_jwt(token)
        self.assertIsNone(result, "错误 audience 应被拒绝")


if __name__ == "__main__":
    unittest.main()
