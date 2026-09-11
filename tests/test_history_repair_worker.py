import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from scripts import history_repair_worker as worker


class HistoryRepairWorkerTests(unittest.TestCase):
    def test_closing_heap_is_replaced_before_calibration_starts(self):
        plan = {'iv_session':'2026-09-10', 'effective_session':'2026-09-11'}
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(worker.sys,'argv',['worker','--root',directory]), \
             patch.object(worker,'SharedCache',return_value=Mock(enabled=True)), \
             patch.object(worker,'sigma_plan',return_value=plan), \
             patch.object(worker,'restore_computed'), patch.object(worker,'restore_universe'), \
             patch.object(worker,'ExpectedSigma'), \
             patch.object(worker,'closing_references',return_value=(plan,{'AAA':{}})), \
             patch.object(worker,'ensure_bucket_statistics') as compute, \
             patch.object(worker.os,'execv') as replace:
            worker.main()
            compute.assert_not_called()
            self.assertEqual(replace.call_args.args[1][-3:],['--root',str(Path(directory).resolve()),'--calibration-only'])

    def test_calibration_phase_uses_validated_state_without_loading_close_inputs(self):
        state = {'today':{'anchor_date':'2026-09-10'},'screening_policy_fingerprint':'policy'}
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(worker.sys,'argv',['worker','--root',directory,'--calibration-only']), \
             patch.object(worker,'SharedCache',return_value=Mock(enabled=True)), \
             patch.object(worker,'sigma_plan',return_value={'iv_session':'2026-09-10'}), \
             patch.object(worker,'screening_policy_fingerprint',return_value='policy'), \
             patch.object(worker,'validate_bundle',side_effect=lambda value:value), \
             patch.object(worker,'ExpectedSigma') as close, \
             patch.object(worker,'ensure_bucket_statistics') as compute:
            (Path(directory)/'dashboard-state.json').write_text(json.dumps(state))
            worker.main()
            compute.assert_called_once_with(Path(directory).resolve(),state,allow_download=True)
            close.assert_not_called()
            state['today']['anchor_date']='2026-09-09'
            (Path(directory)/'dashboard-state.json').write_text(json.dumps(state))
            with self.assertRaisesRegex(RuntimeError,'input changed'):
                worker.main()
            self.assertEqual(compute.call_count,1)
