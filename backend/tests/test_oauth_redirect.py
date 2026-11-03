import unittest

from auth.oauth_redirect import build_frontend_oauth_redirect_url


class TestOAuthRedirect(unittest.TestCase):
    def test_build_frontend_oauth_redirect_inserts_fragment(self) -> None:
        url = build_frontend_oauth_redirect_url(
            "http://frontend.example/app",
            "header.payload.signature",
        )
        self.assertTrue(url.startswith("http://frontend.example/app#session="))
        self.assertIn("header.payload.signature", url)


if __name__ == "__main__":
    unittest.main()
