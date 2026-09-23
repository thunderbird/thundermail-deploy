#!/bin/env python

"""
Stalwart suggests integrating internal Stalwart configurations with infrastructure as code tools
by writing an NDJSON file with all the right object settings in it. This is a file in which each
line is a single valid JSON object. You then provide this NDJSON file to stalwart-cli to apply the
changes.

This is fine, I guess, but it presents some challenges. For example, you cannot use indentation
and present objects across multiple lines, which dramatically increases the readability of these
files and a human's ability to modify them correctly.

Therefore, we have this script, which allows us to write the files in `stalwart-settings` in
regular JSON, indicating a square-bracket-enclosed array of the instructions you'd otherwise write
in the NDJSON file. This can be as readable as you like, as long as it parses. This script
converts the human-readable source JSON files into the more machine-readable NDJSON files.

This script is intended to be run from the root of this repo:

    ./util/convert-stalwart-settings-json.py
"""

import json
import os
from pathlib import Path

settings_dir = Path(os.getcwd()) / 'stalwart-settings'
json_files = list(settings_dir.glob('**.json'))

for json_file in json_files:
    ndjson_file = '.'.join([str(json_file).split('.')[0], 'ndjson'])
    output_lines = []
    with open(json_file, 'r') as source_file:
        source_content = json.loads(source_file.read())
        for command in source_content:
            if '__comment' in command:
                del(command['__comment'])
            output_lines.append(f"{json.dumps(command)}\n")

    print(output_lines)

    with open(ndjson_file, 'w') as output_file:
        output_file.writelines(output_lines)
