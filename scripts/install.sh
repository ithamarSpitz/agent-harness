#!/bin/sh
set -eu

python -m pip install --user --upgrade .
python -P -m harness setup-machine
