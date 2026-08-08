# Timelapse regression tests
#
# Copyright (C) 2026 Christoph Frei <fryakatkop@gmail.com>
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import asyncio
import os
import re
import sys
import tempfile
import types
import unittest
from unittest.mock import patch
from zipfile import ZipFile


# Provide a minimal tornado.ioloop stub for test environments
# where tornado is not installed.
if "tornado.ioloop" not in sys.modules:
    tornado_module = types.ModuleType("tornado")
    ioloop_module = types.ModuleType("tornado.ioloop")

    class _ImportIOLoopInstance:
        def spawn_callback(self, callback, *args, **kwargs):
            result = callback(*args, **kwargs)
            if asyncio.iscoroutine(result):
                asyncio.create_task(result)

        def call_later(self, _delay, callback):
            result = callback()
            if asyncio.iscoroutine(result):
                asyncio.create_task(result)

    class _ImportIOLoop:
        _instance = _ImportIOLoopInstance()

        @classmethod
        def current(cls):
            return cls._instance

    ioloop_module.IOLoop = _ImportIOLoop
    sys.modules["tornado"] = tornado_module
    sys.modules["tornado.ioloop"] = ioloop_module

from component.timelapse import Timelapse


class FakeDatabase:
    def __init__(self):
        self.items = {}
        self.insert_calls = []

    def get_item(self, namespace, key, default):
        return default

    def insert_item(self, namespace, key, value):
        self.insert_calls.append((namespace, key, value))
        self.items[(namespace, key)] = value


class FakeFileManager:
    def __init__(self):
        self.directories = []

    def register_directory(self, name, path, full_access=False):
        self.directories.append((name, path, full_access))


class FakeKlippyAPI:
    def __init__(self):
        self.gcodes = []
        self.print_filename = "test_print.gcode"

    async def run_gcode(self, gcommand):
        self.gcodes.append(gcommand)

    async def query_objects(self, _query):
        return {
            "print_stats": {
                "filename": self.print_filename,
            }
        }


class FakeShellCommand:
    def __init__(self, command, callback, behavior):
        self.command = command
        self.callback = callback
        self.behavior = behavior
        self.run_kwargs = {}

    async def run(self, **kwargs):
        self.run_kwargs = kwargs
        if callable(self.behavior):
            return await self.behavior(self.command, self.callback)
        return bool(self.behavior)


class FakeShellFactory:
    def __init__(self):
        self.commands = []
        self.shell_commands = []
        self.behaviors = []

    def push_behavior(self, behavior):
        self.behaviors.append(behavior)

    def build_shell_command(self, command, callback=None):
        self.commands.append(command)
        behavior = self.behaviors.pop(0) if self.behaviors else True
        shell_command = FakeShellCommand(command, callback, behavior)
        self.shell_commands.append(shell_command)
        return shell_command


class FakeConfigHelper:
    def __init__(self, server, values=None, options=None):
        self.server = server
        self.values = values or {}
        self.options = options or []

    def get_server(self):
        return self.server

    def get(self, key, default=None):
        return self.values.get(key, default)

    def getboolean(self, key, default=False):
        return bool(self.values.get(key, default))

    def getint(self, key, default=0):
        return int(self.values.get(key, default))

    def getfloat(self, key, default=0.0):
        return float(self.values.get(key, default))

    def get_options(self):
        return list(self.options)


class FakeWebRequest:
    def __init__(self, action, args):
        self._action = action
        self._args = args

    def get_action(self):
        return self._action

    def get_args(self):
        return self._args

    def get(self, key):
        return self._args[key]

    def get_boolean(self, key):
        return bool(self._args[key])

    def get_int(self, key):
        return int(self._args[key])

    def get_float(self, key):
        return float(self._args[key])


class FakeIOLoopInstance:
    def __init__(self):
        self.spawned = []

    def spawn_callback(self, callback, *args, **kwargs):
        task = asyncio.create_task(callback(*args, **kwargs))
        self.spawned.append(task)

    def call_later(self, _delay, callback):
        result = callback()
        if asyncio.iscoroutine(result):
            self.spawned.append(asyncio.create_task(result))


class FakeIOLoopClass:
    instance = FakeIOLoopInstance()

    @classmethod
    def current(cls):
        return cls.instance


class FakeServer:
    class error(Exception):
        pass

    def __init__(self):
        self.file_manager = FakeFileManager()
        self.database = FakeDatabase()
        self.klippy_api = FakeKlippyAPI()
        self.shell_factory = FakeShellFactory()
        self.webcam_manager = None
        self.events = []

    def lookup_component(self, name):
        if name == "file_manager":
            return self.file_manager
        if name == "database":
            return self.database
        if name == "klippy_apis":
            return self.klippy_api
        if name == "shell_command":
            return self.shell_factory
        if name == "webcam":
            if self.webcam_manager is None:
                raise KeyError("webcam component unavailable")
            return self.webcam_manager
        raise KeyError(name)

    def register_notification(self, _name):
        pass

    def register_event_handler(self, _event, _callback):
        pass

    def register_remote_method(self, _name, _callback):
        pass

    def register_endpoint(self, _path, _methods, _callback):
        pass

    def send_event(self, _event_name, payload):
        self.events.append(payload)


async def ffmpeg_success_behavior(command, callback):
    mp4_match = re.search(r"'([^']+\.mp4)'", command)
    if mp4_match:
        with open(mp4_match.group(1), "wb") as handle:
            handle.write(b"video")
    if callback is not None and " -r " in command:
        callback(b"frame=  42 fps=30")
    return True


class TimelapseRegressionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_root = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_root.cleanup)

        self.temp_dir = os.path.join(self.temp_root.name, "frames")
        self.out_dir = os.path.join(self.temp_root.name, "out")
        os.makedirs(self.temp_dir, exist_ok=True)
        os.makedirs(self.out_dir, exist_ok=True)

        self.ffmpeg_path = os.path.join(self.temp_root.name, "ffmpeg")
        with open(self.ffmpeg_path, "w", encoding="ascii") as handle:
            handle.write("#!/bin/sh\n")

        self.server = FakeServer()
        self.config_values = {
            "frame_path": self.temp_dir,
            "output_path": self.out_dir,
            "ffmpeg_binary_path": self.ffmpeg_path,
            "wget_skip_cert_check": True,
        }

        self.confighelper = FakeConfigHelper(self.server, self.config_values)
        self.timelapse = Timelapse(self.confighelper)

    def _write_frames(self, count):
        for idx in range(1, count + 1):
            path = os.path.join(self.temp_dir, f"frame{idx:06d}.jpg")
            with open(path, "wb") as handle:
                handle.write(b"jpg")

    def _make_pinned_timelapse(self, **pinned):
        values = dict(self.config_values)
        values.update(pinned)
        confighelper = FakeConfigHelper(
            self.server, values, options=list(pinned)
        )
        timelapse = Timelapse(confighelper)
        self.server.database.insert_calls.clear()
        return timelapse

    async def test_newframe_success_emits_event(self):
        self.server.shell_factory.push_behavior(True)

        await self.timelapse.newframe()

        self.assertEqual(self.timelapse.framecount, 1)
        self.assertEqual(self.timelapse.lastframefile, "frame000001.jpg")
        self.assertEqual(self.server.events[-1]["status"], "success")
        self.assertIn(
            "--no-check-certificate",
            self.server.shell_factory.commands[-1]
        )
        self.assertEqual(
            self.server.shell_factory.shell_commands[-1].run_kwargs["timeout"],
            2.0
        )

    async def test_newframe_quotes_snapshot_url(self):
        self.timelapse.config["snapshoturl"] = (
            "http://camera.local/snapshot?label=print one"
        )
        self.server.shell_factory.push_behavior(True)

        await self.timelapse.newframe()

        self.assertIn(
            "'http://camera.local/snapshot?label=print one'",
            self.server.shell_factory.commands[-1]
        )

    async def test_newframe_uses_configured_timeout(self):
        self.timelapse.wget_timeout = 4.5
        self.server.shell_factory.push_behavior(True)

        await self.timelapse.newframe()

        self.assertEqual(
            self.server.shell_factory.shell_commands[-1].run_kwargs["timeout"],
            4.5
        )

    async def test_newframe_failure_rolls_back_framecount(self):
        self.server.shell_factory.push_behavior(False)

        await self.timelapse.newframe()

        self.assertEqual(self.timelapse.framecount, 0)
        self.assertEqual(self.server.events[-1]["status"], "error")

    async def test_saveframes_skips_without_frames(self):
        result = await self.timelapse.saveFramesZip()
        self.assertEqual(result["action"], "saveframes")
        self.assertNotIn("zipfile", result)

    async def test_saveframes_writes_zip(self):
        self._write_frames(3)
        result = await self.timelapse.saveFramesZip()

        self.assertEqual(result["status"], "finished")
        zip_path = os.path.join(self.out_dir, result["zipfile"])
        self.assertTrue(os.path.exists(zip_path))
        with ZipFile(zip_path) as archive:
            self.assertEqual(len(archive.namelist()), 3)

    async def test_restart_seeds_highest_existing_frame_number(self):
        self._write_frames(2)
        frame_path = os.path.join(self.temp_dir, "frame000005.jpg")
        with open(frame_path, "wb") as handle:
            handle.write(b"jpg")
        frame_path = os.path.join(self.temp_dir, "frame-invalid.jpg")
        with open(frame_path, "wb") as handle:
            handle.write(b"jpg")

        restarted = Timelapse(self.confighelper)
        self.server.shell_factory.push_behavior(True)

        await restarted.newframe()

        self.assertEqual(restarted.framecount, 6)
        self.assertEqual(restarted.lastframefile, "frame000006.jpg")

    def test_render_filter_uses_transpose_zero_for_flip_y(self):
        self.timelapse.config["rotation"] = 270
        self.timelapse.config["flip_y"] = True

        filter_param = (
            self.timelapse.render_service._build_render_filter_param()
        )

        self.assertEqual(filter_param, " -vf 'transpose=0'")

    async def test_render_skips_without_frames(self):
        result = await self.timelapse.render()
        self.assertEqual(result["status"], "skipped")

    async def test_render_reports_missing_ffmpeg(self):
        os.remove(self.ffmpeg_path)
        self.timelapse.ffmpeg_installed = False
        self._write_frames(1)

        result = await self.timelapse.render()

        self.assertEqual(result["status"], "error")
        self.assertIn("not found", result["msg"])

    async def test_render_clears_stale_progress_and_response(self):
        self._write_frames(1)
        self.timelapse.lastrenderprogress = 75
        self.timelapse.lastcmdreponse = "stale output"
        self.server.shell_factory.push_behavior(False)

        result = await self.timelapse.render()

        self.assertEqual(self.timelapse.lastrenderprogress, 0)
        self.assertEqual(result["cmdresponse"], "")

    async def test_render_success_moves_video_and_generates_preview(self):
        self._write_frames(3)
        self.server.shell_factory.push_behavior(ffmpeg_success_behavior)
        self.server.shell_factory.push_behavior(ffmpeg_success_behavior)

        result = await self.timelapse.render()

        self.assertEqual(result["status"], "success")
        output_mp4 = os.path.join(self.out_dir, result["filename"])
        self.assertTrue(os.path.exists(output_mp4))
        self.assertIn("previewimage", result)
        preview_path = os.path.join(self.out_dir, result["previewimage"])
        self.assertTrue(os.path.exists(preview_path))

    async def test_render_duplicate_last_frame_cleanup(self):
        self._write_frames(2)
        self.timelapse.config["duplicatelastframe"] = 2
        self.server.shell_factory.push_behavior(ffmpeg_success_behavior)
        self.server.shell_factory.push_behavior(ffmpeg_success_behavior)

        await self.timelapse.render()

        frame_files = sorted(
            name for name in os.listdir(self.temp_dir)
            if name.startswith("frame")
        )
        self.assertEqual(frame_files, ["frame000001.jpg", "frame000002.jpg"])

    async def test_ffmpeg_callback_emits_progress(self):
        self.timelapse.framecount = 100
        self.timelapse.ffmpeg_cb(b"frame=  50 fps=60")

        self.assertEqual(self.timelapse.lastrenderprogress, 50)
        self.assertEqual(self.server.events[-1]["status"], "running")
        self.assertEqual(self.server.events[-1]["progress"], 50)

    async def test_settings_post_updates_and_persists(self):
        request = FakeWebRequest("POST", {
            "enabled": False,
            "park_time": 0.75,
            "output_framerate": 24,
            "mode": "layermacro",
        })

        with patch("component.timelapse.IOLoop", FakeIOLoopClass):
            result = await self.timelapse.webrequest_settings(request)

        self.assertFalse(result["enabled"])
        self.assertEqual(result["park_time"], 0.75)
        self.assertEqual(result["output_framerate"], 24)
        inserted_keys = [
            entry[1] for entry in self.server.database.insert_calls
        ]
        self.assertIn("config.enabled", inserted_keys)
        self.assertIn("config.park_time", inserted_keys)

    async def test_settings_rejects_snapshot_url_updates(self):
        original_url = self.timelapse.config["snapshoturl"]
        request = FakeWebRequest("POST", {
            "snapshoturl": "http://untrusted.example/snapshot",
        })

        result = await self.timelapse.webrequest_settings(request)

        self.assertEqual(result["snapshoturl"], original_url)
        self.assertEqual(self.server.database.insert_calls, [])

    async def test_settings_rejects_blocked_config_keys(self):
        timelapse = self._make_pinned_timelapse(autorender=True)
        self.assertIn("autorender", timelapse.config["blockedsettings"])
        request = FakeWebRequest("POST", {"autorender": False})

        result = await timelapse.webrequest_settings(request)

        self.assertTrue(result["autorender"])
        self.assertEqual(self.server.database.insert_calls, [])

    async def test_settings_applies_free_key_alongside_blocked_key(self):
        timelapse = self._make_pinned_timelapse(autorender=True)
        request = FakeWebRequest("POST", {
            "autorender": False,
            "park_time": 0.75,
        })

        with patch("component.timelapse.IOLoop", FakeIOLoopClass):
            result = await timelapse.webrequest_settings(request)

        self.assertTrue(result["autorender"])
        self.assertEqual(result["park_time"], 0.75)
        inserted_keys = [
            entry[1] for entry in self.server.database.insert_calls
        ]
        self.assertNotIn("config.autorender", inserted_keys)
        self.assertIn("config.park_time", inserted_keys)

    async def test_settings_declines_blockedsettings_key(self):
        timelapse = self._make_pinned_timelapse(autorender=True)
        request = FakeWebRequest("POST", {
            "blockedsettings": ["nonsense"],
        })

        result = await timelapse.webrequest_settings(request)

        self.assertEqual(result["blockedsettings"], ["autorender"])
        self.assertEqual(self.server.database.insert_calls, [])

    async def test_settings_declines_blockedsettings_key_first(self):
        timelapse = self._make_pinned_timelapse(autorender=True)
        request = FakeWebRequest("POST", {
            "blockedsettings": [],
            "park_time": 0.3,
        })

        with patch("component.timelapse.IOLoop", FakeIOLoopClass):
            result = await timelapse.webrequest_settings(request)

        self.assertEqual(result["blockedsettings"], ["autorender"])
        self.assertEqual(result["park_time"], 0.3)

    async def test_file_selected_cleans_frames(self):
        self._write_frames(2)

        await self.timelapse.handle_gcode_response("File selected")

        self.assertEqual(self.timelapse.framecount, 0)
        self.assertEqual(self.timelapse.lastframefile, "")
        self.assertTrue(self.timelapse.printing)

    async def test_done_printing_triggers_save_and_render(self):
        self.timelapse.config["enabled"] = True
        self.timelapse.config["saveframes"] = True
        self.timelapse.config["autorender"] = True

        calls = {"save": 0, "render": 0}

        async def fake_save(_webrequest=None):
            calls["save"] += 1
            return {"action": "saveframes", "status": "finished"}

        async def fake_render(_webrequest=None):
            calls["render"] += 1
            return {"action": "render", "status": "success"}

        self.timelapse.saveFramesZip = fake_save
        self.timelapse.render = fake_render

        with patch("component.timelapse.IOLoop", FakeIOLoopClass):
            FakeIOLoopClass.instance = FakeIOLoopInstance()
            await self.timelapse.handle_gcode_response("Done printing file")
            await asyncio.gather(*FakeIOLoopClass.instance.spawned)

        self.assertEqual(calls["save"], 1)
        self.assertEqual(calls["render"], 1)
        self.assertFalse(self.timelapse.printing)

    async def test_cancelled_status_stops_hyperlapse(self):
        calls = {"stop": 0}

        async def fake_stop():
            calls["stop"] += 1

        self.timelapse.stop_hyperlapse = fake_stop

        with patch("component.timelapse.IOLoop", FakeIOLoopClass):
            FakeIOLoopClass.instance = FakeIOLoopInstance()
            await self.timelapse.handle_status_update({
                "print_stats": {"state": "cancelled"}
            })
            await asyncio.gather(*FakeIOLoopClass.instance.spawned)

        self.assertFalse(self.timelapse.printing)
        self.assertEqual(calls["stop"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
