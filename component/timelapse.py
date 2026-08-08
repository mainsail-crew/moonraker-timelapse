# Moonraker Timelapse component
#
# Copyright (C) 2021 Christoph Frei <fryakatkop@gmail.com>
#
# This file may be distributed under the terms of the GNU GPLv3 license.
from __future__ import annotations
import logging
import os
import glob
import re
import shlex
import shutil
import asyncio
from datetime import datetime
from tornado.ioloop import IOLoop
from zipfile import ZipFile

# Annotation imports
from typing import (
    TYPE_CHECKING,
    Dict,
    Any,
    Callable,
    Optional,
    List
)
if TYPE_CHECKING:
    from confighelper import ConfigHelper
    from .webcam import WebcamManager, WebCam
    from websockets import WebRequest
    from . import shell_command
    from . import klippy_apis
    from . import database

    APIComp = klippy_apis.KlippyAPI
    SCMDComp = shell_command.ShellCommandFactory
    DBComp = database.MoonrakerDatabase


class TimelapseGcodeService:

    def __init__(self, owner: Timelapse) -> None:
        self.owner = owner

    async def setgcodevariables(self) -> None:
        gcommand = "_SET_TIMELAPSE_SETUP " \
            + f" ENABLE={self.owner.config['enabled']}" \
            + f" VERBOSE={self.owner.config['gcode_verbose']}" \
            + f" PARK_ENABLE={self.owner.config['parkhead']}" \
            + f" PARK_POS={self.owner.config['parkpos']}" \
            + f" CUSTOM_POS_X={self.owner.config['park_custom_pos_x']}" \
            + f" CUSTOM_POS_Y={self.owner.config['park_custom_pos_y']}" \
            + f" CUSTOM_POS_DZ={self.owner.config['park_custom_pos_dz']}" \
            + f" TRAVEL_SPEED={self.owner.config['park_travel_speed']}" \
            + f" RETRACT_SPEED={self.owner.config['park_retract_speed']}" \
            + f" EXTRUDE_SPEED={self.owner.config['park_extrude_speed']}" \
            + " RETRACT_DISTANCE=" \
            + f"{self.owner.config['park_retract_distance']}" \
            + " EXTRUDE_DISTANCE=" \
            + f"{self.owner.config['park_extrude_distance']}" \
            + f" PARK_TIME={self.owner.config['park_time']}" \
            + f" FW_RETRACT={self.owner.config['fw_retract']}" \

        logging.debug(f"run gcommand: {gcommand}")
        try:
            await self.owner.klippy_apis.run_gcode(gcommand)
        except self.owner.server.error:
            msg = f"Error executing GCode {gcommand}"
            logging.exception(msg)

    async def release_parkedhead(self) -> None:
        gcommand = "SET_GCODE_VARIABLE " \
            + "MACRO=TIMELAPSE_TAKE_FRAME " \
            + "VARIABLE=takingframe VALUE=False"

        logging.debug(f"run gcommand: {gcommand}")
        try:
            await self.owner.klippy_apis.run_gcode(gcommand)
        except self.owner.server.error:
            msg = f"Error executing GCode {gcommand}"
            logging.exception(msg)

    async def start_hyperlapse(self) -> None:
        hyperlapse_cycle = self.owner.config['hyperlapse_cycle']
        park_time = self.owner.config['park_time']
        timediff = hyperlapse_cycle - park_time
        if timediff >= 1:
            gcommand = "HYPERLAPSE ACTION=START" \
                       + f" CYCLE={hyperlapse_cycle}"

            logging.debug(f"run gcommand: {gcommand}")
            try:
                await self.owner.klippy_apis.run_gcode(gcommand)
            except self.owner.server.error:
                msg = f"Error executing GCode {gcommand}"
                logging.exception(msg)
            self.owner.hyperlapserunning = True
        else:
            logging.info("WARNING: Blocked start of Hyperlapse, because "
                         f"hyperlapse_cycle ({hyperlapse_cycle}s) is smaller "
                         f"then or to close to park_time ({park_time}s)"
                         )

    async def stop_hyperlapse(self) -> None:
        gcommand = "HYPERLAPSE ACTION=STOP"

        logging.debug(f"run gcommand: {gcommand}")
        try:
            await self.owner.klippy_apis.run_gcode(gcommand)
        except self.owner.server.error:
            msg = f"Error executing GCode {gcommand}"
            logging.exception(msg)
        self.owner.hyperlapserunning = False


class TimelapseFrameService:

    def __init__(self, owner: Timelapse) -> None:
        self.owner = owner

    def seed_framecount(self) -> int:
        highest_frame = 0
        for frame_path in glob.glob(self.owner.temp_dir + "frame*.jpg"):
            frame_name = os.path.basename(frame_path)
            match = re.fullmatch(r"frame(\d+)\.jpg", frame_name)
            if match:
                highest_frame = max(highest_frame, int(match.group(1)))
        return highest_frame

    async def newframe(self) -> None:
        # make sure webcamconfig is uptodate before grabbing a new frame
        await self.owner.getWebcamConfig()

        options = ""
        if self.owner.wget_skip_cert:
            options += "--no-check-certificate "

        self.owner.framecount += 1
        framefile = "frame" + str(self.owner.framecount).zfill(6) + ".jpg"
        output_path = self.owner.temp_dir + framefile
        cmd = "wget " + options \
            + shlex.quote(self.owner.config['snapshoturl']) \
            + " -O " + shlex.quote(output_path)
        self.owner.lastframefile = framefile
        logging.debug(f"cmd: {cmd}")

        shell_cmd: SCMDComp = self.owner.server.lookup_component(
            'shell_command')
        scmd = shell_cmd.build_shell_command(cmd, None)
        cmdstatus = False
        try:
            cmdstatus = await scmd.run(timeout=self.owner.wget_timeout,
                                       verbose=False)
        except Exception:
            logging.exception(f"Error running cmd '{cmd}'")

        result = {'action': 'newframe'}
        if cmdstatus:
            result.update({
                'frame': str(self.owner.framecount),
                'framefile': framefile,
                'status': 'success'
            })
        else:
            logging.info(f"getting newframe failed: {cmd}")
            self.owner.framecount -= 1
            result.update({'status': 'error'})

        self.owner.notify_event(result)
        self.owner.takingframe = False

    def cleanup(self) -> None:
        logging.debug("cleanup frame directory")
        filelist = glob.glob(self.owner.temp_dir + "frame*.jpg")
        if filelist:
            for filepath in filelist:
                os.remove(filepath)
        self.owner.framecount = 0
        self.owner.lastframefile = ""

    async def save_frames_zip(self, webrequest=None):
        filelist = sorted(glob.glob(self.owner.temp_dir + "frame*.jpg"))
        self.owner.framecount = len(filelist)
        result = {'action': 'saveframes'}

        if not filelist:
            msg = "no frames to save, skip"
            status = "skipped"
        elif self.owner.saveisrunning:
            msg = "saving frames already"
            status = "running"
        else:
            self.owner.saveisrunning = True

            # get printed filename
            kresult = await self.owner.klippy_apis.query_objects(
                {'print_stats': None})
            pstats = kresult.get("print_stats", {})
            gcodefilename = pstats.get("filename", "").split("/")[-1]

            # prepare output filename
            now = datetime.now()
            date_time = now.strftime(self.owner.config['time_format_code'])
            outfile = f"timelapse_{gcodefilename}_{date_time}"
            outfileFull = outfile + "_frames.zip"

            with ZipFile(self.owner.out_dir + outfileFull, "w") as zipObj:
                for frame in filelist:
                    zipObj.write(frame, frame.split("/")[-1])

            logging.info(f"saved frames: {outfile}_frames.zip")

            result.update({
                'status': 'finished',
                'zipfile': outfileFull
            })

            self.owner.saveisrunning = False

        return result


class TimelapseRenderService:

    def __init__(self, owner: Timelapse) -> None:
        self.owner = owner

    def _build_render_filter_param(self) -> str:
        filterParam = ""
        rotation = self.owner.config['rotation']
        flip_x = self.owner.config['flip_x']
        flip_y = self.owner.config['flip_y']
        if rotation == 90 and flip_y:
            filterParam = " -vf 'transpose=3'"
        elif rotation == 90:
            filterParam = " -vf 'transpose=1'"
        elif rotation == 180:
            filterParam = " -vf 'hflip,vflip'"
        elif rotation == 270 and flip_y:
            filterParam = " -vf 'transpose=0'"
        elif rotation == 270:
            filterParam = " -vf 'transpose=2'"
        elif rotation > 0:
            pi = 3.141592653589793
            rot = str(rotation*(pi/180))
            filterParam = " -vf 'rotate=" + rot + "'"
        elif flip_x and flip_y:
            filterParam = " -vf 'hflip,vflip'"
        elif flip_x:
            filterParam = " -vf 'hflip'"
        elif flip_y:
            filterParam = " -vf 'vflip'"
        return filterParam

    def _build_ffmpeg_video_cmd(self,
                                fps: int,
                                inputfiles: str,
                                filter_param: str,
                                output_path: str
                                ) -> str:
        return self.owner.ffmpeg_binary_path \
            + " -r " + str(fps) \
            + " -i '" + inputfiles + "'" \
            + filter_param \
            + " -threads 2 -g 5" \
            + " -crf " + str(self.owner.config['constant_rate_factor']) \
            + " -vcodec libx264" \
            + " -pix_fmt " + self.owner.config['pixelformat'] \
            + " -an" \
            + " " + self.owner.config['extraoutputparams'] \
            + " '" + output_path + "' -y"

    def _build_ffmpeg_preview_cmd(self,
                                  preview_file_path: str,
                                  filter_param: str
                                  ) -> str:
        return self.owner.ffmpeg_binary_path \
            + " -i '" + preview_file_path + "'" \
            + filter_param \
            + " -an" \
            + " " + self.owner.config['extraoutputparams'] \
            + " '" + preview_file_path + "' -y"

    async def _run_ffmpeg_cmd(self,
                              cmd: str,
                              cb: Optional[Callable] = None,
                              verbose: bool = True,
                              log_complete: bool = False,
                              timeout: float = 9999999999,
                              ) -> bool:
        shell_cmd: SCMDComp = self.owner.server.lookup_component(
            'shell_command')
        scmd = shell_cmd.build_shell_command(cmd, cb)
        try:
            return await scmd.run(verbose=verbose,
                                  log_complete=log_complete,
                                  timeout=timeout,
                                  )
        except Exception:
            logging.exception(f"Error running cmd '{cmd}'")
            return False

    async def render(self, webrequest=None):
        filelist = sorted(glob.glob(self.owner.temp_dir + "frame*.jpg"))
        self.owner.framecount = len(filelist)
        result = {'action': 'render'}

        # make sure webcamconfig is uptodate for the rotation/flip feature
        await self.owner.getWebcamConfig()

        if not filelist:
            msg = "no frames to render, skip"
            status = "skipped"
        elif self.owner.renderisrunning:
            msg = "render is already running"
            status = "running"
        elif not self.owner.ffmpeg_installed:
            msg = (f"{self.owner.ffmpeg_binary_path} not found, "
                   "please install ffmpeg")
            status = "error"
            logging.info(f"timelapse: {msg}")
        else:
            self.owner.renderisrunning = True

            # get printed filename
            kresult = await self.owner.klippy_apis.query_objects(
                {'print_stats': None})
            pstats = kresult.get("print_stats", {})
            gcodefilename = pstats.get("filename", "").split("/")[-1]

            # prepare output filename
            now = datetime.now()
            date_time = now.strftime(self.owner.config['time_format_code'])
            inputfiles = self.owner.temp_dir + "frame%6d.jpg"
            outfile = f"timelapse_{gcodefilename}_{date_time}"
            output_path = self.owner.temp_dir + outfile + ".mp4"

            # dublicate last frame
            duplicates: List[str] = []
            if self.owner.config['duplicatelastframe'] > 0:
                lastframe = filelist[-1:][0]

                for i in range(self.owner.config['duplicatelastframe']):
                    nextframe = str(self.owner.framecount + i + 1).zfill(6)
                    duplicate = "frame" + nextframe + ".jpg"
                    duplicatePath = self.owner.temp_dir + duplicate
                    duplicates.append(duplicatePath)
                    try:
                        shutil.copy(lastframe, duplicatePath)
                    except OSError as err:
                        logging.info(f"duplicating last frame failed: {err}")

                filelist = sorted(
                    glob.glob(self.owner.temp_dir + "frame*.jpg"))
                self.owner.framecount = len(filelist)

            # variable framerate
            if self.owner.config['variable_fps']:
                fps = int(self.owner.framecount /
                          self.owner.config['targetlength'])
                fps = max(min(fps,
                              self.owner.config['variable_fps_max']),
                          self.owner.config['variable_fps_min'])
            else:
                fps = self.owner.config['output_framerate']

            filterParam = self._build_render_filter_param()
            cmd = self._build_ffmpeg_video_cmd(
                fps, inputfiles, filterParam, output_path)

            logging.info(f"start FFMPEG: {cmd}")
            self.owner.lastrenderprogress = 0
            self.owner.lastcmdreponse = ""
            result.update({
                'status': 'started',
                'framecount': str(self.owner.framecount),
                'settings': {
                    'framerate': fps,
                    'crf': self.owner.config['constant_rate_factor'],
                    'pixelformat': self.owner.config['pixelformat']
                }
            })

            self.owner.notify_event(result)
            cmdstatus = await self._run_ffmpeg_cmd(cmd, self.ffmpeg_cb)

            if cmdstatus:
                status = "success"
                msg = f"Rendering Video successful: {outfile}.mp4"
                result.update({
                    'filename': f"{outfile}.mp4",
                    'printfile': gcodefilename
                })
                result.pop("settings")

                try:
                    shutil.move(output_path,
                                self.owner.out_dir + outfile + ".mp4")
                except OSError as err:
                    logging.info(f"moving output file failed: {err}")

                if self.owner.config['previewimage']:
                    previewFile = f"{outfile}.jpg"
                    previewFilePath = self.owner.out_dir + previewFile
                    previewSrc = filelist[-1:][0]
                    try:
                        shutil.copy(previewSrc, previewFilePath)
                    except OSError as err:
                        logging.info(f"copying preview image failed: {err}")
                    else:
                        result.update({'previewimage': previewFile})

                    if filterParam or self.owner.config['extraoutputparams']:
                        cmd = self._build_ffmpeg_preview_cmd(previewFilePath,
                                                             filterParam)
                        logging.info(f"Rotate preview image cmd: {cmd}")
                        await self._run_ffmpeg_cmd(cmd)

            else:
                status = "error"
                msg = (f"Rendering Video failed: {cmd} : "
                       f"{self.owner.lastcmdreponse}")
                result.update({
                    'cmd': cmd,
                    'cmdresponse': self.owner.lastcmdreponse
                })

            self.owner.renderisrunning = False

            if duplicates:
                for dupe in duplicates:
                    try:
                        os.remove(dupe)
                    except OSError as err:
                        logging.info(f"remove duplicate failed: {err}")

        logging.info(msg)
        result.update({
            'status': status,
            'msg': msg
        })
        self.owner.notify_event(result)

        if self.owner.byrendermacro:
            gcommand = "SET_GCODE_VARIABLE " \
                       + "MACRO=TIMELAPSE_RENDER VARIABLE=render VALUE=False"
            logging.debug(f"run gcommand: {gcommand}")
            try:
                await self.owner.klippy_apis.run_gcode(gcommand)
            except self.owner.server.error:
                msg = f"Error executing GCode {gcommand}"
                logging.exception(msg)
            self.owner.byrendermacro = False

        return result

    def ffmpeg_cb(self, response):
        self.owner.lastcmdreponse = response.decode("utf-8")
        try:
            frame = re.search(
                r'(?<=frame=)*(\d+)(?=.+fps)', self.owner.lastcmdreponse
            ).group()
        except AttributeError:
            return
        percent = int(frame) / self.owner.framecount * 100
        if percent > 100:
            percent = 100

        if self.owner.lastrenderprogress != int(percent):
            self.owner.lastrenderprogress = int(percent)
            result = {
                'action': 'render',
                'status': 'running',
                'progress': self.owner.lastrenderprogress
            }
            self.owner.notify_event(result)


class TimelapseConfigService:

    def __init__(self, owner: Timelapse) -> None:
        self.owner = owner

    def overwrite_dbconfig_with_confighelper(self) -> None:
        blockedsettings = []

        for config in self.owner.confighelper.get_options():
            if config in self.owner.config:
                configtype = type(self.owner.config[config])
                if configtype == str:
                    self.owner.config[config] = self.owner.confighelper.get(
                        config)
                elif configtype == bool:
                    self.owner.config[config] = \
                        self.owner.confighelper.getboolean(config)
                elif configtype == int:
                    self.owner.config[config] = self.owner.confighelper.getint(
                        config)
                elif configtype == float:
                    self.owner.config[config] = \
                        self.owner.confighelper.getfloat(config)

                blockedsettings.append(config)

        self.owner.config.update({'blockedsettings': blockedsettings})
        logging.debug(
            f"blockedsettings {self.owner.config['blockedsettings']}")

    async def get_webcam_config(self) -> None:
        webcam_name = self.owner.config['camera']
        try:
            wcmgr: WebcamManager = self.owner.server.lookup_component("webcam")
            cams = wcmgr.get_webcams()

            if not cams:
                logging.info("WARNING: no camera configured, " +
                             "using the fallback config")
                fallback = {'snapshot_url': self.owner.config['snapshoturl'],
                            'rotation': self.owner.config['rotation'],
                            'flip_horizontal': self.owner.config['flip_x'],
                            'flip_vertical': self.owner.config['flip_y']
                            }
                self.parse_webcam_config(fallback)
                return

            if webcam_name and webcam_name in cams:
                camera = cams[webcam_name]
            else:
                camera = list(cams.values())[0]

            self.parse_webcam_config(camera.as_dict())

        except Exception as e:
            logging.info(f"something went wrong getting"
                         f"Cam Camera:{webcam_name} from Database. "
                         f"Exception: {e}"
                         )

    def parse_webcam_config(self, webcamconfig) -> None:
        snapshoturl = webcamconfig['snapshot_url']
        flip_x = webcamconfig['flip_horizontal']
        flip_y = webcamconfig['flip_vertical']
        rotation = webcamconfig['rotation']

        oldWebcamConfig = {"url": self.owner.config['snapshoturl'],
                           "flip_x": self.owner.config['flip_x'],
                           "flip_y": self.owner.config['flip_y'],
                           "rotation": self.owner.config['rotation']
                           }

        self.owner.config['snapshoturl'] = self.owner.confighelper.get(
            'snapshoturl', snapshoturl)
        self.owner.config['flip_x'] = self.owner.confighelper.getboolean(
            'flip_x', flip_x)
        self.owner.config['flip_y'] = self.owner.confighelper.getboolean(
            'flip_y', flip_y)
        self.owner.config['rotation'] = self.owner.confighelper.getint(
            'rotation', rotation)

        if not self.owner.config['snapshoturl'].startswith('http'):
            if not self.owner.config['snapshoturl'].startswith('/'):
                self.owner.config['snapshoturl'] = (
                    "http://localhost/" + self.owner.config['snapshoturl'])
            else:
                self.owner.config['snapshoturl'] = (
                    "http://localhost" + self.owner.config['snapshoturl'])

        newWebcamConfig = {"url": self.owner.config['snapshoturl'],
                           "flip_x": self.owner.config['flip_x'],
                           "flip_y": self.owner.config['flip_y'],
                           "rotation": self.owner.config['rotation']
                           }

        if not oldWebcamConfig == newWebcamConfig:
            logging.info("snapshoturl: "
                         f"{self.owner.config['snapshoturl']}, "
                         f"Flip V/H: {self.owner.config['flip_y']}/"
                         f"{self.owner.config['flip_y']}, "
                         f"rotation: {self.owner.config['rotation']}"
                         )

    async def webrequest_settings(self,
                                  webrequest: WebRequest
                                  ) -> Dict[str, Any]:
        action = webrequest.get_action()
        if action == 'POST':

            args = webrequest.get_args()
            logging.debug("webreq_args: " + str(args))

            gcodechange = False
            settingsWithGcodechange = [
                'enabled', 'parkhead',
                'parkpos', 'park_custom_pos_x',
                'park_custom_pos_y', 'park_custom_pos_dz',
                'park_travel_speed', 'park_retract_speed',
                'park_extrude_speed', 'park_retract_distance',
                'park_extrude_distance', 'park_time', 'fw_retract'
            ]
            modechanged = False

            for setting in args:
                if setting in self.owner.config:
                    settingtype = type(self.owner.config[setting])
                    if setting == "snapshoturl":
                        logging.debug(
                            "snapshoturl cannot be changed via webrequest")
                        continue
                    elif setting in self.owner.config['blockedsettings']:
                        logging.debug(
                            f"timelapse: {setting} is set in moonraker.conf "
                            f"and cannot be changed via webrequest")
                        continue
                    elif settingtype == str:
                        settingvalue = webrequest.get(setting)
                    elif settingtype == bool:
                        settingvalue = webrequest.get_boolean(setting)
                    elif settingtype == int:
                        settingvalue = webrequest.get_int(setting)
                    elif settingtype == float:
                        settingvalue = webrequest.get_float(setting)
                    else:
                        # would otherwise fall through holding the previous
                        # key's value, or unbound if it came first
                        logging.debug(
                            f"timelapse: ignoring {setting}, no conversion "
                            f"for {settingtype}")
                        continue

                    self.owner.config[setting] = settingvalue

                    self.owner.database.insert_item(
                        "timelapse",
                        f"config.{setting}",
                        settingvalue
                    )

                    if setting == "camera":
                        if not self.owner.noWebcamDb:
                            await self.get_webcam_config()
                        else:
                            logging.info("Webcam Namespace not intialized, "
                                         "please restart moonraker service!")

                    if setting in settingsWithGcodechange:
                        gcodechange = True

                    if setting == "mode":
                        modechanged = True

                    logging.debug(f"changed setting: {setting} "
                                  f"value: {settingvalue} "
                                  f"type: {settingtype}"
                                  )

            if modechanged:
                if self.owner.config['mode'] == "hyperlapse":
                    if not self.owner.hyperlapserunning:
                        if self.owner.printing:
                            ioloop = IOLoop.current()
                            ioloop.spawn_callback(self.owner.start_hyperlapse)
                else:
                    if self.owner.hyperlapserunning:
                        ioloop = IOLoop.current()
                        ioloop.spawn_callback(self.owner.stop_hyperlapse)
            if gcodechange:
                ioloop = IOLoop.current()
                ioloop.spawn_callback(self.owner.setgcodevariables)

        return self.owner.config


class Timelapse:

    def __init__(self, confighelper: ConfigHelper) -> None:

        # setup vars
        self.renderisrunning = False
        self.saveisrunning = False
        self.takingframe = False
        self.framecount = 0
        self.lastframefile = ""
        self.lastrenderprogress = 0
        self.lastcmdreponse = ""
        self.byrendermacro = False
        self.hyperlapserunning = False
        self.printing = False
        self.noWebcamDb = False

        self.confighelper = confighelper
        self.server = confighelper.get_server()
        self.klippy_apis: APIComp = self.server.lookup_component('klippy_apis')
        self.database: DBComp = self.server.lookup_component("database")

        # setup static (nonDB) settings
        out_dir_cfg = confighelper.get(
            "output_path", "~/timelapse/")
        temp_dir_cfg = confighelper.get(
            "frame_path", "/tmp/timelapse/")
        self.ffmpeg_binary_path = confighelper.get(
            "ffmpeg_binary_path", "/usr/bin/ffmpeg")
        self.wget_skip_cert = confighelper.getboolean(
            "wget_skip_cert_check", False)
        self.wget_timeout = confighelper.getfloat("wget_timeout", 2.0)

        # Setup default config
        self.config: Dict[str, Any] = {
            'enabled': True,
            'mode': "layermacro",
            'camera': "",
            'snapshoturl': "http://localhost:8080/?action=snapshot",
            'stream_delay_compensation': 0.05,
            'gcode_verbose': False,
            'parkhead': False,
            'parkpos': "back_left",
            'park_custom_pos_x': 10.0,
            'park_custom_pos_y': 10.0,
            'park_custom_pos_dz': 0.0,
            'park_travel_speed': 100,
            'park_retract_speed': 15,
            'park_extrude_speed': 15,
            'park_retract_distance': 1.0,
            'park_extrude_distance': 1.0,
            'park_time': 0.1,
            'fw_retract': False,
            'hyperlapse_cycle': 30,
            'autorender': True,
            'constant_rate_factor': 23,
            'output_framerate': 30,
            'pixelformat': "yuv420p",
            'time_format_code': "%Y%m%d_%H%M",
            'extraoutputparams': "",
            'variable_fps': False,
            'targetlength': 10,
            'variable_fps_min': 5,
            'variable_fps_max': 60,
            'rotation': 0,
            'flip_x': False,
            'flip_y': False,
            'duplicatelastframe': 5,
            'previewimage': True,
            'saveframes': False
        }

        # Get Config from Database and overwrite defaults
        dbconfig: Dict[str, Any] = self.database.get_item("timelapse",
                                                          "config",
                                                          self.config)
        if isinstance(dbconfig, asyncio.Future):
            self.config.update(dbconfig.result())
        else:
            self.config.update(dbconfig)

        # setup internal config service
        self.config_service = TimelapseConfigService(self)

        # Overwrite Config with fixed config made in moonraker.conf
        # this is a fallback to older setups and when the Frontend doesn't
        # support the settings endpoint
        self.overwriteDbconfigWithConfighelper()

        # check if ffmpeg is installed
        self.ffmpeg_installed = os.path.isfile(self.ffmpeg_binary_path)
        if not self.ffmpeg_installed:
            self.config['autorender'] = False
            logging.info(f"timelapse: {self.ffmpeg_binary_path} \
                        not found please install to use render functionality")

        # setup directories
        # remove trailing "/"
        out_dir_cfg = os.path.join(out_dir_cfg, '')
        temp_dir_cfg = os.path.join(temp_dir_cfg, '')
        # evaluate and expand "~"
        self.out_dir = os.path.expanduser(out_dir_cfg)
        self.temp_dir = os.path.expanduser(temp_dir_cfg)
        # create directories if they doesn't exist
        os.makedirs(self.temp_dir, exist_ok=True)
        os.makedirs(self.out_dir, exist_ok=True)

        # setup internal services
        self.gcode_service = TimelapseGcodeService(self)
        self.frame_service = TimelapseFrameService(self)
        self.render_service = TimelapseRenderService(self)
        self.framecount = self.frame_service.seed_framecount()

        # setup eventhandlers and endpoints
        file_manager = self.server.lookup_component("file_manager")
        file_manager.register_directory("timelapse",
                                        self.out_dir,
                                        full_access=True
                                        )
        file_manager.register_directory("timelapse_frames", self.temp_dir)
        self.server.register_notification("timelapse:timelapse_event")
        self.server.register_event_handler(
            "server:gcode_response", self.handle_gcode_response)
        self.server.register_event_handler(
            "server:status_update", self.handle_status_update)
        self.server.register_event_handler(
            "server:klippy_ready", self.handle_klippy_ready)
        self.server.register_remote_method(
            "timelapse_newframe", self.call_newframe)
        self.server.register_remote_method(
            "timelapse_saveFrames", self.call_saveFramesZip)
        self.server.register_remote_method(
            "timelapse_render", self.call_render)
        self.server.register_endpoint(
            "/machine/timelapse/render", ['POST'], self.render)
        self.server.register_endpoint(
            "/machine/timelapse/saveframes", ['POST'], self.saveFramesZip)
        self.server.register_endpoint(
            "/machine/timelapse/settings", ['GET', 'POST'],
            self.webrequest_settings)
        self.server.register_endpoint(
            "/machine/timelapse/lastframeinfo", ['GET'],
            self.webrequest_lastframeinfo)

    async def component_init(self) -> None:
        await self.getWebcamConfig()

    def overwriteDbconfigWithConfighelper(self) -> None:
        self.config_service.overwrite_dbconfig_with_confighelper()

    async def getWebcamConfig(self) -> None:
        await self.config_service.get_webcam_config()

    def parseWebcamConfig(self, webcamconfig) -> None:
        self.config_service.parse_webcam_config(webcamconfig)

    async def webrequest_lastframeinfo(self,
                                       webrequest: WebRequest
                                       ) -> Dict[str, Any]:
        return {
            'framecount': self.framecount,
            'lastframefile': self.lastframefile
        }

    async def webrequest_settings(self,
                                  webrequest: WebRequest
                                  ) -> Dict[str, Any]:
        return await self.config_service.webrequest_settings(webrequest)

    async def handle_klippy_ready(self) -> None:
        ioloop = IOLoop.current()
        ioloop.spawn_callback(self.setgcodevariables)

        ioloop = IOLoop.current()
        ioloop.spawn_callback(self.stop_hyperlapse)

    async def setgcodevariables(self) -> None:
        await self.gcode_service.setgcodevariables()

    def call_newframe(self, macropark=False, hyperlapse=False) -> None:
        if self.config['enabled']:
            if self.config['mode'] == "hyperlapse":
                if hyperlapse:
                    if not self.takingframe:
                        self.takingframe = True
                        self.spawn_newframe_callbacks()
                    else:
                        logging.info("last take frame hasn't completed"
                                     + " ignoring take frame command"
                                     )
                else:
                    logging.info("ignoring non hyperlapse triggered macros"
                                 + "in hyperlapse mode"
                                 )
            else:
                self.spawn_newframe_callbacks()
        else:
            logging.info("NEW_FRAME macro ignored timelapse is disabled")

    def spawn_newframe_callbacks(self) -> None:
        ioloop = IOLoop.current()
        # release parked head after park time is passed
        park_time = self.config['park_time']
        ioloop.call_later(delay=park_time, callback=self.release_parkedhead)
        # capture the frame after stream delay is passed
        stream_delay = self.config['stream_delay_compensation']
        ioloop.call_later(delay=stream_delay, callback=self.newframe)

    async def release_parkedhead(self) -> None:
        await self.gcode_service.release_parkedhead()

    async def start_hyperlapse(self) -> None:
        await self.gcode_service.start_hyperlapse()

    async def stop_hyperlapse(self) -> None:
        await self.gcode_service.stop_hyperlapse()

    async def newframe(self) -> None:
        await self.frame_service.newframe()

    async def handle_status_update(self, status: Dict[str, Any]) -> None:
        if 'print_stats' in status:
            printstats = status['print_stats']
            if 'state' in printstats:
                state = printstats['state']
                if state == 'cancelled':
                    self.printing = False
                    ioloop = IOLoop.current()
                    ioloop.spawn_callback(self.stop_hyperlapse)

    async def handle_gcode_response(self, gresponse: str) -> None:
        if gresponse == "File selected":
            # print_started
            self.cleanup()
            self.printing = True

            # start hyperlapse if mode is set
            if self.config['mode'] == "hyperlapse":
                ioloop = IOLoop.current()
                ioloop.spawn_callback(self.start_hyperlapse)

        elif gresponse == "Done printing file":
            # print_done
            self.printing = False

            # stop hyperlapse if mode is set
            if self.config['mode'] == "hyperlapse":
                ioloop = IOLoop.current()
                ioloop.spawn_callback(self.stop_hyperlapse)

            if self.config['enabled']:
                if self.config['saveframes']:
                    ioloop = IOLoop.current()
                    ioloop.spawn_callback(self.saveFramesZip)
                if self.config['autorender']:
                    ioloop = IOLoop.current()
                    ioloop.spawn_callback(self.render)

    def cleanup(self) -> None:
        self.frame_service.cleanup()

    def call_saveFramesZip(self) -> None:
        ioloop = IOLoop.current()
        ioloop.spawn_callback(self.saveFramesZip)

    async def saveFramesZip(self, webrequest=None):
        return await self.frame_service.save_frames_zip(webrequest)

    def call_render(self, byrendermacro=False) -> None:
        self.byrendermacro = byrendermacro
        ioloop = IOLoop.current()
        ioloop.spawn_callback(self.render)

    async def render(self, webrequest=None):
        return await self.render_service.render(webrequest)

    def ffmpeg_cb(self, response):
        self.render_service.ffmpeg_cb(response)

    def notify_event(self, result: Dict[str, Any]) -> None:
        logging.debug(f"notify_event: {result}")
        self.server.send_event("timelapse:timelapse_event", result)


def load_component(config: ConfigHelper) -> Timelapse:
    return Timelapse(config)
