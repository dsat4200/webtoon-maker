that was an interesting exploration. I tried it and have undone the changes, as i found it too inflexible.

previously what i was doing was making a nice render in blender, importing that into a drawing program, and then performing various operations on it as an image layer.

what if instead, we have an image object that can have nondestructive operations performed on it (such as the transform tool) and have things drawn on it, etc, but, it's image is streamed from a specific blender "comic view". comic views are stored from a blender plugin

a comic view is essentially data that stores anything that could have a keyframe change between panels - what lights are on, all transforms (whole character keyframe for rigs), what objects are visible, a camera's position, parameters, etc, which camera is active what collections are toggled, modifier stack keyframe state, shape key usage, etc. comic 3d panel data does not include geometry.

these frames have their own thumbnail preview, and only one is active at a time and streams at a time to the comic program. if none is selected, or if a frame isn;t currently streaming, it just stays as is. 3d comic frames act as an image, and as a render from blender. they are essentially a viewport-fidelity render with a transparent background.

as far as the drawing program knows, the 3d stream is just an image.

This architecture is substantially cleaner because **Blender stops being part of the drawing UI entirely**. From the drawing program's perspective, a Blender comic view is just another dynamic image source—similar to a webcam feed or periodically changing image file—except that it freezes on its last valid frame whenever Blender is not streaming.

The key separation would be:

```
BLENDER
owns:
3D scene + Comic View state + rendering

              │
              │ RGBA frames
              ▼

DRAWING PROGRAM
owns:
image object + transforms + filters + drawing + compositing
```

No drawing operations ever cross into Blender.

## 1. Overall architecture

I would build it as three mostly independent systems:

```
┌─────────────────────────────────────────────────────────┐
│                    BLENDER                              │
│                                                         │
│  Actual scene / geometry                                │
│            │                                            │
│            ▼                                            │
│  Comic View Add-on                                      │
│  ├── Comic View database                                │
│  ├── state capture / restore                            │
│  ├── thumbnails                                         │
│  └── viewport renderer                                  │
│            │                                            │
│            ▼                                            │
│        RGBA framebuffer                                 │
└─────────────┬───────────────────────────────────────────┘
              │
              │ shared memory
              │
              ▼
┌─────────────────────────────────────────────────────────┐
│                 DRAWING PROGRAM                         │
│                                                         │
│ BlenderComicViewSource                                  │
│            │                                            │
│            ▼                                            │
│       ImageObject                                       │
│       ├── last received image                           │
│       ├── transform                                     │
│       ├── crop / filters / effects                      │
│       ├── opacity / blend                               │
│       └── attached drawing                              │
│            │                                            │
│            ▼                                            │
│         Canvas compositor                               │
└─────────────────────────────────────────────────────────┘
```

The Blender image isn't special after it enters the drawing application.

That's the important property.

---

# 2. A Comic View is state, not geometry

I'd formalize a Comic View as:

> A named snapshot of all **panel-variable scene state required to reproduce a particular 3D panel**, plus presentation/render settings and a thumbnail.

It contains references to existing Blender data, but **does not duplicate meshes, armatures, textures, etc.**

Conceptually:

```
ComicView
│
├── UUID
├── name
├── revision
│
├── active camera
├── camera state
│
├── object state
│   ├── transforms
│   └── visibility
│
├── rig state
│   ├── pose/control transforms
│   └── rig custom properties
│
├── shape key values
│
├── light state
│   ├── visibility
│   ├── energy
│   ├── color
│   └── relevant parameters
│
├── collection visibility/exclusion
│
├── modifier state
│   ├── viewport enabled
│   └── selected parameters
│
├── other registered properties
│
├── viewport/render settings
│
└── thumbnail
```

Blender already exposes object visibility, including view-layer-specific visibility, and modifier viewport/render states through its Python API.

So two comic views can reference the exact same geometry:

```
VIEW A                         VIEW B

Character                     Character
same mesh                     same mesh
same rig                      same rig

Pose A                        Pose B
Camera A                      Camera B
Light A ON                    Light A OFF
Collection X visible          Collection X hidden
Smile = 0                     Smile = 0.8
Subdivision = 1               Subdivision = 2
```

The major benefit is that if you later improve the model itself:

```
modify geometry
       ↓
all Comic Views
still reference it
       ↓
all panels can be rerendered
```

No stored comic-view geometry becomes stale.

---

# 3. Don't literally capture “every keyframeable property”

Your concept of:

> anything that could change between panels

is correct.

But technically I wouldn't implement it by blindly enumerating **every Blender property marked animatable**.

That's likely to become enormous and unpredictable.

Instead use:

```
DEFAULT CAPTURE SET

Transforms
Camera
Visibility
Collections
Lights
Rig controls
Shape keys
Modifier enable state
Selected modifier parameters
Selected material parameters
etc.

           +

EXTENSIBLE PROPERTY CAPTURE

"This property should also belong
to Comic Views"
```

You could provide something like:

```
Right click property
        ↓
"Include in Comic Views"
```

Then the plugin stores an RNA-style target/property reference and value. Blender's RNA structures expose path mechanisms such as `path_from_id` / `path_resolve`, which makes a generic property-addressing layer practical.

This makes unusual things possible without hardcoding them:

```
Geometry Nodes input
shader parameter
constraint influence
custom rig control
world parameter
modifier parameter
camera DOF
```

The architecture therefore becomes:

```
Comic View State
├── standard strongly-typed state
└── arbitrary registered properties
```

That should scale much better.

---

# 4. Rig state deserves special handling

I would **not store the evaluated mesh or even necessarily final bone matrices**.

For a rig, ideally you store the things the animator manipulates:

```
control bone transforms
+
rig custom properties
+
shape keys
+
other relevant driver inputs
```

rather than:

```
resulting evaluated mesh
```

For example:

```
CharacterRig

hand_IK.rotation
head_control.rotation
eye_target.location
mouth_open = .6
blink_left = .2
blink_right = .2
```

Then Blender's normal constraints, drivers, modifiers, shape keys and dependency evaluation produce the final character.

That preserves exactly the property you want:

**Comic Views describe the pose; Blender continues to describe the character.**

For unknown third-party rigs, you can initially have a broader generic capture mode and later add configurable “rig capture profiles.”

---

# 5. Activating a Comic View

Selecting this thumbnail in the Blender add-on:

```
┌──────┐ ┌──────┐ ┌──────┐
│      │ │      │ │      │
│  12  │ │  13  │ │  14  │
│      │ │      │ │      │
└──────┘ └──▲───┘ └──────┘
             │
             selected
```

would run:

```
Comic View 13
      ↓
resolve referenced Blender objects
      ↓
apply transforms
      ↓
apply rig controls
      ↓
apply visibility
      ↓
apply collections
      ↓
apply lights
      ↓
apply shape keys
      ↓
apply modifiers/properties
      ↓
set active camera
      ↓
Blender evaluates scene
      ↓
render Comic View
```

I would treat these as **snapshots**, rather than requiring every Comic View to correspond to an actual Blender timeline frame.

You could later support:

```
Comic View optionally references
animation frame 127
```

but it shouldn't be fundamental to the design.

---

# 6. The Blender renderer

You specifically want something like:

> viewport fidelity, transparent background

Blender gives you two relevant approaches.

Its normal **Viewport Render** functionality generates preview renders from the current viewport, including images and animations, and the Python render API exposes OpenGL viewport rendering.

For the streaming implementation, however, I'd investigate `GPUOffScreen` first.

Blender exposes:

```
GPUOffScreen(width, height)
```

and:

```
offscreen.draw_view3d(
    scene,
    view_layer,
    view3d,
    region,
    view_matrix,
    projection_matrix,
    ...
)
```

specifically to render a 3D viewport into an offscreen framebuffer. Its framebuffer API also exposes pixel readback.

So conceptually:

```
Blender Scene
     ↓
Comic View state
     ↓
Viewport shading
     ↓
GPUOffScreen
1920 × 1080 × RGBA
     ↓
read pixels
     ↓
shared memory
```

`draw_view3d()` also exposes a `draw_background` option, so I'd specifically prototype rendering with the background disabled and verify the resulting alpha behavior with the shading modes you intend to support.

You don't need:

```
Blender window capture
screen capture
OBS
virtual camera
PNG files every frame
```

at all.

---

# 7. Don't send the images through your command socket

Use two communication channels.

```
                DRAWING APP        BLENDER

CONTROL            │                 │
small messages     │◄───────────────►│
                   │                 │
                   │                 │
PIXELS             │                 │
shared memory      │◄════════════════│
                   │    RGBA         │
```

### Control channel

A local socket handles tiny messages such as:

```
HELLO
LIST_VIEWS
ACTIVATE_VIEW
START_STREAM
STOP_STREAM
SET_RESOLUTION
RENDER_ONCE

FRAME_READY
VIEW_UPDATED
ERROR
```

### Pixel channel

Use shared memory for the actual framebuffer.

Python's standard shared-memory API lets separate processes access the same memory block directly and specifically avoids having to serialize/deserialize a large payload through normal process messaging.

That is perfect for this application.

---

# 8. The framebuffer layout

For example:

```
SharedMemory
│
├── header
│   ├── protocol_version
│   ├── width
│   ├── height
│   ├── stride
│   ├── pixel_format = RGBA8
│   ├── view_uuid
│   ├── view_revision
│   ├── frame_sequence
│   └── active_buffer
│
├── framebuffer 0
├── framebuffer 1
└── framebuffer 2
```

I'd use a **triple buffer** eventually.

Blender writes:

```
buffer 0
      ↓
mark frame 412 READY

buffer 1
      ↓
mark frame 413 READY

buffer 2
      ↓
mark frame 414 READY
```

while Qt can still be displaying an older buffer.

At 1920×1080 RGBA8, one frame is only about:

```
1920 × 1080 × 4
≈ 8.3 MB
```

so three raw buffers are roughly 25 MB.

That's trivial compared with typical 3D scene memory.

---

# 9. PySide receives an ordinary image

This is the beautiful part.

Qt can construct a `QImage` around an existing raw memory buffer with explicit width, height, stride and pixel format, so the incoming shared-memory framebuffer maps very naturally to an image source.

Conceptually:

```
frame = blender_source.current_frame()

image = QImage(
    frame.memory,
    frame.width,
    frame.height,
    frame.stride,
    QImage.Format_RGBA8888,
)
```

The details of buffer lifetime need to be managed carefully because Qt requires that externally supplied memory remain valid for the lifetime of the `QImage`.

For an MVP I'd actually tolerate one additional copy after reading the shared buffer:

```
Blender shared memory
       ↓
QImage / application image buffer
       ↓
display
```

Then optimize to a true rotating zero-copy-ish arrangement only if profiling says you need it.

---

# 10. The drawing program should introduce an `ImageSource`

This is probably the most important software abstraction.

Instead of an image layer owning:

```
image.png
```

it owns:

```
ImageSource
```

For example:

```
ImageSource

├── StaticImageSource
│      ↓
│   cat.png
│
└── BlenderComicViewSource
       ↓
    ComicView UUID
```

Everything downstream is identical.

```
                    ┌─ PNG
                    │
ImageObject ← ImageSource
                    │
                    └─ Blender stream
       │
       ▼
transform
       │
       ▼
filters
       │
       ▼
opacity
       │
       ▼
drawing/compositing
```

This is much less invasive than adding “Blender support” throughout your image tools.

---

# 11. Nondestructive transforms become particularly elegant

Suppose the Blender source produces:

```
SOURCE

┌───────────────┐
│      ◯        │
│     /|\       │
│     / \       │
└───────────────┘
```

Your drawing application stores:

```
ImageObject
│
├── source = BlenderView("Panel-17")
│
├── transform
│   ├── translate
│   ├── rotate
│   ├── scale
│   ├── perspective
│   └── warp
│
├── effects
│
└── attached drawing
```

So:

```
Blender sends frame A
        ↓
existing Transform Stack
        ↓
canvas


Blender later sends frame B
        ↓
SAME Transform Stack
        ↓
canvas
```

Updating Blender doesn't destroy or reset anything in the drawing program.

The pixels change underneath the operations.

---

# 12. “Drawing on the image” should also be nondestructive

I would **not burn strokes into Blender's framebuffer**.

Instead:

```
ImageObject
│
├── Source image       ← Blender
│
├── Attached drawing
│   ├── stroke
│   ├── stroke
│   └── stroke
│
└── Object transform
```

Store attached strokes in **image-local coordinates**.

For example:

```
image coordinate space

0,0 ───────────────── 1,0
 │
 │       ✎────
 │
 │
0,1 ───────────────── 1,1
```

Then:

```
ImageObject transform
       ↓
moves source
AND
attached drawing
```

So if you:

1. stream a character from Blender;

2. draw construction lines over their head;

3. rotate the entire image object 15°;

4. update the character pose in Blender;

the construction drawing can remain attached to the image object's coordinate space.

You could still support normal independent canvas layers above it as well.

---

# 13. The last rendered image should become a cache

This directly implements your desired:

> if none is selected, or if a frame isn't currently streaming, it just stays as is

The `BlenderComicViewSource` should always have:

```
current_live_frame
        OR
last_cached_frame
```

Never:

```
"Blender disconnected"
        ↓
blank layer
```

Instead:

```
Blender connected
      ↓
new image 918
      ↓
cache image 918


Blender disconnected
      ↓
keep image 918


Comic View no longer active
      ↓
keep image 918
```

I'd even persist the last good frame into the drawing project.

That means opening the comic project without Blender available would still show:

```
all 3D panels
all drawings
all transformations
```

They're simply showing cached versions.

Then when Blender connects:

```
cached image
      ↓
new Blender image arrives
      ↓
source replaced
      ↓
all nondestructive operations remain
```

That's an excellent failure mode.

---

# 14. Thumbnails and full streams should be different systems

Every Comic View gets a cheap persistent thumbnail:

```
Blender Comic Views

┌────────┐
│        │  Panel 001
│   A    │
└────────┘

┌────────┐
│        │  Panel 002
│   B    │
└────────┘

┌────────┐
│        │  Panel 003
│   C    │
└────────┘
```

These are generated when:

```
Create Comic View
Update Comic View
Refresh Thumbnail
```

and cached.

Only one Comic View has:

```
ACTIVE STREAM
```

at a time.

Therefore having 200 comic views doesn't mean 200 render streams.

---

# 15. I wouldn't actually stream continuously when nothing changes

Internally I'd implement this as a **dirty-frame renderer**, not a literal 30/60 FPS video encoder.

```
Active Comic View
        │
        ▼
scene unchanged
        │
        └── DON'T RENDER


user moves character
        ↓
scene dirty
        ↓
render latest state
        ↓
send frame


user rapidly moves character
 ↓ ↓ ↓ ↓ ↓ ↓ ↓ ↓ ↓
many changes
        ↓
coalesce
        ↓
render latest state
```

So the renderer can target something like:

```
maximum 30 FPS
```

but doesn't render 30 identical images every second.

And importantly:

```
render slower than requested FPS
        ↓
DROP INTERMEDIATE REQUESTS
        ↓
always produce newest state
```

Never create a queue like:

```
frame 47
frame 48
frame 49
frame 50
...
```

that makes the drawing application several seconds behind Blender.

For this use case, **latency matters; frame completeness does not.**

---

# 16. Comic View revisions versus stream frames

I'd track both separately.

```
Comic View revision

View 17 revision 6
```

means:

> the stored panel state changed.

Whereas:

```
frame_sequence = 1842
```

means:

> this is the 1,842nd framebuffer Blender emitted.

That lets the drawing program know:

```
ComicView 17
revision 6
frame 1842
```

and reject something stale like:

```
ComicView 17
revision 5
frame 1839
```

if it somehow arrives late.

---

# 17. One critical detail: object identity

Do **not** identify things solely by Blender names.

You don't want:

```
CharacterArmature
```

being renamed to:

```
Alice_Rig
```

and suddenly 80 comic panels becoming broken.

I'd have the add-on assign persistent Comic IDs:

```
Alice Rig

comic_uuid:
4a451258-b802-...
```

Then a Comic View stores:

```
target_uuid
property
value
```

rather than relying primarily on:

```
"Object.001"
```

Names can remain human-readable metadata.

If something genuinely disappears, activation should produce:

```
Comic View loaded with warnings:

2 unresolved targets
```

rather than failing the entire view.

---

# 18. What the final workflow could feel like

In Blender:

```
COMIC VIEWS

[ + New View ]

┌────────────┐
│ thumbnail  │ Panel 14
└────────────┘

┌────────────┐
│ thumbnail  │ Panel 15   ← ACTIVE
└────────────┘

[ Update View ]
[ Duplicate View ]
[ Delete ]
```

You pose the character, move the camera, alter the lights, toggle props, adjust shape keys, then press:

```
Update View
```

The add-on captures that state and updates its thumbnail.

Meanwhile the drawing program has:

```
ImageObject #38

Source:
    Blender
    comic_view = Panel 15

Status:
    LIVE

Transform:
    x = 428
    y = 213
    rotation = -4°
    scale = 1.31

Effects:
    Levels
    Desaturate 20%
    Perspective warp

Attached drawing:
    6 strokes
```

Blender changes:

```
new framebuffer
       ↓
ImageObject source changes
       ↓
everything else stays untouched
```

---

# 19. The boundary I'd aim for

This is the whole architecture distilled down:

```
                         BLENDER

 Geometry ─────────────┐
 Materials ────────────┤
 Rig ──────────────────┤
                       ▼
                  Scene State

Comic View 1 ─────────►│
Comic View 2 ─────────►│ APPLY
Comic View 3 ─────────►│
                       ▼
                Blender evaluates
                       ▼
                 GPU viewport
                       ▼
                    RGBA
                       │
═══════════════════════╪════════════════════════
              PROCESS BOUNDARY
                       │
                       ▼
              BlenderImageSource
                       │
                       ▼
                   ImageObject
                       │
               ┌───────┼────────┐
               ▼       ▼        ▼
            Transform Effects Draw-over
               │       │        │
               └───────┼────────┘
                       ▼
                 Canvas compositor

                    PYSIDE6
```

That gives you an unusually strong abstraction:

> **Blender is not integrated into the drawing program. Blender is simply an image-source provider.**

And I'd make that abstraction generic enough that Blender is merely one implementation:

```
ImageSource
│
├── FileImageSource
├── ClipboardImageSource
├── BlenderComicViewSource
└── potentially other live sources later
```

That means essentially all of the complexity stays in two contained places: **the Blender Comic View add-on** and **the BlenderComicViewSource adapter**. Your transform tool, drawing system, layer compositor and future nondestructive image operations shouldn't need to care where the pixels came from.

plan to make a working prototype of this system.