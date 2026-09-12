# Comic texture material

`comic_texture.blend` contains only the `mug` material from the supplied
`blender_extension/material.blend`, renamed to `Webtoon Comic Texture` and tagged
for the extension's node controls. The original file and its backup are unchanged.
The source SHA-256 is
`28e796dde4a9413068ff442fde00b307d8c03f6af33cdfbb4927e4e12e3aa98c`.

The material retains all 18 nodes, 19 links, ramp stops, positions, blend modes,
socket defaults, and blended surface settings. Its old Image Texture image is
cleared so no external user texture is included. Create Texture assigns the
new image and material name. Subsurface method is normalized to `RANDOM_WALK`
when importing because the enum differs between Blender 4.5 and 5.2; the supplied
shader's Subsurface Weight remains zero.

The library was exported with Blender 4.5 to support both versions. It contains
no objects, scenes, or images. Shader to RGB requires Eevee for this comic shading.
