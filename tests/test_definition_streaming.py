import unittest
from unittest.mock import patch
import pandas as pd
from scripts.iv_expansion_history import DownloadLedger


class DefinitionStreamingTests(unittest.TestCase):
    def test_definitions_keep_only_unique_symbols_from_bounded_chunks(self):
        class Store:
            def to_df(self, **kwargs):
                assert kwargs == {'count': 10000, 'map_symbols': False}
                yield pd.DataFrame({'raw_symbol': ['A', 'B'], 'unused': [1, 2]})
                yield pd.DataFrame({'raw_symbol': ['B', 'C'], 'unused': [3, 4]})
        with patch('scripts.iv_expansion_history.db.DBNStore', Store):
            frame = DownloadLedger._frame(Store(), 'definitions')
        self.assertEqual(list(frame.columns), ['raw_symbol'])
        self.assertEqual(frame.raw_symbol.tolist(), ['A', 'B', 'C'])
