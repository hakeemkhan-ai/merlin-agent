import os
import unittest
from unittest.mock import patch

from pydantic import ValidationError

from merlin.config import Settings


class SettingsTests(unittest.TestCase):
    def test_empty_optional_environment_values_use_defaults(self):
        with patch.dict(os.environ, {"MERLIN_GITHUB_INSTALLATION_ID": ""}):
            self.assertIsNone(Settings(_env_file=None).github_installation_id)

    def test_example_configuration_can_be_parsed_without_credentials(self):
        settings = Settings(_env_file=".env.example")
        self.assertGreaterEqual(settings.max_input_tokens, 8192)

    def test_negative_rates_and_invalid_limits_are_rejected(self):
        for arguments in [
            {"input_cost_per_million": -1},
            {"max_output_tokens": 0},
            {"max_input_tokens": 1},
        ]:
            with self.subTest(arguments=arguments), self.assertRaises(ValidationError):
                Settings(_env_file=None, **arguments)
