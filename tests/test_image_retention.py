import unittest
from scripts.retain_production_images import obsolete_tags


class ImageRetentionTests(unittest.TestCase):
    def test_retains_active_rollback_and_all_container_references(self):
        tags = ['sigma-tape:active', 'sigma-tape:rollback', 'sigma-tape:old',
                'sigma-tape:in-use', 'sigma-tape-staging:old', 'postgres:16',
                'sigma-tape:<none>', '<none>:<none>']
        self.assertEqual(obsolete_tags(tags, ['sigma-tape:active', 'sigma-tape:rollback'],
                                      ['sigma-tape:in-use']), ['sigma-tape:old'])

    def test_unused_alias_can_be_removed_without_removing_retained_tag(self):
        self.assertEqual(obsolete_tags(['sigma-tape:active', 'sigma-tape:alias'],
                                      ['sigma-tape:active'], ['sigma-tape:active']),
                         ['sigma-tape:alias'])
