"""Keep CI strict about regressions while reporting explicitly accepted defects."""
import argparse
import json
from pathlib import Path
import re


def failures(report, known):
    """Return gate failures without changing the underlying test results."""
    if not isinstance(known, dict) or any(
        not isinstance(entry, dict) or type(entry.get('issue')) is not int
        or entry['issue'] < 1 or not isinstance(entry.get('signature'), str)
        or not re.fullmatch(r'[0-9a-f]{64}', entry['signature'])
        for entry in known.values()
    ):
        raise ValueError('Invalid known-failure list')
    checks = report.get('checks')
    if not isinstance(checks, list) or not checks:
        raise ValueError('Missing test results')
    errors, seen = [], set()
    for row in checks:
        check_id = row['id']
        if check_id in seen:
            raise ValueError('Duplicate check ID')
        seen.add(check_id)
        entry = known.get(check_id)
        if row['status'] == 'passed' and entry:
            errors.append(f'{check_id}: fixed; remove the known-failure entry for #{entry["issue"]}')
        elif row['status'] in {'passed', 'not_applicable'} and not entry:
            continue
        elif (entry and row['status'] == 'confirmed_failure'
              and row.get('consistent') is True
              and len(row.get('attempts', [])) >= 2
              and all(attempt.get('status') == 'failed'
                      and attempt.get('signature') == entry['signature']
                      for attempt in row['attempts'])):
            continue
        else:
            errors.append(f'{check_id}: {row["status"]} is not an accepted failure')
    errors.extend(f'{check_id}: known check missing from report' for check_id in sorted(known.keys() - seen))
    return errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('report', type=Path)
    args = parser.parse_args()
    known = json.loads(Path(__file__).with_name('known_failures.json').read_text(encoding='utf-8'))
    report = json.loads(args.report.read_text(encoding='utf-8'))
    errors = failures(report, known)
    print(json.dumps({'gate': 'failed' if errors else 'passed',
                      'known_issues': {key: entry['issue'] for key, entry in known.items()},
                      'errors': errors}, ensure_ascii=False, indent=2))
    return 1 if errors else 0


if __name__ == '__main__':
    raise SystemExit(main())
