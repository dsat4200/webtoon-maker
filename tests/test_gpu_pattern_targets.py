"""Real-driver coverage for target-layer halftone colors and HSL controls.

Use QT_QPA_PLATFORM=windows (or another desktop backend) to exercise OpenGL.
The regular offscreen test suite safely skips tests without a usable driver.
"""
from dataclasses import replace

import numpy as np
import pytest
from PySide6.QtGui import QColor, QImage, QPainter

from comic_editor.core.models import HalftoneModifier
from comic_editor.ui.gpu_pattern_effects import GpuPatternRenderer


@pytest.fixture(scope="module")
def gpu(qapp):
    renderer = GpuPatternRenderer()
    if not renderer.available:
        pytest.skip(renderer.reason)
    yield renderer
    renderer.close()


def solid(color, width=100, height=100):
    result=QImage(width,height,QImage.Format_ARGB32_Premultiplied)
    result.fill(QColor(*color))
    return result


def pixels(image):
    image=image.convertToFormat(QImage.Format_RGBA8888_Premultiplied)
    return np.frombuffer(image.constBits(),np.uint8).reshape(image.height(),image.bytesPerLine())[:,:image.width()*4].reshape(image.height(),image.width(),4).copy()


def modifier(**kwargs):
    return HalftoneModifier(**({"base_resolution":100,"spacing":16,"blur":0,
                               "transparent_background":True,"color_mode":"target_layer"}|kwargs))


@pytest.mark.parametrize("grid,style",[(grid,"circle") for grid in
    ("square","hexagonal","radial","line","ring","stippling")]+[("square",style) for style in
    ("incircle","triangle","square","polygon","line","blob","delaunay","liquid")])
def test_target_changes_ink_only_for_every_grid_and_shape(gpu,grid,style):
    owner=solid((70,70,70,113))
    target=solid((255,0,0,17))
    effect=modifier(grid_type=grid,dot_style=style)
    reference=pixels(gpu.render(owner,replace(effect,color_mode="source")))
    colored=pixels(gpu.render(owner,effect,color_source=target))
    np.testing.assert_array_equal(colored[...,3],reference[...,3])
    np.testing.assert_array_equal(colored[...,0],colored[...,3])
    assert not colored[...,1:3].any()
    assert colored[...,3].max()==113


@pytest.mark.parametrize("missing",[None,"null","transparent","wrong_size"])
def test_unavailable_target_falls_back_to_incoming_rgb(gpu,missing):
    owner=solid((160,40,90,127))
    target={None:None,"null":QImage(),"transparent":solid((0,255,0,0)),
            "wrong_size":solid((0,255,0,255),99,100)}[missing]
    effect=modifier()
    actual=pixels(gpu.render(owner,effect,color_source=target))
    expected=pixels(gpu.render(owner,replace(effect,color_mode="source")))
    np.testing.assert_array_equal(actual,expected)


def test_target_samples_the_same_spatial_cell_centers(gpu):
    owner=solid((70,70,70,255))
    target=solid((255,0,0,255))
    painter=QPainter(target)
    painter.fillRect(50,0,50,100,QColor("blue"))
    painter.end()
    result=pixels(gpu.render(owner,modifier(spacing=20),color_source=target))
    assert result[50,30,0]>240 and result[50,30,2]<5
    assert result[50,70,2]>240 and result[50,70,0]<5


@pytest.mark.parametrize("adjustment,expected",[
    ({"target_hue":120},(0,1,0)),
    ({"target_saturation":-100},(.5,.5,.5)),
    ({"target_lightness":-100},(0,0,0)),
    ({"target_lightness":100},(1,1,1)),
])
def test_target_hsl_controls_modify_rgb_without_changing_marks(gpu,adjustment,expected):
    owner=solid((70,70,70,113))
    target=solid((255,0,0,255))
    effect=modifier(**adjustment)
    actual=pixels(gpu.render(owner,effect,color_source=target))
    baseline=pixels(gpu.render(owner,modifier(),color_source=target))
    np.testing.assert_array_equal(actual[...,3],baseline[...,3])
    expected_rgb=actual[...,3:4].astype(float)*np.array(expected)
    assert np.max(np.abs(actual[...,:3].astype(float)-expected_rgb))<=1


def test_hsl_adjusts_fallback_and_is_exclusive_to_target_mode(gpu):
    owner=solid((255,0,0,255))
    adjusted=modifier(target_hue=120)
    actual=pixels(gpu.render(owner,adjusted))
    np.testing.assert_array_equal(actual[...,1],actual[...,3])
    assert not actual[...,(0,2)].any()
    source=pixels(gpu.render(owner,replace(adjusted,color_mode="source")))
    reference=pixels(gpu.render(owner,replace(modifier(),color_mode="source")))
    np.testing.assert_array_equal(source,reference)


def test_owner_and_target_uploads_are_cached_independently(gpu):
    owner=solid((70,70,70,255))
    target=solid((255,0,0,255))
    effect=modifier()
    owner_uploads,target_uploads=gpu.uploads,gpu.color_uploads
    for hue in (0,30,60,90):
        effect.target_hue=hue
        assert gpu.render(owner,effect,color_source=target) is not None
    assert gpu.uploads==owner_uploads+1
    assert gpu.color_uploads==target_uploads+1
    target.fill(QColor("blue"))
    assert gpu.render(owner,effect,color_source=target) is not None
    assert gpu.uploads==owner_uploads+1
    assert gpu.color_uploads==target_uploads+2
