"""Adaptive vector cage baking: retain editable cubics and pressure channels."""
import copy
import numpy as np
from comic_editor.core.cage import map_points
from comic_editor.core.models import VectorStrokePoint
from comic_editor.core.vector_geometry import cubic_eval, cubic_derivative
from comic_editor.ui.cage_rendering import transform_points


def warp_vector(obj, grid, mapping, tolerance=.2):
    inverse, valid = mapping.inverted()
    if not valid:
        raise ValueError("Cannot deform a vector drawing with a singular placement")
    result = copy.deepcopy(obj)

    def warp(points):
        return transform_points(inverse, map_points(grid, transform_points(mapping, points)))

    def width_scale(point):
        p = np.asarray(point)
        a, b, c = warp([p, p+(.01, 0), p+(0, .01)])
        jacobian = np.stack((b-a, c-a), axis=1)/.01
        return float(np.sqrt(abs(np.linalg.det(jacobian))))

    for stroke in result.strokes:
        original = list(stroke.points)
        if len(original) == 1:
            original[0].width *= width_scale(original[0].position)
            original[0].position = tuple(warp([original[0].position])[0])
            stroke.touch_render_revision()
            continue
        output = []
        segments = len(original) if stroke.closed else len(original)-1
        for index in range(segments):
            start, end = original[index], original[(index+1) % len(original)]
            curve = (start.position, start.outgoing or start.position,
                     end.incoming or end.position, end.position)
            accepted = []

            def fit(t0, t1, depth=0):
                # Chain-rule tangent transport through the nonlinear cage.
                positions = np.asarray([cubic_eval(curve, t0), cubic_eval(curve, t1)])
                derivatives = np.asarray([cubic_derivative(curve, t0), cubic_derivative(curve, t1)])
                mapped = warp(positions)
                tangent = (warp(positions+derivatives*1e-4)-warp(positions-derivatives*1e-4))/2e-4
                fitted = (tuple(mapped[0]), tuple(mapped[0]+tangent[0]*(t1-t0)/3),
                          tuple(mapped[1]-tangent[1]*(t1-t0)/3), tuple(mapped[1]))
                ratios = np.asarray((.25, .5, .75))
                exact = warp([cubic_eval(curve, t0+(t1-t0)*t) for t in ratios])
                estimate = np.asarray([cubic_eval(fitted, t) for t in ratios])
                # Compare error in document space, not possibly scaled local space.
                error = np.linalg.norm(transform_points(mapping, exact)-transform_points(mapping, estimate), axis=1).max()
                if error > tolerance and depth < 10:
                    mid = (t0+t1)/2
                    fit(t0, mid, depth+1)
                    fit(mid, t1, depth+1)
                else:
                    accepted.append((t0, t1, fitted))

            fit(0., 1.)
            for t0, t1, fitted in accepted:
                if not output:
                    node = copy.deepcopy(start)
                    node.position = fitted[0]
                    node.width = start.width*width_scale(start.position)
                    output.append(node)
                output[-1].outgoing = fitted[1]
                if t1 == 1.:
                    node = copy.deepcopy(end)
                else:
                    node = VectorStrokePoint(width=start.width+(end.width-start.width)*t1,
                                             opacity=start.opacity+(end.opacity-start.opacity)*t1)
                node.width *= width_scale(cubic_eval(curve, t1))
                node.position, node.incoming = fitted[3], fitted[2]
                output.append(node)
        if stroke.closed:
            output[0].incoming = output[-1].incoming
            output.pop()
        stroke.points = output
        stroke.touch_render_revision()
    result.touch_revision()
    return result
