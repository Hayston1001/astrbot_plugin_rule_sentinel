import unittest

from sentinel.provider_capabilities import declared_modalities, image_capability, tool_capability


class ProviderCapabilityTests(unittest.TestCase):
    def test_missing_or_empty_modalities_are_undeclared(self):
        for config in ({}, {"modalities": None}, {"modalities": []}, {"modalities": "image"}, None):
            with self.subTest(config=config):
                self.assertIsNone(declared_modalities(config))
                self.assertEqual(image_capability(config), "undeclared")
                self.assertEqual(tool_capability(config), "undeclared")

    def test_declared_capabilities_are_read_as_is(self):
        config = {"modalities": ["text", "image", "tool_use"]}
        self.assertEqual(declared_modalities(config), ("text", "image", "tool_use"))
        self.assertEqual(image_capability(config), "image")
        self.assertEqual(tool_capability(config), "tool_use")
        self.assertEqual(image_capability({"modalities": ["text"]}), "no_image")
        self.assertEqual(tool_capability({"modalities": ["text", "image"]}), "no_tool_use")


if __name__ == "__main__":
    unittest.main()
