#!/usr/bin/env python3
# coding: utf-8

# Copyright 2020 eve autonomy inc. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import asyncio
from collections import deque
import os
import signal
import subprocess
import threading
import time

from audio_driver_msgs.msg import SoundDriverCtrl
from audio_driver_msgs.msg import SoundDriverRes
import gi
from gi.repository import GObject, Gst
import rclpy
from rclpy.node import Node
from rclpy.qos import QoSDurabilityPolicy, QoSProfile

gi.require_version('Gst', '1.0')
gi.require_version('GObject', '2.0')


class AudioTemporaryStore():
    # Class for Storing audio information.

    def __init__(
        self,
        file_path: str = "",
        volume: float = 0.0,
        is_loop: bool = False,
        loop_delay: float = 0.0,
        start_delay: float = 0.0,
        flg_stop: bool = False
    ):
        self._file_path = file_path
        self._volume = volume
        self._is_loop = is_loop
        self._loop_delay = loop_delay
        self._start_delay = start_delay
        self._first_loop_complete = False
        self._flg_stop = flg_stop

    def change_value(self, info):
        # Exclusion control should be done on the caller side.
        self._file_path = info.file_path
        self._volume = info.volume
        self._is_loop = info.is_loop
        self._loop_delay = info.loop_delay
        self._start_delay = info.start_delay
        self._first_loop_complete = False
        self._flg_stop = info.flg_stop

    def change_volume(self, volume):
        # Exclusion control should be done on the caller side.
        self._volume = volume

    @property
    def file_path(self):
        return self._file_path

    @property
    def volume(self):
        return self._volume

    @property
    def is_loop(self):
        return self._is_loop

    @property
    def loop_delay(self):
        return self._loop_delay

    @property
    def start_delay(self):
        return self._start_delay

    @property
    def flg_stop(self):
        return self._flg_stop

    @property
    def first_loop_complete(self):
        return self._first_loop_complete

    @first_loop_complete.setter
    def first_loop_complete(self, value: bool):
        self._first_loop_complete = value


class AudioDriver(Node):

    async def wait_with_delay(self, delay_sec):
        await asyncio.sleep(delay_sec)

    def recovery_with_retry(self):
        start_time = time.time()
        
        # The time of delay should be set to guarantee 2 times
        # the time it takes to execute the command.
        subprocess.run(['pulseaudio', '-k'])
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(
            self.wait_with_delay(0.02))

        while(True):
            if time.time() - start_time > self._recovery_timeout_sec:
                raise TimeoutError
            code = subprocess.run(['pulseaudio', '--check'])
            self._loop.run_until_complete(
                self.wait_with_delay(0.05))
            if (code.returncode != 0):
                continue
            self.play()
            break

    def receive_command_callback(self, data: SoundDriverCtrl):
        logger = self.get_logger()
        logger.info(
            "audio_driver {0} {1} {2}".format(
                data.cmd_type,
                data.is_loop,
                data.volume))

        cmd_type = data.cmd_type
        volume = max(0.0, min(1.0, data.volume))

        # Exclusion control to prevent `bus_call` from being executed.
        self._lock.acquire()

        if cmd_type == SoundDriverCtrl.CMD_PLAY:
            # Create an instance to store the audio information.
            audio_info = AudioTemporaryStore(
                file_path=data.file_path,
                volume=volume,
                is_loop=data.is_loop,
                loop_delay=data.loop_delay,
                start_delay=data.start_delay
            )

            # Audio information is stocked only when the first 
            #   playback is not completed whether loop playback or not.
            if ((self._playing is False) or
                    (self._current_audio.first_loop_complete is True)):
                self._current_audio.change_value(audio_info)
                self.play()
            else:
                if len(self._audio_info_stock) >= 1:
                    # Clear the old stock and stock the last information.
                    self._audio_info_stock.clear()
                self._audio_info_stock.append(audio_info)
        elif cmd_type == SoundDriverCtrl.CMD_STOP:
            self.stop()
        elif cmd_type == SoundDriverCtrl.CMD_VOLUME:
            self._current_audio.change_volume(volume)
            self.set_property(
                "volume", self._current_audio.volume)
        else:
            logger.error("[play] invalid command {}".format(cmd_type))
        self._lock.release()

    # Callback function from Gstreamer.
    def bus_call(self, bus, msg):
        logger = self.get_logger()

        # The following Message Types are received in large numbers.
        # Manage as a data list without log output.
        white_data_list = [Gst.MessageType.STATE_CHANGED,
                           Gst.MessageType.STREAM_STATUS,
                           Gst.MessageType.STREAM_START,
                           Gst.MessageType.LATENCY,
                           Gst.MessageType.TAG,
                           Gst.MessageType.ASYNC_DONE,
                           Gst.MessageType.NEW_CLOCK,
                           Gst.MessageType.EOS]

        if msg.type == Gst.MessageType.EOS:

            # Exclusion control to
            #   prevent `receive_command_callback` from being executed.
            self._lock.acquire()

            self.set_state(Gst.State.NULL)

            if self._playing is True:
                if self._current_audio.is_loop is False:
                    # One time playback mode.
                    logger.info("[done]")
                    pub_msg = SoundDriverRes()
                    pub_msg.stamp = self.get_clock().now().to_msg()
                    self._pub.publish(pub_msg)
                    # Set the playback flag to false.(playback complete)
                    self._playing = False
                else:
                    # Loop playback mode.
                    # If in stock, switch to the next audio.
                    result = self.check_next_audio_and_play()
                    if not result:
                        self._current_audio.first_loop_complete = True
                        if self._current_audio.loop_delay != 0:
                            # Wait the next playback
                            #   for the time set in "Loop Delay".
                            asyncio.set_event_loop(self._loop)

                            # [Remaining issue] Asynchronization using "asyncio" 
                            #   is assumed, but it is not asynchronous processing.
                            #   The current implementation is equivalent to 
                            #   the implementation using "sleep".
                            self._loop.run_until_complete(
                                self.wait_with_delay(self._current_audio.loop_delay))
                        self.gst_play()

            self._lock.release()
        elif msg.type == Gst.MessageType.ERROR:
            logger.error("[audio driver] bus_call Gst.MessageType.ERROR")
            try:
                self.recovery_with_retry()
            except TimeoutError:
                logger.error("[audio driver] failed recovery")

        if msg.type not in white_data_list:
            logger.info("[audio_driver] msg.type : {}".format(msg.type))

        return True

    def __init__(self):
        super().__init__("audio_driver")

        GObject.threads_init()

        recovery_timeout_sec = self.declare_parameter("recovery_timeout_sec", 1.0)
        self._recovery_timeout_sec = recovery_timeout_sec.get_parameter_value().double_value

        self._playing = False
        self._gst_thread_loop = GObject.MainLoop()

        # Publisher
        depth = 4
        profile = QoSProfile(depth=depth)
        profile.durability = QoSDurabilityPolicy.TRANSIENT_LOCAL
        self._pub = self.create_publisher(SoundDriverRes, "audio_res", profile)

        # Subscriber
        depth = 3
        profile = QoSProfile(depth=depth)
        profile.durability = QoSDurabilityPolicy.TRANSIENT_LOCAL
        self._sub = self.create_subscription(
            SoundDriverCtrl,
            "audio_cmd",
            self.receive_command_callback,
            profile)

        # Create playbin pipeline.
        self._playbin = Gst.ElementFactory.make('playbin', self.get_name())
        self._bus = self._playbin.get_bus()
        self._current_audio = AudioTemporaryStore()
        self._audio_info_stock = deque()
        self._loop = asyncio.new_event_loop()
        self._lock = threading.Lock()
        self._bus.add_watch(0, self.bus_call)

        self._gst_th = threading.Thread(target=self._gst_thread_loop.run)
        self._gst_th.start()
        self.set_state(Gst.State.NULL)

        self.get_logger().info("initialize")

    def play(self):
        logger = self.get_logger()
        self.set_state(Gst.State.NULL)

        logger.info(
            "[play] self.filepath = {}".format(self._current_audio.file_path))

        if not os.path.isfile(self._current_audio.file_path):
            logger.error("[play] no wav file exist {}".format(
                self._current_audio.file_path))
            return

        # Clear any remaining stock.
        if len(self._audio_info_stock) >= 1:
            self._audio_info_stock.clear()

        file_path = os.path.realpath(self._current_audio.file_path)
        self.set_property("uri", "file://" + file_path)
        self.set_property("volume", self._current_audio.volume)
        self._playing = True

        # Wait the next playback for the time set in "start_delay".
        # Considering the soft interrupt load and the actual hearing
        #   does not change even if it differs by 0.01 seconds, 
        #   0.01 seconds or less should be ignored.
        if self._current_audio.start_delay >= 0.01:
            logger.info("[play] start delay {}".format(
                self._current_audio.start_delay))
            asyncio.set_event_loop(self._loop)

            # [Remaining issue] Asynchronization using "asyncio"
            #   is assumed, but it is not asynchronous processing.
            #   The current implementation is equivalent to
            #   the implementation using "sleep".
            self._loop.run_until_complete(
                self.wait_with_delay(self._current_audio.start_delay))
        self.gst_play()

    def gst_play(self):
        self.set_property("volume", self._current_audio.volume)
        self.set_state(Gst.State.PLAYING)

    def check_next_audio_and_play(self) -> bool:
        result = False
        if len(self._audio_info_stock) >= 1:
            # If the next audio is stocked, pop it to play.
            self.stop()
            self._current_audio.change_value(self._audio_info_stock.pop())
            if not self._current_audio.flg_stop:
                self.play()
            result = True
        return result

    def set_state(self, state_value):
        logger = self.get_logger()
        try:
            self._playbin.set_state(state_value)
        except Exception as e:
            logger.error("[set_state] exception occurred : {}".format(e.args))

    def set_property(self, name, value):
        logger = self.get_logger()
        try:
            self._playbin.set_property(name, value)
        except Exception as e:
            logger.error("[set_property] exception occurred : {}".format(e.args))

    def stop(self):
        self._playing = False
        self.set_state(Gst.State.NULL)

    def fin(self):
        self.set_state(Gst.State.NULL)
        self._bus.remove_watch()
        self._playing = False
        self._current_audio = None
        if len(self._audio_info_stock) >= 1:
            self._audio_info_stock.clear()
        self._loop.close()
        self._gst_thread_loop.quit()

    def __del__(self):
        self.fin()


def shutdown(signal, frame):
    node.fin()
    node.destroy_node()
    rclpy.shutdown()


def main(args=None):
    global node
    Gst.init(None)
    signal.signal(signal.SIGINT, shutdown)

    rclpy.init(args=args)

    node = AudioDriver()
    rclpy.spin(node)


if __name__ == '__main__':
    main()
