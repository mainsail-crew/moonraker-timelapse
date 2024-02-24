#!/bin/bash

set -e
#fails if camera is not connected
exec gphoto2 --get-config /main/imgsettings/imageformat >& /dev/null
