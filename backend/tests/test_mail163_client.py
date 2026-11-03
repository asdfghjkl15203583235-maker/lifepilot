import imaplib
import smtplib
import unittest
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formatdate, make_msgid
from unittest.mock import AsyncMock, MagicMock, patch

from services.mail163_client import (
    Mail163Config,
    Mail163Error,
    _connect_imap,
    _connect_smtp,
    _parse_search_query,
    get_email_owner_and_subject,
    list_emails,
    mark_as_read,
    mark_as_unread,
    send_email,
    verify_login,
)

CONFIG = Mail163Config(
    email="user@163.com",
    auth_code="authcode-123",
    smtp_host="smtp.163.com",
    smtp_port=465,
    imap_host="imap.163.com",
    imap_port=993,
    timeout_seconds=5,
)


def _fake_email() -> str:
    message = MIMEMultipart("alternative")
    message["Subject"] = "=?utf-8?B?5rWL6K+V5Li76aKY?="  # 测试主题 encoded as UTF-8 base64
    message["From"] = "同事 <colleague@example.com>"
    message["To"] = CONFIG.email
    message["Date"] = formatdate(datetime.now(timezone.utc).timestamp())
    message["Message-ID"] = make_msgid(domain="example.com")
    message.attach(MIMEText("这是测试邮件正文。", "plain", "utf-8"))
    return message.as_bytes()


class TestMail163Client(unittest.TestCase):
    def test_connect_imap_sends_id_after_login(self) -> None:
        if "ID" not in imaplib.Commands:
            imaplib.Commands["ID"] = ("AUTH",)
        mail = MagicMock()
        mail.login.return_value = ("OK", [b"user@163.com"])
        mail._simple_command.return_value = ("OK", [b"ID OK"])
        with patch("imaplib.IMAP4_SSL", return_value=mail):
            result = _connect_imap(CONFIG)
        self.assertIs(result, mail)
        mail.login.assert_called_once_with(CONFIG.email, CONFIG.auth_code)
        mail._simple_command.assert_called_once()
        name = mail._simple_command.call_args.args[0]
        args = mail._simple_command.call_args.args[1]
        self.assertEqual(name, "ID")
        self.assertIn('"name"', args)
        self.assertIn('"LifePilot"', args)
        self.assertIn('"version"', args)

    def test_parse_search_query(self) -> None:
        terms = _parse_search_query('from:alice subject:"项目进度" is:unread newer_than:14d')
        self.assertIn("FROM alice", terms)
        self.assertIn("SUBJECT 项目进度", terms)
        self.assertIn("UNSEEN", terms)
        self.assertTrue(any(term.startswith("SINCE ") for term in terms))

    def test_parse_search_query_empty_falls_back_to_all(self) -> None:
        self.assertEqual(_parse_search_query(""), [])
        self.assertEqual(_parse_search_query("  "), [])


class TestMail163AsyncClient(unittest.IsolatedAsyncioTestCase):
    def _fake_mail(self) -> MagicMock:
        mail = MagicMock()
        mail.select.return_value = ("OK", [b"1"])
        mail.logout.side_effect = None
        return mail

    @patch("services.mail163_client._connect_imap")
    async def test_verify_login_success(self, connect_imap) -> None:  # type: ignore[no-untyped-def]
        mail = self._fake_mail()
        connect_imap.return_value = mail
        result = await verify_login(CONFIG)
        connect_imap.assert_called_once_with(CONFIG)
        mail.select.assert_called_once_with("INBOX")
        self.assertEqual(result, "user@163.com")

    @patch("services.mail163_client._connect_imap")
    async def test_verify_login_unsafe_login_surfaces_troubleshooting(self, connect_imap) -> None:  # type: ignore[no-untyped-def]
        mail = self._fake_mail()
        mail.select.return_value = ("NO", [b"SELECT Unsafe Login. Please contact kefu@188.com for help"])
        connect_imap.return_value = mail
        with self.assertRaises(Mail163Error) as caught:
            await verify_login(CONFIG)
        self.assertEqual(caught.exception.code, 503)
        self.assertIn("Unsafe Login", caught.exception.message)
        self.assertIn("MAIL163_EMAIL_AUTH_CODE", caught.exception.message)
        mail.logout.assert_called_once()

    @patch("services.mail163_client._connect_imap")
    async def test_list_emails(self, connect_imap) -> None:  # type: ignore[no-untyped-def]
        mail = self._fake_mail()
        connect_imap.return_value = mail
        mail.uid.side_effect = [
            ("OK", [b"42 43"]),
            ("OK", [(b"(RFC822 {size}", _fake_email())]),
            ("OK", [(b"(RFC822 {size}", _fake_email())]),
        ]
        result = await list_emails(CONFIG, max_results=5)
        self.assertEqual(result["fetched_message_count"], 2)
        self.assertEqual(result["messages"][0]["id"], "42")

    @patch("services.mail163_client._connect_imap")
    async def test_mark_as_read(self, connect_imap) -> None:  # type: ignore[no-untyped-def]
        mail = self._fake_mail()
        connect_imap.return_value = mail
        mail.uid.return_value = ("OK", [b"1"])
        result = await mark_as_read(CONFIG, "42")
        self.assertEqual(result["status"], "read")

    @patch("services.mail163_client._connect_imap")
    async def test_mark_as_unread(self, connect_imap) -> None:  # type: ignore[no-untyped-def]
        mail = self._fake_mail()
        connect_imap.return_value = mail
        mail.uid.return_value = ("OK", [b"1"])
        result = await mark_as_unread(CONFIG, "42")
        self.assertEqual(result["status"], "unread")

    @patch("services.mail163_client._connect_smtp")
    async def test_send_email(self, connect_smtp) -> None:  # type: ignore[no-untyped-def]
        smtp = MagicMock()
        smtp.sendmail.return_value = {}
        connect_smtp.return_value = smtp
        result = await send_email(CONFIG, "team@example.com", "测试主题", "正文")
        self.assertEqual(result["status"], "sent")
        sent_args = smtp.sendmail.call_args
        self.assertEqual(sent_args.args[0], "user@163.com")
        self.assertEqual(sent_args.args[1], ["team@example.com"])
        self.assertNotIn("authcode-123", sent_args.args[2])

    @patch("services.mail163_client._connect_imap")
    async def test_get_email_owner_and_subject(self, connect_imap) -> None:  # type: ignore[no-untyped-def]
        mail = self._fake_mail()
        connect_imap.return_value = mail
        mail.uid.return_value = ("OK", [(b"(RFC822 {size}", _fake_email())])
        result = get_email_owner_and_subject(CONFIG, "42")
        self.assertEqual(result["subject"], "测试主题")
        self.assertIn("同事", result["from"])

    @patch("services.mail163_client._connect_imap")
    async def test_credentials_not_in_outputs(self, connect_imap) -> None:  # type: ignore[no-untyped-def]
        mail = self._fake_mail()
        connect_imap.return_value = mail
        mail.uid.side_effect = [
            ("OK", [b"42"]),
            ("OK", [(b"(RFC822 {size}", _fake_email())]),
        ]
        result = await list_emails(CONFIG, max_results=5)
        self.assertNotIn("authcode-123", str(result))


class TestSmtpLoginRetry(unittest.TestCase):
    """163 在短时间连续发信时会用 535 拒绝登录，隔两秒重试通常就能过。"""

    def test_retries_once_when_login_is_temporarily_refused(self) -> None:
        first, second = MagicMock(), MagicMock()
        first.login.side_effect = smtplib.SMTPAuthenticationError(535, b"too frequent")

        with patch("smtplib.SMTP_SSL", side_effect=[first, second]), patch(
            "services.mail163_client.time.sleep"
        ) as sleep:
            smtp = _connect_smtp(CONFIG)

        self.assertIs(smtp, second)
        second.login.assert_called_once_with(CONFIG.email, CONFIG.auth_code)
        first.close.assert_called_once()
        sleep.assert_called_once()

    def test_gives_up_after_two_attempts_and_marks_the_error_retryable(self) -> None:
        first, second = MagicMock(), MagicMock()
        first.login.side_effect = smtplib.SMTPAuthenticationError(535, b"nope")
        second.login.side_effect = smtplib.SMTPAuthenticationError(535, b"nope")

        with patch("smtplib.SMTP_SSL", side_effect=[first, second]), patch(
            "services.mail163_client.time.sleep"
        ):
            with self.assertRaises(Mail163Error) as caught:
                _connect_smtp(CONFIG)

        self.assertTrue(caught.exception.retryable)
        self.assertEqual(caught.exception.code, 502)
        self.assertIn("稍后重试", caught.exception.message)


if __name__ == "__main__":
    unittest.main()