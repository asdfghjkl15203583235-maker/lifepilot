import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from db.dependencies import verify_database_ready
from main import app


class TestAuthProtection(unittest.TestCase):
    def setUp(self) -> None:
        app.dependency_overrides[verify_database_ready] = lambda: None

    def tearDown(self) -> None:
        app.dependency_overrides.clear()

    def test_health_without_auth_returns_ok(self) -> None:
        with TestClient(app) as client:
            response = client.get("/health")
        self.assertEqual(response.status_code, 200)

    def test_chat_without_cookie_returns_401(self) -> None:
        with TestClient(app) as client:
            response = client.post(
                "/chat",
                json={"messages": [{"role": "user", "content": "hello"}]},
            )
        self.assertEqual(response.status_code, 401)

    def test_auth_me_without_cookie_returns_401(self) -> None:
        with TestClient(app) as client:
            response = client.get("/auth/me")
        self.assertEqual(response.status_code, 401)

    def test_auth_logout_without_cookie_returns_200(self) -> None:
        with TestClient(app) as client:
            response = client.post("/auth/logout")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"ok": True})

    def test_stop_background_job_without_auth_returns_401(self) -> None:
        with TestClient(app) as client:
            response = client.post("/background-jobs/00000000-0000-0000-0000-000000000001/stop")
        self.assertEqual(response.status_code, 401)

    def test_delete_background_job_without_auth_returns_401(self) -> None:
        with TestClient(app) as client:
            response = client.delete("/background-jobs/00000000-0000-0000-0000-000000000001")
        self.assertEqual(response.status_code, 401)

    def test_mail163_status_without_auth_returns_401(self) -> None:
        with TestClient(app) as client:
            response = client.get("/mail163/status")
        self.assertEqual(response.status_code, 401)

    def test_mail163_disconnect_without_auth_returns_401(self) -> None:
        with TestClient(app) as client:
            response = client.post("/mail163/disconnect")
        self.assertEqual(response.status_code, 401)

    def test_calendar_disconnect_without_auth_returns_401(self) -> None:
        with TestClient(app) as client:
            response = client.post("/calendar/disconnect")
        self.assertEqual(response.status_code, 401)

    def test_notion_disconnect_without_auth_returns_401(self) -> None:
        with TestClient(app) as client:
            response = client.post("/notion/disconnect")
        self.assertEqual(response.status_code, 401)

    def test_notifications_list_without_auth_returns_401(self) -> None:
        with TestClient(app) as client:
            response = client.get("/notifications")
        self.assertEqual(response.status_code, 401)

    def test_conversations_list_without_auth_returns_401(self) -> None:
        with TestClient(app) as client:
            response = client.get("/conversations")
        self.assertEqual(response.status_code, 401)

    def test_conversation_get_without_auth_returns_401(self) -> None:
        with TestClient(app) as client:
            response = client.get("/conversations/00000000-0000-0000-0000-000000000001")
        self.assertEqual(response.status_code, 401)

    def test_conversation_delete_without_auth_returns_401(self) -> None:
        with TestClient(app) as client:
            response = client.delete("/conversations/00000000-0000-0000-0000-000000000001")
        self.assertEqual(response.status_code, 401)

    def test_notifications_unread_count_without_auth_returns_401(self) -> None:
        with TestClient(app) as client:
            response = client.get("/notifications/unread-count")
        self.assertEqual(response.status_code, 401)

    def test_task_scan_plan_without_auth_returns_401(self) -> None:
        with TestClient(app) as client:
            response = client.get("/tasks/scan-plan")
        self.assertEqual(response.status_code, 401)


if __name__ == "__main__":
    unittest.main()
