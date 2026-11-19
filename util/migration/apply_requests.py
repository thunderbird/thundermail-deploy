#!/usr/bin/env python3
import datetime
import re
import sys

LINE = re.compile(r'(\S+) \[debug\] (->|<-) (\S+) (\S+)')


def stamp(text):
    whole, _, frac = text.rstrip('Z').partition('.')
    return datetime.datetime.fromisoformat(whole) + datetime.timedelta(
        seconds=float('0.' + (frac or '0'))
    )


def main(path):
    start, worst, n = {}, 0.0, 0
    with open(path, encoding='utf-8', errors='replace') as fh:
        for line in fh:
            m = LINE.match(line)
            if not m:
                continue
            t, key = stamp(m.group(1)), (m.group(3), m.group(4))
            if m.group(2) == '->':
                start[key] = t
            elif key in start:
                n += 1
                worst = max(worst, (t - start.pop(key)).total_seconds())
    print('%d %.1f' % (n, worst))


if __name__ == '__main__':
    main(sys.argv[1])
