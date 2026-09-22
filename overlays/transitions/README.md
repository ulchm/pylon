# OBS stinger transitions

Empty by default, and that is fine: Pylon's scene switching uses OBS's own
**Fade**, which every install has, and the replay wipe you see over the race is
drawn in CSS by the overlay page itself (it takes your brand colour, so there is
no file to re-make when you change it).

A stinger is a video with an alpha channel that OBS plays over the programme
while it cuts underneath. If you have one you want to use between the race and
the holding cards, put the file here and set it up in OBS:

1. **Scene Transitions** -> **+** -> **Stinger**.
2. **Video File**: point at your file.
3. **Transition Point**: `Time (milliseconds)`, set to the moment your file is
   fully covering the frame. This is when the cut happens underneath.
4. **Audio Monitoring**: leave off unless the file carries sound.
5. Name it, then put that same name in `config.toml` as `obs.transition`.

This part is by hand, and has to be: obs-websocket can get and set the *current*
transition but has no request to **create** one, so `pylon obs-setup` can build
every scene and source and still not build a stinger.

WebM / VP9 with a real alpha channel (`yuva420p`) is the format to aim for: OBS
reads it natively and it squeezes flat colour down to a couple of hundred KB. If
a machine's OBS refuses alpha WebM, which happens on some older Windows builds,
QuickTime Animation (`.mov`) works everywhere at a much larger file size.
