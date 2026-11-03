"""一轮扫描只登一次 IMAP：连接复用与中途断线重连。"""

import imaplib
import unittest
from unittest.mock import MagicMock, patch

from services import mail163_client
from services.mail163_client import Mail163Config, Mail163Session


def _config() -> Mail163Config:
    return Mail163Config(email="someone@163.com", auth_code="auth-code")


class Mail163SessionTests(unittest.TestCase):
    def test_one_connection_is_shared_by_every_operation(self) -> None:
        with patch.object(
            mail163_client, "_connect_imap", return_value=MagicMock()
        ) as connect, patch.object(
            mail163_client, "_list_messages", return_value={"messages": []}
        ), patch.object(
            mail163_client, "_fetch_email_dict", return_value={"id": "1"}
        ):
            session = Mail163Session(_config())
            session.list_messages(query="newer_than:2d")
            session.fetch_email("1")
            session.fetch_email("2")
            session.list_messages()
            session.close()

        connect.assert_called_once()

    def test_dropped_connection_is_retried_once(self) -> None:
        with patch.object(
            mail163_client, "_connect_imap", side_effect=[MagicMock(), MagicMock()]
        ) as connect, patch.object(
            mail163_client,
            "_fetch_email_dict",
            side_effect=[imaplib.IMAP4.abort("connection reset"), {"id": "1"}],
        ):
            session = Mail163Session(_config())
            result = session.fetch_email("1")

        self.assertEqual(result, {"id": "1"})
        self.assertEqual(connect.call_count, 2)

    def test_close_is_safe_without_any_operation(self) -> None:
        session = Mail163Session(_config())

        session.close()
