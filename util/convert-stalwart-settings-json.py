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

# Since our users are intelligent and are sure to have read the usage text above, they will
# certainly be running this script from the root of this repo. We can determine where our Stalwart
# configs live based on that. Make sure to only look at *.json files, ignoring the readme and the
# output of this code.
settings_dir = Path(os.getcwd()) / 'stalwart-settings'
json_files = list(settings_dir.glob('**.json'))

# Iterate over the JSON files we found
for json_file in json_files:
    # Determine what the output file will be called (just swap out the extension)
    ndjson_file = '.'.join([str(json_file).split('.')[0], 'ndjson'])
    output_lines = []
    with open(json_file, 'r') as source_file:
        # Read in the array of Stalwart API commands from that file
        source_content = json.loads(source_file.read())
        for command in source_content:
            # Allow for commentary at the root of each object
            if '__comment' in command:
                del(command['__comment'])
            # Reassemble the modified commands, minify them, adding newlines to them
            output_lines.append(f"{json.dumps(command)}\n")

    # Write out the text to the NDJSON file to be run against Stalwart
    with open(ndjson_file, 'w') as output_file:
        output_file.writelines(output_lines)
