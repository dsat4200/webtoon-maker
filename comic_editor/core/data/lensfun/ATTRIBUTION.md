# Lensfun profile data

`profiles.xml` contains a selected and modified subset of the **Lensfun lens
database**, copyright the Lensfun database contributors, licensed under
**Creative Commons Attribution-ShareAlike 3.0 Unported (CC BY-SA 3.0)**.

- Project: <https://lensfun.github.io/>
- Source: <https://github.com/lensfun/lensfun>
- Pinned revision: `bbd4332a9ec566fd9aa548c9e0d8ced238c56261`
- Source files: [`slr-canon.xml`](https://github.com/lensfun/lensfun/blob/bbd4332a9ec566fd9aa548c9e0d8ced238c56261/data/db/slr-canon.xml)
  and [`slr-nikon.xml`](https://github.com/lensfun/lensfun/blob/bbd4332a9ec566fd9aa548c9e0d8ced238c56261/data/db/slr-nikon.xml)
- License: <https://creativecommons.org/licenses/by-sa/3.0/>
- The complete upstream license text is included as `COPYING.CC_BY-SA_3.0`.

Modifications: selected four cameras and nine calibrated lens/sensor records,
combined them into one XML file, and removed non-geometric calibration entries
(such as vignetting and chromatic aberration). The original geometric distortion
measurements, maker/model identifiers, mounts, and sensor crop factors were
retained. This adapted XML dataset is distributed under the same CC BY-SA 3.0
license. Lensfun and its contributors do not endorse Webtoon Maker.

The Python loader and renderer are independently written. No Lensfun library
code is bundled. They read Lensfun's documented `poly3`, `poly5`, and `ptlens`
models, linearly interpolate neighboring focal-length measurements, and account
for the selected camera's crop factor. Imported Lensfun XML can be embedded in
projects to keep the profile available without a network connection or the
original external file. Correction applies geometric distortion only.
