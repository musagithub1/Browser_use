import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

from jev_linux import config
from jev_linux.voice import create_voice_recognizer, speech_model_name


class SpeechConfigTests(unittest.TestCase):
    def test_legacy_config_migrates_to_moonshine_and_preserves_vosk_and_key(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps({"speech_model_path": "/models/vosk", "openrouter_api_key": "test-private-key"}))
            with patch.object(config, "CONFIG_PATH", path), patch.dict("os.environ", {"OPENROUTER_API_KEY": "", "TYPESAFE_API_KEY": ""}):
                result = config.load_config()
            self.assertEqual(result["speech_engine"], "moonshine")
            self.assertEqual(result["speech_model_arch"], 4)
            self.assertEqual(result["speech_model_path"], config.DEFAULTS["speech_model_path"])
            self.assertEqual(result["vosk_model_path"], "/models/vosk")
            self.assertEqual(result["openrouter_api_key"], "test-private-key")

    def test_default_factory_uses_local_moonshine(self):
        factory = Mock()
        module = types.SimpleNamespace(MoonshineVoiceRecognizer=factory)
        settings = dict(config.DEFAULTS)
        transcript, status = Mock(), Mock()
        with patch.dict("sys.modules", {"jev_linux.moonshine_voice": module}):
            create_voice_recognizer(settings, transcript, status)
        factory.assert_called_once_with(settings, transcript, status)

    def test_explicit_vosk_fallback_keeps_its_own_path(self):
        settings = {**config.DEFAULTS, "speech_engine": "vosk", "vosk_model_path": "/models/vosk"}
        with patch("jev_linux.voice.VoiceRecognizer") as factory:
            create_voice_recognizer(settings, None, None)
        factory.assert_called_once_with("/models/vosk", None, None)

    def test_model_name_shows_selected_quality(self):
        self.assertIn("Small Streaming", speech_model_name({"speech_model_arch": 4}))
        self.assertIn("Medium Streaming", speech_model_name({"speech_model_arch": 5}))


if __name__ == "__main__":
    unittest.main()
