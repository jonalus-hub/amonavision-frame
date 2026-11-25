# Sample media

What the simulator puts on the frame on its first start (and after
`reset.sh`), as if the family had sent it. `media.json` lists each item in the
order it is sent, who "sends" it, and where it comes from.

Everything is free to use, at no cost and with no attribution required:

- **12 photos** (`01`–`12 .jpg`): stock photos from StockSnap
  (https://stocksnap.io), found through Openverse (https://openverse.org),
  which lists each of them under **CC0 1.0** (public domain dedication). These
  are the 960 px wide versions StockSnap serves publicly; the frame's screen is
  1024×600, so that is enough.
- **4 voice notes** (`01`, `03`, `07`, `09 .ogg`): replies to those photos,
  paired the way Telegram does it (the voice note replies to the photo). The
  voices are synthetic, made with Kokoro TTS v1.0 (Apache-2.0), so they are
  no real person's voice. The text and the voice of each one are in `media.json`.
- **1 video**: "Family making a video call on smartphone" from Mixkit, under
  the **Mixkit Stock Video Free License** (https://mixkit.co/license/#videoFree).
  That license lets you use the clip for free, commercially too, but Mixkit's
  terms don't let you pass the file itself on. So the video is **not in this
  repository**: the simulator downloads it from Mixkit (`download_url` in
  `media.json`) on its first start and keeps it in `simulator/data/`. Without a
  network connection it is skipped. Mixkit's other video license, "Restricted",
  only allows personal use, so check the license before adding another Mixkit video.

The author and the original page of every item are in `media.json` anyway.
The sender names are made up.
