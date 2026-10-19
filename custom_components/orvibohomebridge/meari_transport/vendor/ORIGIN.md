# Meari protocol primitives

Adapted from Anthony Jeanney's MIT-licensed Ha-CloudEdge-Meari:
https://github.com/ajeanney/Ha-CloudEdge-Meari

Pinned commit: `5bc9f97766c3d8076da8bb15dcb7b62ca7721127`.
Original source: `custom_components/cloudplus/`, including `p2p_streamer/`.
The original MIT license is in LICENSE and applies to these derived files.

Only signaling, TURN, KCP, discovery, SDP and frame/codec primitives are included.
No CloudPlus account login, MQTT, engine, snap keepalive, wake, relay probe,
background heartbeat thread, media muxer or Home Assistant module is imported.

Local changes add a shared monotonic soft deadline, bounded discovery and STUN
transactions, socket shutdown on close, strict frame limits and an explicit VVP
channel. Root discovery uses only session-derived domain hints, never a list of
all geographic profiles. The parent kills and waits for this disposable process
at its hard deadline, including if an operating-system DNS call blocks.

The worker uses the actual external-session partnerId/userID and explicitly
identifies the emulated Android version. The public reference's SDK discovery
version 15259 still needs real-device validation for this APK. For iotType=3,
p2p=2 and no custom video password, native evidence confirms hostKey[:16] and
the formatted licenceId in the signature; these are explicit context flags,
never inferred from password length. Native live/stop/heartbeat header word
at offset 4 becomes network-order 1 in the auth finalizer, param is 8,
and their selected channel is preserved. Codec checks reject header-only NALs
and include HEVC IRAP types 16–21; they do not replace a decoder.
An encoded keyframe with parameter sets proves only
encoded media reception; it does not prove decoding, playback or disk history.
