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

    def test_shared_cache_keeps_timestamps_but_drops_wide_definition_fields(self):
        from scripts.historical_cache import _read_frame
        class Store:
            def to_df(self, **kwargs):
                assert kwargs == {'count': 10000, 'map_symbols': False}
                for stamp in ['2026-09-08T13:30:00Z', '2026-09-08T13:31:00Z']:
                    yield pd.DataFrame({'raw_symbol': ['AAA'], 'unused': [99]}, index=pd.to_datetime([stamp]))
        frame = _read_frame(Store(), 'definition')
        self.assertEqual(list(frame.columns), ['raw_symbol'])
        self.assertEqual(len(frame), 2)
        self.assertEqual(frame.index[0], pd.Timestamp('2026-09-08T13:30:00Z'))

    def test_incremental_catalog_never_downloads_or_uses_future_contracts(self):
        from datetime import date
        from unittest.mock import Mock
        from scripts.iv_expansion_history import _contracts_for_session
        rows = [{'ticker':'AAA','iv_chain':{'occ_root':'AAA','strikes':[100]},
                 'iv_expansion_reference':{'reference_session':'2026-09-04','option_expiry':'2026-09-11'}}]
        client, ledger = Mock(), Mock()
        with patch.dict('os.environ', {'SIGMA_BUCKET_CACHED_CONTRACTS_ONLY':'1'}):
            result = _contracts_for_session(client, ledger, rows, date(2026,9,2),
                       observation_session=date(2026,9,8), available_at=date(2026,9,4))
            self.assertEqual(result['AAA'][date(2026,9,11)][0]['strike'], 100)
            self.assertEqual(_contracts_for_session(client, ledger, rows, date(2026,9,2),
                       observation_session=date(2026,9,8), available_at=date(2026,9,3)), {})
        ledger.fetch_frame.assert_not_called()
        client.assert_not_called()
