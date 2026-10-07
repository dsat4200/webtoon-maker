"""Frozen native asset extraction and ordinary scene thumbnail preparation."""
from comic_editor.core.assets import AssetRepository, extract_asset
from comic_editor.render.admission import RENDER_ADMISSION, snapshot_working_bytes
from comic_editor.render.outputs import asset_thumbnail, capture_document


def prepare_asset(snapshot, kind, identifier, name):
    # Extracting bounds may decode cold originals. Keep that work on the
    # detached owner and use the captured revision's immutable file pins.
    with RENDER_ADMISSION.reserve('asset-copy', snapshot_working_bytes(snapshot), priority=1):
        snapshot.finish_sources()
        manifest, tiles, images = extract_asset(snapshot.chapter, snapshot.tiles,
            kind, identifier, name, source_images=snapshot.images, include_images=True)
        frozen = capture_document(manifest.document, tiles, images,
                                  pixel_environment=snapshot.pixel_environment)
        thumbnail = asset_thumbnail(frozen, manifest)
        return manifest, tiles, images, thumbnail


def publish_asset(root, prepared, existing_id, folder_id):
    repository = AssetRepository(root)
    manifest, tiles, images, thumbnail = prepared
    if existing_id:
        manifest = repository.replace(existing_id, manifest, tiles, thumbnail, images=images)
    else:
        manifest = repository.create(manifest, tiles, thumbnail, images=images, folder_id=folder_id)
    return manifest, tiles, images
