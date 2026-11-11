import json
from pathlib import Path
import unittest

from collector.domain import *


class DomainTests(unittest.TestCase):
    def setUp(self):
        self.payload = json.loads(Path('fixtures/sample.json').read_text())

    def test_domain_behavior(self):
        self.assertEqual(normalize_label(' Ｘ１００  '), 'x100')
        self.assertNotEqual(source_identity('a', '1'), source_identity('b', '1'))
        with self.assertRaises(ValueError):
            source_identity('', '1')

    def test_deterministic_observations(self):
        self.assertEqual(collect(self.payload), collect(self.payload))
