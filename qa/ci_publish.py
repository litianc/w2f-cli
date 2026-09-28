"""Serialize publishing across platform reports with one global creation budget."""
import argparse
import json
from pathlib import Path

from qa.issues import publish


def publish_reports(directory, repo, max_new=3):
    remaining = max_new
    results = []
    for path in sorted(Path(directory).glob('**/report.json')):
        report = json.loads(path.read_text(encoding='utf-8'))
        decisions = publish(report, repo, max_new=remaining)
        remaining -= sum(item['status'] == 'created' for item in decisions)
        results.extend({'platform': report.get('platform'), **item} for item in decisions
                       if item['status'] != 'skipped')
        if any(item['status'] == 'error' for item in decisions):
            break  # An uncertain write must not be retried via another platform.
    if not results and not list(Path(directory).glob('**/report.json')):
        raise ValueError('No test reports found')
    return results


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('directory', type=Path)
    parser.add_argument('--repo', default='litianc/w2f-cli')
    parser.add_argument('--max-new', type=int, default=3)
    args = parser.parse_args()
    results = publish_reports(args.directory, args.repo, args.max_new)
    print(json.dumps(results, ensure_ascii=False, indent=2))
    return 1 if any(item['status'] == 'error' for item in results) else 0


if __name__ == '__main__':
    raise SystemExit(main())
