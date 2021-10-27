# Installation

This document provides a guide on how to install Moonraker-timelapse component.
As this is a plugin to Moonraker, Moonraker and klipper needs to be installed
before hand. 

## Installing the component
To install the Component you need to connect to your Raspberrypi via ssh and
execute following commands:

```
cd ~/
git clone https://github.com/mainsail-crew/moonraker-timelapse.git
bash ~/moonraker-timelapse/install.sh
```

This will clone the repository and execute the installer script.

## Updating

This repo can be updated with the update manager of Moonraker. To do so 
add following to your 'moonraker.conf' 

```
# moonraker.conf

[update_manager timelapse]
type: git_repo
primary_branch: main
path: /home/pi/moonraker-timelapse
origin: https://github.com/mainsail-crew/moonraker-timelapse.git
```

The script assumes that Klipper is also in your home directory under
"klipper": `${HOME}/klipper` and "moonraker": `${HOME}\moonraker`.

>:point_up: **NOTE:** Currently, there is a dummy systemd service installed
> to satisfy moonraker's update manager which also restarts Moonraker and Klipper.

# Configuration

Please see [configuration.md](configuration.md) for details on how to
configure the timelapse component.
