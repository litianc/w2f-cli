import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from qa.ci_publish import publish_reports


class CIPublishingTests(unittest.TestCase):
    def reports(self, root):
        for platform in ('Linux', 'Windows'):
            directory = Path(root) / platform
            directory.mkdir()
            (directory / 'report.json').write_text(json.dumps({'platform': platform}), encoding='utf-8')

    def test_creation_budget_shared_between_platforms(self):
        with tempfile.TemporaryDirectory() as root:
            self.reports(root)
            with patch('qa.ci_publish.publish', side_effect=[
                [{'id': 'one', 'status': 'created'}], [{'id': 'one', 'status': 'duplicate'}]
            ]) as publish:
                results = publish_reports(root, 'owner/repo', 2)
            self.assertEqual([c.kwargs['max_new'] for c in publish.call_args_list], [2, 1])
            self.assertEqual(len(results), 2)

    def test_uncertain_write_stops_other_platforms(self):
        with tempfile.TemporaryDirectory() as root:
            self.reports(root)
            with patch('qa.ci_publish.publish', return_value=[{'id': 'one', 'status': 'error'}]) as publish:
                publish_reports(root, 'owner/repo')
            publish.assert_called_once()

    def test_missing_artifacts_is_not_success(self):
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaisesRegex(ValueError, 'No test reports'):
                publish_reports(root, 'owner/repo')
