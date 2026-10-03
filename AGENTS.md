# Rendering and cache constraints

Camera zoom and display density must not increase derived artwork cache
resolution. Final exact document tiles use one sample per canvas pixel;
transient previews may use less. Preserve original source pixels, source/effect
sampling grids, and color/precision contracts required for native output.
Presentation surfaces and editing gizmos may use physical display resolution.

Disk caching uses the same semantic cache keys, dependency validation, exact
results, and rendering pipeline as memory caching. Never introduce a separate
disk renderer or independent invalidation logic. Drafts and live editing state
must never become durable exact cache entries.

Any sampling exception requires documented native-pixel correctness and
performance evidence. Read the rendering and persistence guides in docs/llm
before changing these contracts. Preserve unrelated work in the checkout.
