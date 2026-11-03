"""Speech-to-text and text-to-speech tests."""

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from config import Settings
from openai import AuthenticationError
from services.speech_service import (
    SiliconFlowInvalidApiKeyError,
    transcribe_audio,
)


class TestSpeechTranscription(unittest.IsolatedAsyncioTestCase):
    async def test_transcribe_audio_uses_siliconflow_openai_client(self) -> None:
        settings = Settings(
            siliconflow_api_key="test-siliconflow-key",
            siliconflow_base_url="https://api.siliconflow.cn/v1",
            siliconflow_transcription_model="FunAudioLLM/SenseVoiceSmall",
        )
        fake_client = MagicMock()
        fake_response = AsyncMock()
        fake_response.text = "转写结果"
        fake_client.audio.transcriptions.create = AsyncMock(return_value=fake_response)

        with patch("services.speech_service.AsyncOpenAI", return_value=fake_client) as mock_client:
            result = await transcribe_audio(settings, b"audio-bytes", "test.wav")

        self.assertEqual(result, "转写结果")
        mock_client.assert_called_once()
        client_kwargs = mock_client.call_args.kwargs
        self.assertEqual(client_kwargs["api_key"], "test-siliconflow-key")
        self.assertEqual(client_kwargs["base_url"], "https://api.siliconflow.cn/v1")
        fake_client.audio.transcriptions.create.assert_awaited_once()
        kwargs = fake_client.audio.transcriptions.create.call_args.kwargs
        self.assertEqual(kwargs["model"], "FunAudioLLM/SenseVoiceSmall")
        self.assertEqual(kwargs["file"], ("test.wav", b"audio-bytes"))

    async def test_transcribe_audio_maps_auth_error_to_siliconflow_error(self) -> None:
        settings = Settings(
            siliconflow_api_key="bad-key",
            siliconflow_base_url="https://api.siliconflow.cn/v1",
            siliconflow_transcription_model="FunAudioLLM/SenseVoiceSmall",
        )
        fake_client = MagicMock()
        fake_client.audio.transcriptions.create = AsyncMock(
            side_effect=AuthenticationError(
                message="invalid api key",
                response=MagicMock(),
                body=None,
            )
        )
        with patch("services.speech_service.AsyncOpenAI", return_value=fake_client):
            with self.assertRaises(SiliconFlowInvalidApiKeyError):
                await transcribe_audio(settings, b"audio-bytes", "test.wav")


if __name__ == "__main__":
    unittest.main()