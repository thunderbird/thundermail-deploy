#!/usr/bin/env python3

"""In the `stalwart-settings/` directory, there are JSON files containing instructions on how to 
configure different Stalwart environments. The user supplies one of these files, and this script
converts it into an appropriately formatted .ndjson file to be applied to a server. This is
accomplished by mounting the .ndjson file to a `stalwart-cli` container on the relevant cluster.
Also mounted are arbitrary other files you may need to apply, like TLS certs and keys. The .ndjson
can refer to these things, and to environment variables, which carry the server connection details.

Example:
  python deploy-stalwart-settings.py \\
    --namespace thundermail-deployment \\
    --json ./stalwart-settings/thundermail-deployment.json \\
    --mount /tmp/ssl/domain.crt:/tmp/domain.crt \\
    --mount /tmp/ssl/domain.key:/tmp/domain.key \\
    --env-file .env

Notes:
  - You can't do local-Docker-style bind mounts with Kube containers, so we use a ConfigMap. Total
    file size must come in under 1 MiB (should be fine for our use cases).
  - The Pod and ConfigMap are both deleted after the run unless you pass --keep.
  - This script shells out `kubectl` commands, so users must have that installed and configured on
    their system already. They must have access to the Kube cluster the script is aimed at.
"""

import argparse
import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from random import choice


def parse_args():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument('--namespace', help='Target namespace')
    parser.add_argument(
        '--json', required=True, help='Local path to the JSON file to convert and apply'
    )
    parser.add_argument(
        '--ndjson-container-path',
        default='/tmp/apply.ndjson',
        help='Path in the container where the ndjson file should be mounted',
    )
    parser.add_argument(
        '--mount',
        action='append',
        default=[],
        metavar='HOST_FILE:CONTAINER_FILE',
        help='Mounts an arbitrary file on the host to an arbirtary location on the container',
    )
    parser.add_argument(
        '--env-file',
        help='Dotenv file for the pod environment. Should contain `STALWART_*` connection '
        'variables and anything else the NDJSON file requires.',
    )
    parser.add_argument(
        '--image',
        default='stalwartlabs/cli:latest',
        help='Image to run, which must contain stalwart-cli in the $PATH.',
    )
    parser.add_argument(
        '--name',
        default='stalwart-cli',
        help='Name prefix for the pod and configmap',
    )
    parser.add_argument(
        '--timeout',
        type=int,
        default=300,
        help='Seconds to wait for the pod to finish',
    )
    parser.add_argument(
        '--keep',
        action='store_true',
        help='Leave pod and configmap in place after the run. The user must manually clean these.',
    )
    parser.add_argument(
        '--extra-args',
        nargs='*',
        default=[],
        help='Extra args to pass to the `stalwart-cli apply` command. '
        'Must be the last arguments provided.',
    )
    return parser.parse_args()


def run_kubectl(ns, *args, stdin=None):
    """Executes the kubectl command to launch stalwart-cli. Waits for completion. Returns the
    command's output.
    """

    cmd = ['kubectl', '-n', ns, *args]
    proc = subprocess.run(cmd, input=stdin, capture_output=True, text=True)
    if proc.returncode != 0:
        sys.exit(
            f'error: kubectl exited {proc.returncode}\n$ {" ".join(cmd)}\n{proc.stderr.strip()}'
        )
    return proc.stdout


def parse_dotenv(path):
    """Reads a standard dotenv file from disk and extracts the actual key/value pairs from it."""

    env = {}
    for raw in Path(path).read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        if line.startswith('export '):
            line = line[len('export ') :].lstrip()
        key, delim, value = line.partition('=')
        if not delim or not key:
            sys.exit(f'Error reading environment file -- Bad line in {path}: {raw}')
        value = value.strip()
        # If the value is surrounded by quotation marks, strip them out
        if len(value) >= 2 and value[0] == value[-1] and value[0] in '"\'':
            value = value[1:-1]
        env[key.strip()] = value
    return env


def generate_random_id():
    """Simple random ID generator to use as container name suffixes."""
    alphabet = 'abcdefg0123456789'
    return ''.join([choice(alphabet) for i in range(6)])


def main():
    args = parse_args()

    # Quick-n-dirty way to conform --name argument to DNS-1123, since K8s resource names must
    arg_name = re.sub(r'[^a-zA-Z0-9]+', '-', args.name).lower()

    # Collect files as (configmap key, host path, container path).
    files = []

    def convert_json_to_ndjson(json_path: str, task_id: str, work_dir: Path):
        """Converts the JSON input to NDJSON output, removing commentary and special formatting.
        Returns the path of the generated NDJSON file.
        """

        # Create the ndjson file in the working directory
        filename = Path(json_path).parts[-1]
        ndjson_path = Path(f'{work_dir}/{filename}').with_suffix(f'.{task_id}.ndjson')
        print(f'Converting JSON file {json_path} to NDJSON file {ndjson_path}')

        output_lines = []
        with open(json_path, 'r') as source_file:
            # Read in the array of Stalwart API commands from that file
            source_content = json.loads(source_file.read())
            for command in source_content:
                # Remove special commentary field at the root of each object
                if '__comment' in command:
                    del command['__comment']
                # Reassemble the modified commands, minify them, adding newlines to them
                output_lines.append(f'{json.dumps(command)}\n')

        # Write out the text to the NDJSON file to be run against Stalwart
        with open(ndjson_path, 'w') as output_file:
            output_file.writelines(output_lines)

        return ndjson_path

    def add_file(host_path: str, container_path: str):
        """Adds the file found at host_path to the container at container_path."""

        # Validate the host file
        host_path = Path(host_path).expanduser().resolve()
        if not host_path.is_file():
            sys.exit(f'Error -- File not found: {host_path}')

        # Ensure no other file entry indicates the same destination on the container
        container_path = (container_path or str(host_path)).rstrip('/')
        if container_path in [f[1] for f in files]:
            sys.exit(
                f"Error -- Duplicate target '{container_path}' across mounts. "
                'Use distinct container paths'
            )
        
        # `files` contains tuples with the following parts:
        #   - The key that labels this file within the ConfigMap
        #   - The path on the machine this script runs on where the file can be found
        #   - The path on the container where the file should be mounted
        cm_key = host_path.parts[-1]
        files.append((cm_key, host_path, container_path))

    # --- Validate, process inputs

    # To avoid collisions when a user has used the --keep options, append a random ID to our K8s
    # resource names. Use the same ID for both the ConfigMap and the Pod so we know they are paired.
    task_id = generate_random_id()
    cm_name = f'{arg_name}-files-{task_id}'
    pod_name = f'{arg_name}-{task_id}'

    print(f'Running with task_id {task_id}')

    # Create a temporary directory for our working files
    work_dir = Path(f'./workdir-{task_id}')
    if not work_dir.exists():
        work_dir.mkdir()
        print(f'Created working directory {work_dir}')
    else:
        print(f'Working directory {work_dir} already exists')

    # Convert the given JSON file to NDJSON and add that output file to the list
    ndjson_path = convert_json_to_ndjson(args.json, task_id, work_dir)
    add_file(ndjson_path, args.ndjson_container_path)

    # Add each additionally specified file as well
    for spec in args.mount:
        host_path, delim, cont_path = spec.rpartition(':')
        if not delim:
            sys.exit(f'Error -- Colon (":") delimiter not found in mount spec: {spec}')
        add_file(host_path, cont_path)

    # Read the environment from a file
    env = parse_dotenv(args.env_file) if args.env_file else {}

    # --- Create ConfigMap to mount files through

    # Create a ConfigMap with a kubectl command, specifying each file to mount. --dry-run prevents
    # changes. Validation happens client-side. "-o yaml" produces the YAML manifest.
    cm_create_args = ['create', 'configmap', cm_name, '--dry-run=client', '-o', 'yaml']
    for cm_key, host_path, _ in files:
        # Use the filename without the full path as the key in the ConfigMap
        cm_create_args.append(f'--from-file={cm_key}={host_path}')
    cm_manifest = run_kubectl(args.namespace, *cm_create_args)

    cm_manifest_file = f'{work_dir}/configmap.yaml'
    with open(cm_manifest_file, 'w') as fh:
        fh.write(cm_manifest)

    # Apply the generated (and validated) manifest server-side with another kubectl run.
    print(f'Applying ConfigMap from manifest {cm_manifest_file}')
    run_kubectl(args.namespace, 'apply', '-f', '-', stdin=cm_manifest)

    # --- Run stalwart-cli Pod

    # Define the parameters of the stalwart-cli container
    container = {
        'name': 'apply',
        'image': args.image,
        'command': [
            'stalwart-cli',
            'apply',
            '--file',
            args.ndjson_container_path,
            *args.extra_args,
        ],
        'volumeMounts': [],
    }

    # Add volumeMounts for each file to mount
    for cm_key, host_path, container_path in files:
        container['volumeMounts'].append({
            'name': 'files', # We hardcode this name for all CM'd files below
            'subPath': cm_key, # Key in the ConfigMap with the content
            'mountPath': container_path, # Path on container to mount file to
        })
    
    # Add any env-vars from the dotenv file
    if env:
        container['env'] = [{'name': k, 'value': v} for k, v in env.items()]

    # Define a pod manifest to run the container, then apply it.
    # This triggers execution of the stalwart-cli command as configured.
    pod = json.dumps({
        'apiVersion': 'v1',
        'kind': 'Pod',
        'metadata': {'name': pod_name, 'namespace': args.namespace},
        'spec': {
            # Failures don't trigger a retry
            'restartPolicy': 'Never',
            'containers': [container],
            'volumes': [{'name': 'files', 'configMap': {'name': cm_name}}],
        },
    })

    pod_manifest_file = f'{work_dir}/pod.yaml'
    with open(pod_manifest_file, 'w') as fh:
        fh.write(pod)

    print(f'Applying Pod from manifest {pod_manifest_file}')
    run_kubectl(args.namespace, 'apply', '-f', '-', stdin=pod)

    # Monitor the running pod, waiting for it to end. Detect success or failure.
    ok = False
    phase = ''
    deadline = time.time() + args.timeout

    # Poll for pod phase every other second
    while time.time() < deadline:
        phase = run_kubectl(
            args.namespace, 'get', 'pod', pod_name, '-o', 'jsonpath={.status.phase}'
        ).strip()
        if phase in ('Succeeded', 'Failed'):
            break
        time.sleep(2)

    if phase not in ('Succeeded', 'Failed'):
        print(
            f'Error -- pod did not finish within {args.timeout}s (phase: {phase or "unknown"})',
            file=sys.stderr,
        )
        print(f'To debug: kubectl -n {args.namespace} describe pod {pod_name}', file=sys.stderr)
    else:
        # If the pod succeeded, try to pull its logs
        ok = phase == 'Succeeded'
        try:
            logs = run_kubectl(args.namespace, 'logs', pod_name).strip()
        except SystemExit as e:  # pod never started (e.g. image pull failure)
            print(f'Warning -- Could not read logs ({e})', file=sys.stderr)
        else:
            if logs:
                print(logs)
        
        # Report failures
        if not ok:
            print(
                f'\nFAILED -- Inspect with: kubectl -n {args.namespace} describe pod {pod_name}',
                file=sys.stderr,
            )

    
    # --- Cleanup

    if not args.keep:
        # Clean up the pod and config map
        print(f'Cleaning up Pod {pod_name} and ConfigMap {cm_name}')
        for kind, name in (('pod', pod_name), ('configmap', cm_name)):
            try:
                run_kubectl(args.namespace, 'delete', kind, name, '--ignore-not-found')
            except SystemExit:
                pass

        # Clean up the working files
        print(f'Cleaning up working directory {work_dir}')
        shutil.rmtree(str(work_dir))
    else:
        print('The --keep option was provided. To clean up from this execution, run:')
        print(f'kubectl -n {args.namespace} delete cm {cm_name}')
        print(f'kubectl -n {args.namespace} delete pod {pod_name}')
        print(f'rm -rf {work_dir}')
       

    sys.exit(0 if ok else 1)


if __name__ == '__main__':
    main()
