#
This document describes Moonraker's full configuration.  As this file
references configuration for both Klipper (`printer.cfg`) and Moonraker
(`moonraker.conf`), each example contains a commment indicating which
configuration file is being refrenenced. 


## `[Timelapse]`
Generate Timelapse of a Print

This Component depends on FFMPEG and mjpegstreamer installed on the System which
is preinstalled in MainsailOs and FluidPI.
If not you can install it manually using this Guide:
https://github.com/cncjs/cncjs/wiki/Setup-Guide:-Raspberry-Pi-%7C-MJPEG-Streamer-Install-&-Setup-&-FFMpeg-Recording#mjpeg-streamer-install--setup

You may want to change your Webcamstream to higher resolution, 
depeding on your OS the mpjepg-streamer config file location differ:   
- MainsailOS: /boot/mainsail.txt   
- FluiddPI: 	/boot/fluiddpi.txt   

NOTE:   /boot is owned by root and can not editet as pi user!
        To edit it use ``sudo nano /boot/mainsail.txt`` via ssh
        or plug your sd card into your pc and edit it there.  

    
mjpeg-streamer options see:    
https://github.com/jacksonliam/mjpg-streamer/blob/master/mjpg-streamer-experimental/plugins/input_uvc/README.md


### Activate and configure the plugin adding following to your moonraker.conf:
```ini
# moonraker.conf

[timelapse]
#enabled: True
##   If this set to False the Gcode macros are ignored and
##   the autorender is disabled at the end of the print.
##   The idea is to disable the plugin by default and only activate 
##   it during runtime via the http endpoint if a timelapse is desired.
#autorender: True
##   If this is set to False, the autorender is disabled at the end of the print.
#constant_rate_factor: 23
##   The range of the CRF scale is 0–51, where 0 is lossless,
##   23 is the default, and 51 is worst quality possible. 
##   A lower value generally leads to higher quality, and a 
##   subjectively sane range is 17–28.
##   more info: https://trac.ffmpeg.org/wiki/Encode/H.264
#output_framerate: 30
##   Output framerate of the generated video
#output_path: ~/timelapse/
##   Path where the generated video will be saved
#frame_path: /tmp/timelapse/
##   Path where the temporary frames are saved
#time_format_code: %Y%m%d_%H%M
##   Manipulates datetime format of the output filename
##   see: https://docs.python.org/3/library/datetime.html#strftime-and-strptime-format-codes
#snapshoturl: http://localhost:8080/?action=snapshot
##   url to your webcamstream
#pixelformat: yuv420p
##   set pixelformat for output video
##   default to yuv420p because eg. yuvj422p will not play on 
##   on most smartphones or older media players
#extraoutputparams: 
##   here you can extra output parameters to FFMPEG 
##   further info: https://ffmpeg.org/ffmpeg.html 
##   eg rotate video by 180° "-vf transpose=2,transpose=2"
##   or repeat last frame for 5 seconds:
##   -filter_complex "[0]trim=0:5[hold];[0][hold]concat[extended];[extended][0]overlay"
```

### Define the Gcode Macro:
Include the macro file to your printer.cfg
```ini
# printer.cfg

[include timelapse.cfg]

```

### Add the macro to your Slicer:
Add the ``TIMELAPSE_TAKE_FRAME`` macro to your slicer so that it is added to the Gcode before or after a layer change.

Note: Also add the macro to your End G-code to get a extra "finished print"
frame.

#### Prusa Slicer
Printer Settings -> Custom G-code -> Before layer change Gcode -> ``TIMELAPSE_TAKE_FRAME``

![PrusaSlicer Configuration](assets/img/timelapse-PS-config.png)

#### Ultimaker Cura
Extensions -> Post Processing -> Modify G-Code ->   
Add a script -> Insert at layer change -> G-code to insert = ``TIMELAPSE_TAKE_FRAME``

![Cura Configuration](assets/img/timelapse-cura-config.png)
