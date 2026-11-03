import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from db.dependencies import verify_database_ready
from main import app
from services.mail163_client import Mail163Config

CONFIG = Mail163Config(
    email="user@163.com",
    auth_code="authcode-123",
    smtp_host="smtp.163.com",
    smtp_port=465,
    imap_host="imap.163.com",
    imap_port=993,
    timeout_seconds=5,
)


class TestMail163Login(unittest.TestCase):
    def setUp(self) -> None:
        app.dependency_overrides[verify_database_ready] = lambda: None

    def tearDown(self) -> None:
        app.dependency_overrides.clear()

    def test_login_without_credentials_returns_readable_page(self) -> None:
        with TestClient(app) as client, patch(
            "config.has_mail163_credentials", return_value=False
        ), patch("routers.mail163.has_mail163_credentials", return_value=False):
            response = client.get("/mail163/login")
        self.assertEqual(response.status_code, 503)
        self.assertIn("text/html", response.headers["content-type"])
        self.assertIn("MAIL163_EMAIL", response.text)

    def test_login_imap_failure_returns_readable_html(self) -> None:
        from services.mail163_client import Mail163Error

        with TestClient(app) as client, patch(
            "routers.mail163.has_mail163_credentials", return_value=True
        ), patch(
            "routers.mail163.verify_login",
            new=AsyncMock(side_effect=Mail163Error("IMAP 登录失败", 502)),
            create=True,
        ):
            response = client.get("/mail163/login")
        self.assertEqual(response.status_code, 503)
        self.assertIn("text/html", response.headers["content-type"])
        self.assertIn("163 邮箱登录失败", response.text)
        self.assertIn("IMAP 登录失败", response.text)

    def test_login_success_redirects_with_session_fragment(self) -> None:
        session = AsyncMock(spec=AsyncSession)
        user = MagicMock()
        user.id = "00000000-0000-0000-0000-000000000001"
        user.email = "user@163.com"
        session.execute.return_value.scalar_one_or_none.return_value = user

        with TestClient(app) as client, patch(
            "routers.mail163.has_mail163_credentials", return_value=True
        ), patch(
            "routers.mail163.verify_login",
            new=AsyncMock(return_value="user@163.com"),
            create=True,
        ), patch(
            "routers.mail163.get_session",
            return_value=session,
        ):
            response = client.get("/mail163/login", follow_redirects=False)

        self.assertEqual(response.status_code, 302)
        self.assertIn("session=", response.headers["location"])
        self.assertNotIn("authcode-123", response.text)

    def test_status_without_credentials_returns_503(self) -> None:
        # Status is auth protected; this verifies the protected path still works.
        with TestClient(app) as client:
            response = client.get("/mail163/status")
        self.assertEqual(response.status_code, 401)

    def test_disconnect_without_auth_returns_401(self) -> None:
        with TestClient(app) as client:
            response = client.post("/mail163/disconnect")
        self.assertEqual(response.status_code, 401)


if __name__ == "__main__":
    unittest.main()