"""Opt-in Blender CLI probe using the same operator as Image > Edit Externally.

Run with Blender --background --python this_file -- --editor EXE --image PNG
--report JSON. --configure also saves the external-editor preference, keeping
a backup beside the report. Only the dedicated test image is loaded.
"""
import argparse
import json
from pathlib import Path
import shutil
import sys
import time

import bpy


parser = argparse.ArgumentParser()
parser.add_argument("--editor", required=True)
parser.add_argument("--image", required=True)
parser.add_argument("--report", required=True)
parser.add_argument("--configure", action="store_true")
options = parser.parse_args(sys.argv[sys.argv.index("--") + 1:])
if options.configure and "--factory-startup" in sys.argv:
    parser.error("Configure must load existing preferences; omit --factory-startup.")
editor = Path(options.editor).resolve(strict=True)
image_path = Path(options.image).resolve(strict=True)
report_path = Path(options.report).resolve()
report_path.parent.mkdir(parents=True, exist_ok=True)
report = {"blender": bpy.app.version_string,
          "previous_editor": bpy.context.preferences.filepaths.image_editor,
          "editor": str(editor), "image": str(image_path)}
bpy.context.preferences.filepaths.image_editor = str(editor)
image = bpy.data.images.load(str(image_path), check_existing=False)
area = next(iter(bpy.context.screen.areas))
area.type = "IMAGE_EDITOR"
area.spaces.active.image = image
with bpy.context.temp_override(area=area):
    # Background Blender executes operators without their UI invoke stage.
    # Resolve the same image-user path that invoke() passes to execute().
    filepath = bpy.path.abspath(image.filepath_from_user(image_user=area.spaces.active.image_user))
    result = bpy.ops.image.external_edit(filepath=filepath)
report["operator_result"] = sorted(result)
project = image_path.with_suffix("")
deadline = time.monotonic() + 30
while not (project / "series.json").is_file() and time.monotonic() < deadline:
    time.sleep(0.1)
assert (project / "series.json").is_file(), "The editor did not open the image"
chapter_path = next((project / "chapters").glob("*/chapter.json"))
chapter = json.loads(chapter_path.read_text(encoding="utf-8"))
assert chapter["size"] == list(image.size), (chapter["size"], list(image.size))
assert chapter["external_image_path"] == str(image_path)
report.update(project=str(project), size=chapter["size"],
              document_kind=chapter["document_kind"], chapter_id=chapter["id"])
if options.configure:
    preference_file = Path(bpy.utils.user_resource("CONFIG", path="userpref.blend"))
    if preference_file.is_file():
        backup = report_path.parent / "userpref-before-external-editor.blend"
        if not backup.exists():
            shutil.copy2(preference_file, backup)
        report["preferences_backup"] = str(backup)
    report["saved_preferences"] = sorted(bpy.ops.wm.save_userpref())
report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
print("WEBTOON_EXTERNAL_EDITOR_OK " + json.dumps(report), flush=True)
