#!/bin/bash
set -e
BASEFILE=${1%%.jpg}

# note: make sure that you're camera just creates small/medium jpg and no raws or
# you're raspberrypi will habe all lot of data to process

gphoto2 --quiet --capture-image-and-download --filename="$BASEFILE.%C" --force-overwrite
