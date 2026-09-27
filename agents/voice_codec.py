"""Run the optional native SILK decoder in an isolated, time-bounded child."""
import sys
from pathlib import Path


def main():
    source, target = map(Path, sys.argv[1:3])
    if not 10 <= source.stat().st_size <= 16 * 1024 * 1024:
        raise ValueError('invalid voice size')
    import pilk
    pilk.decode(str(source), str(target), pcm_rate=24000)
    if not target.is_file() or target.stat().st_size == 0:
        raise ValueError('empty decoded audio')


if __name__ == '__main__':
    main()
