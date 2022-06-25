# audio_driver

## Overview
This node is a driver that controls audio by receiving play / stop commands for the specified audio file.
The [GStreamer](https://gstreamer.freedesktop.org/) library is used for audio playback.

## Input and Output
- input
  - from [ad_sound_manager](https://github.com/eve-autonomy/ad_sound_manager)
    - `audio_cmd` : Audio playback request.
- output
  - to [ad_sound_manager](https://github.com/eve-autonomy/ad_sound_manager)
    - `audio_res` : Notification that audio playback is complete.

## Node Graph
![node graph](http://www.plantuml.com/plantuml/proxy?cache=no&src=https://raw.githubusercontent.com/eve-autonomy/audio_driver/main/docs/node_graph.pu)

## Parameter description
<table>
  <thead>
    <tr>
      <th scope="col">Name</th>
      <th scope="col">Description</th>
    </tr>
  </thead>
  <tbody>
    <tr>
      <td>recovery_timeout_sec</td>
      <td>Allowable period for recovery processing for GStreamer errors (give up recovery after this period)</td>
    </tr>
  </tbody>
</table>
