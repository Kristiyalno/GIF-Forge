# GIF Forge

> A desktop GIF composition editor for stacking transparent GIFs, managing
> layered animations, preserving original timing, and exporting optimized
> GIFs with advanced rendering options.

## Running it

```
pip install -r requirements.txt
python main.py
```

Requires Python 3.9+ with Tkinter (bundled with most Python installs; on
Linux you may need `sudo apt install python3-tk`). Tested on Python 3.12.

## What's implemented

- **Layer list** (left panel): Add GIF, Add Transparent Space, Remove,
  Remove All; right-click context menu with Move Up/Down/To Top/To Bottom,
  Duplicate, Lock, Hide, Change File / Edit Height, Remove; drag-and-drop
  reordering; click the Visible/Locked columns to toggle them; double-click
  a GIF row to swap its source file, double-click a Space row to edit its
  height. Each GIF layer also has X/Y offset fields and a Width/Height
  resize with an **Auto Size** toggle - Auto renders at the source GIF's
  native size (default), turning it off locks in that size as an editable
  starting point so a layer that's too big for the canvas can be shrunk to
  fit rather than only ever overflowing past the edge.
- **Preview** (right panel): animated playback with Play/Pause/Prev/Next
  and a frame counter. Selecting a GIF layer previews just that layer,
  scaled to fit the preview area at its own aspect ratio (a wide or tall
  GIF fills edge-to-edge, it's never padded into a square). Selecting a
  transparent space shows a checkerboard height readout. Deselecting
  everything - press Escape, or click empty space below the last row in
  the layer list - shows the whole composited canvas instead, animated,
  at every visible layer's real position and size, so there's always
  something meaningful on screen either way.
- **Bottom panel**: output width/height, output filename, Last Byte option
  (None / 21 / 2C), output Duration (Auto or Custom, see below), a
  live-updating Resolution / Estimated Frames / Estimated Duration /
  Estimated File Size readout, and Generate.
- **Decode caching** keyed to each source file's mtime, so duplicate layers
  or reused files are only decoded once, and edits to a source file are
  picked up automatically.
- **Projects** (`.gfp`, plain JSON): layer order, offsets, visibility,
  locks, output size, Last Byte option, duration setting, and theme, plus
  the last output path. Source GIF pixel data is never embedded, only
  file paths. Keybinds are global (one shared config for the whole app,
  in `settings/keybinds.json`), not saved per project.
- **Undo/redo** (20 levels), autosave with recovery-on-launch, an
  unsaved-changes indicator in the title bar, and recent projects (File >
  Open Recent, plus a small "recent projects" prompt on a fresh launch).
- **Menu bar**: File / Edit / View (System / Dark / Light theme, applied
  live, with polling to follow OS theme changes on Windows/macOS) /
  Keybinds / Help.
- **Background-thread rendering** with a progress dialog (Decoding GIFs >
  Building Timeline > Rendering Frames > Writing Output > Finished) and
  safe cancellation.
- **Validation warnings** before generating: canvas too small, a layer
  extending outside the canvas, or an unusually large estimated file size —
  each lets you proceed or cancel.

## Timeline: independent looping, no LCM

Early on this used the least common multiple of every layer's loop length
to line everything back up exactly - which sounds precise, but two GIFs
that are each only a couple of seconds long could force an output minutes
long just to reach a moment where both loop back to frame one together.

That's not actually the goal. The goal is to preserve each GIF's original
timing while letting it loop on its own, not to wait for a perfect
realignment. So every GIF layer keeps its own internal timeline built
straight from its stored frame delays, and at any render time `t`, a
layer's active frame is found independently of every other layer:

```
local_time = t % layer_cycle_ms
```

Nothing is ever resampled, no FPS is ever assumed, and every layer keeps
its exact original per-frame delays. Total output duration is no longer
tied to any of that math - instead, you pick:

- **Auto** - a practical duration: the longest single layer's own loop,
  doubled, so short GIFs still get more than one pass.
- **Custom** - any duration you enter, in milliseconds.

All layers keep looping independently, each at its own speed, until the
chosen duration is reached.

## Keybinds: ordered + exact combos

The keybind editor (Keybinds > Edit Keybinds...) is built the same way as
the one from the ReDone project: each row is Action / Keys / Set / Ordered
/ Exact.

- Type a combo directly (`control+shift+s`), or click **Set** to capture
  one by holding the keys down and releasing one.
- **Ordered** means the keys must be pressed in that specific sequence,
  not just held together at the same time - useful for chord-style
  shortcuts rather than plain modifier combos.
- **Exact** means the combo only fires when *exactly* those keys are held
  and nothing else. This is what stops, say, Move Up (`control+up`) from
  also firing while you're actually holding Move To Top's
  (`control+shift+up`) combo - every shipped shortcut ships with Exact on
  for this reason.
- Conflict detection flags it if a combo you're setting is already used
  elsewhere, with the option to reassign it anyway.
- Restore Defaults, and JSON Import/Export, are both available from the
  same dialog. Changes save automatically as you make them.

Unlike ReDone (which hooks the OS directly via `pynput` so its shortcuts
work globally, even when unfocused), GIF Forge only needs shortcuts while
its own window has focus, so it tracks key state through ordinary Tkinter
key events instead of an OS-level listener. Shortcuts that need a modifier
(Control/Alt) still work while you're typing in a text field; bare-key
shortcuts like Delete or Space are suppressed there so they don't fight
with normal text editing.

## One design call worth flagging

The spec says GIF layers store an X/Y offset and that transparent spaces
"contribute vertical spacing before following layers," but doesn't fully
spell out how layers are positioned overall. This is implemented as a
vertical auto-stack: each layer's default position is directly beneath the
combined height of everything above it in the layer list (GIF layers
contribute their own frame height, spaces contribute their configured
height), and a layer's X/Y offset then nudges it from that computed
default. That's what makes "Add Transparent Space" actually do something
visible, and gives GIF layers a sensible default position with manual
fine-tuning on top.

If you actually want GIF layers positioned purely by absolute X/Y (i.e. no
auto-stacking, spaces just reserve conceptual height without affecting
anyone's position), that's a small, contained change in
`renderer.compute_layout` - let me know and I'll switch it.

## Project structure

```
gifforge/
├── main.py        entry point
├── gui.py          main window: menus, panels, wiring
├── renderer.py     layout, compositing, background render thread, export
├── timeline.py     independent per-layer timelines, Auto/Custom duration
├── project.py      .gfp save/load, undo/redo stack, autosave
├── layers.py       GifLayer / SpaceLayer data model
├── preview.py      right-panel preview widget
├── keybinds.py     keybind manager + ordered/exact editor + capture dialog
├── settings.py     window state, recent projects, persisted settings
├── theme.py        System / Dark / Light theming
├── cache.py        decode caching
├── utils.py        small shared helpers
├── assets/
├── projects/       .gfp files land here by default; .autosave/ holds the autosave
└── settings/       app_settings.json, keybinds.json
```

## Not yet wired up

The spec's "Future-Friendly Architecture" section calls out canvas
zoom/pan/guides/snapping/scaling/rotation as things to leave room for later
- none of those are built now, but nothing here should block adding them
(the preview canvas and `compute_layout` are the natural extension points).
